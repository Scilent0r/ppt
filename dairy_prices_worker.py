"""
dairy_prices.py

Scrapes protein yogurt / dairy dessert products from s-kaupat.fi and
stores name, price/kg, protein/kg and calories/kg into SQLite.
Overwrites (UPSERTs) previous values on collision by product id.

Built on the same GraphQL pattern as beer_prices.py for the *listing*
(RemoteFilteredProducts, same persisted query hash, just a different
slug + queryString). The listing API almost certainly does NOT return
nutrition data (protein/calories) - that lives in a table on each
product's own detail page - so this adds a second pass: for every
product found, GET its detail page HTML directly with requests and
regex out the nutrition table.

UNTESTED AGAINST THE LIVE SITE (I have no network path to s-kaupat.fi
to verify from where I'm running). Two things are genuinely uncertain
until you run this:

1. Field names on the listing API response for the product's stable id
   and/or its own URL slug. beer_prices.py only ever reads `name` and
   `pricing.currentPrice`, so we don't actually know what else the
   persisted query returns. This script defensively tries a few likely
   key names and PRINTS the raw JSON of the first product it sees so
   you can tell me the real key names if my guesses are wrong.

2. Whether product detail pages are server-rendered (so plain
   requests.get() sees the nutrition table in the HTML) or client-side
   rendered only (in which case requests will get an empty shell and
   protein/calories will come back None for everything). If you see
   nutrition consistently None, that's the tell - let me know and we
   can look at the page's own GraphQL calls (browser devtools > Network
   > Fetch/XHR on a product page) for a persisted-query hash we can
   call directly instead of scraping HTML.

Usage:
    python dairy_prices.py
"""

import json
import random
import re
import sqlite3
import time
from datetime import datetime
from urllib.parse import quote

import requests

DB_PATH = "dairy_prices.db"
BASE_URL = "https://api.s-kaupat.fi/"
STORE_ID = "513971200"  # same store as beer_prices.py - change if needed

# Reuse the same persisted query hash as beer_prices.py. Persisted-query
# hashes key off the GraphQL query document, not the variables, so the
# same hash should work for any slug/queryString - it's the same
# RemoteFilteredProducts search query the site uses everywhere.
CURRENT_SHA256 = "ff102bea7318821d5d984b890d0a6322d2e3d9c01ba50e6eed6adb865c63efe1"

CATEGORIES = [
    ("jogurtit", "maito-munat-ja-rasvat/jogurtit"),
    ("rahkat-vanukkaat-ja-jalkiruoka", "maito-munat-ja-rasvat/rahkat-vanukkaat-ja-jalkiruoka"),
]
QUERY_STRING = "proteiini"
LIMIT = 24

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "fi-FI,fi;q=0.9,en;q=0.8",
    "Referer": "https://www.s-kaupat.fi/",
    "Origin": "https://www.s-kaupat.fi",
}

HTML_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "fi-FI,fi;q=0.9,en;q=0.8",
    "Referer": "https://www.s-kaupat.fi/",
}

PAGE_DELAY = 3.5          # increased to reduce 429s
MAX_RETRIES = 5

PROTEIN_RE = re.compile(r"Proteiinia\D{0,20}?(\d+(?:[,.]\d+)?)\s*g", re.IGNORECASE)
ENERGY_RE = re.compile(r"Energia\D{0,20}?[\d,.]+\s*kJ\s*/\s*(\d+[,.]?\d*)\s*kcal", re.IGNORECASE)

_UMLAUT_MAP = str.maketrans({"ä": "a", "ö": "o", "å": "a", "®": "r", "%": ""})


def fi_float(s):
    return float(s.replace(",", "."))


def slugify(name: str) -> str:
    s = name.lower().translate(_UMLAUT_MAP)
    s = s.replace(",", "-")
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s


def init_db(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dairy_products (
            product_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            category TEXT,
            price REAL,
            price_per_kg REAL,
            protein_per_kg REAL,
            calories_per_kg REAL,
            source_unit TEXT,
            url TEXT,
            last_updated TEXT NOT NULL
        )
        """
    )
    # Migration: if this is an existing db from before calories_per_gram_protein
    # existed, add it rather than erroring on the next upsert.
    cursor = conn.execute("PRAGMA table_info(dairy_products)")
    existing_cols = {row[1] for row in cursor.fetchall()}
    if "calories_per_gram_protein" not in existing_cols:
        conn.execute("ALTER TABLE dairy_products ADD COLUMN calories_per_gram_protein REAL")
    conn.commit()


def fetch_listing_page(slug, query_string, offset):
    variables = {
        "facets": [
            {"key": "brandName", "order": "asc"},
            {"key": "labels"},
        ],
        "generatedSessionId": "0b5cd573-3cfe-4814-9a9f-ac33f5140c37",
        "fetchSponsoredContent": True,
        "includeAgeLimitedByAlcohol": True,
        "limit": LIMIT,
        "queryString": query_string,
        "slug": slug,
        "storeId": STORE_ID,
        "useRandomId": False,
        "marketingId": "e5b2ded0-b696-44f7-afdd-c5dc73ac20f4",
        "from": offset,
    }
    extensions = {"persistedQuery": {"version": 1, "sha256Hash": CURRENT_SHA256}}
    params = {
        "operationName": "RemoteFilteredProducts",
        "variables": json.dumps(variables),
        "extensions": json.dumps(extensions),
    }
    url = (
        f"{BASE_URL}?operationName={params['operationName']}"
        f"&variables={quote(params['variables'])}"
        f"&extensions={quote(params['extensions'])}"
    )
    response = requests.get(url, headers=HEADERS, timeout=12)
    response.raise_for_status()
    data = response.json()
    if "errors" in data:
        raise RuntimeError(json.dumps(data["errors"]))
    return data.get("data", {}).get("store", {}).get("products", {}).get("items", [])


def extract_product_id(product: dict):
    for key in ("ean", "id", "productId", "sku", "gtin", "code"):
        val = product.get(key)
        if val:
            return str(val)
    return None


def build_product_url(product: dict, product_id):
    """Build the product detail URL. The site needs BOTH the slug and the
    trailing product id (e.g. /tuote/<slug>/<ean>) - a slug alone 404s.
    So whatever candidate we find (a returned url/slug field, or a
    slugified name as last resort), we make sure product_id ends up
    appended at the end."""
    candidate = None
    for key in ("url", "canonicalUrl", "productUrl", "slug"):
        val = product.get(key)
        if val:
            candidate = val
            break
    if candidate:
        if candidate.startswith("http"):
            full = candidate
        elif candidate.startswith("/"):
            full = f"https://www.s-kaupat.fi{candidate}"
        else:
            full = f"https://www.s-kaupat.fi/tuote/{candidate}"
    else:
        full = f"https://www.s-kaupat.fi/tuote/{slugify(product.get('name', ''))}"
    if product_id and not full.rstrip("/").endswith(str(product_id)):
        full = full.rstrip("/") + f"/{product_id}"
    return full


def collect_products(category_label, slug):
    offset = 0
    results = []
    first_product_logged = False
    while True:
        try:
            items = fetch_listing_page(slug, QUERY_STRING, offset)
        except Exception as e:
            print(f"  [error] {category_label} offset={offset}: {e}")
            break
        if not items:
            break
        if not first_product_logged:
            print(f"  [debug] raw first product from '{category_label}':")
            print("  " + json.dumps(items[0], indent=2, ensure_ascii=False)[:1500])
            first_product_logged = True
        for product in items:
            name = product.get("name")
            price = product.get("pricing", {}).get("currentPrice")
            if not name or price is None:
                continue
            results.append((category_label, product))
        print(f"  [{category_label}] offset={offset}: {len(items)} items (total so far {len(results)})")
        offset += LIMIT
        time.sleep(PAGE_DELAY + random.uniform(0.5, 1.5))
    return results


def scrape_nutrition(url):
    """Fetch a product detail page and regex out protein/kcal per 100g.
    Returns (protein_per_100g, kcal_per_100g), either possibly None.
    Retries on 429 with exponential backoff + jitter.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=HTML_HEADERS, timeout=15)

            if resp.status_code == 429:
                # Exponential backoff: ~5s, 10s, 20s, 40s, 80s + jitter
                wait = (2 ** attempt) * 2.5 + random.uniform(0.5, 2.0)
                print(f"    [429] rate limited – sleeping {wait:.1f}s (attempt {attempt}/{MAX_RETRIES})")
                time.sleep(wait)
                continue

            resp.raise_for_status()
            text = resp.text
            protein_match = PROTEIN_RE.search(text)
            energy_match = ENERGY_RE.search(text)
            protein = fi_float(protein_match.group(1)) if protein_match else None
            kcal = fi_float(energy_match.group(1)) if energy_match else None
            return protein, kcal

        except requests.exceptions.RequestException as e:
            print(f"    [error] fetching {url}: {e}")
            if attempt == MAX_RETRIES:
                return None, None
            time.sleep(3 + random.uniform(0, 2))

    return None, None


def upsert_product(cursor, product_id, category, url, name, price, price_per_kg,
                    protein_per_kg, calories_per_kg, calories_per_gram_protein,
                    source_unit, now):
    cursor.execute(
        """
        INSERT INTO dairy_products
            (product_id, name, category, price, price_per_kg, protein_per_kg,
             calories_per_kg, calories_per_gram_protein, source_unit, url, last_updated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(product_id) DO UPDATE SET
            name=excluded.name,
            category=excluded.category,
            price=excluded.price,
            price_per_kg=excluded.price_per_kg,
            protein_per_kg=excluded.protein_per_kg,
            calories_per_kg=excluded.calories_per_kg,
            calories_per_gram_protein=excluded.calories_per_gram_protein,
            source_unit=excluded.source_unit,
            url=excluded.url,
            last_updated=excluded.last_updated
        """,
        (product_id, name, category, price, price_per_kg, protein_per_kg,
         calories_per_kg, calories_per_gram_protein, source_unit, url, now),
    )


def main():
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    cursor = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    all_products = []
    for category_label, slug in CATEGORIES:
        print(f"Collecting listing for '{category_label}'...")
        all_products.extend(collect_products(category_label, slug))
    print(f"\nTotal listing items collected: {len(all_products)}\n")
    upserted = 0
    for i, (category_label, product) in enumerate(all_products, start=1):
        name = product.get("name")
        price = product.get("pricing", {}).get("currentPrice")
        comparison_price = product.get("pricing", {}).get("comparisonPrice")
        comparison_unit = product.get("pricing", {}).get("comparisonUnit") or "kg"
        product_id = extract_product_id(product)
        if not product_id:
            # Last resort: not a real stable id, just so we have a primary key.
            # URL built from this alone will likely 404 - watch for that in
            # the [error] fetching logs below and tell me if it's common,
            # since it means the listing API isn't giving us an id at all.
            product_id = slugify(name)
        url = build_product_url(product, product_id)
        print(f"[{i}/{len(all_products)}] {name}")

        # Skip scraping if we already have nutrition data for this product
        cursor.execute(
            "SELECT protein_per_kg, calories_per_kg FROM dairy_products WHERE product_id = ?",
            (product_id,),
        )
        row = cursor.fetchone()
        if row and row[0] is not None and row[1] is not None:
            print("    [skip] already has nutrition data")
            # Still update price / last_updated
            protein_per_kg = row[0]
            calories_per_kg = row[1]
            calories_per_gram_protein = (
                round(calories_per_kg / protein_per_kg, 3) if protein_per_kg else None
            )
            price_per_kg = comparison_price if comparison_price is not None else price
            upsert_product(
                cursor, product_id, category_label, url, name, price, price_per_kg,
                protein_per_kg, calories_per_kg, calories_per_gram_protein,
                comparison_unit, now,
            )
            conn.commit()
            upserted += 1
            print(
                f"    price/kg={price_per_kg} protein/kg={protein_per_kg} "
                f"kcal/kg={calories_per_kg} kcal/g_protein={calories_per_gram_protein}"
            )
            time.sleep(0.3)  # tiny pause even on skip
            continue

        protein_100g, kcal_100g = scrape_nutrition(url)
        price_per_kg = comparison_price if comparison_price is not None else price
        protein_per_kg = protein_100g * 10 if protein_100g is not None else None
        calories_per_kg = kcal_100g * 10 if kcal_100g is not None else None
        calories_per_gram_protein = (
            round(calories_per_kg / protein_per_kg, 3)
            if protein_per_kg
            else None
        )
        upsert_product(
            cursor, product_id, category_label, url, name, price, price_per_kg,
            protein_per_kg, calories_per_kg, calories_per_gram_protein,
            comparison_unit, now,
        )
        conn.commit()
        upserted += 1
        print(
            f"    price/kg={price_per_kg} protein/kg={protein_per_kg} "
            f"kcal/kg={calories_per_kg} kcal/g_protein={calories_per_gram_protein}"
        )
        time.sleep(PAGE_DELAY + random.uniform(0.3, 1.2))
    conn.close()
    print(f"\nDone. Upserted {upserted}/{len(all_products)} products into {DB_PATH}")


if __name__ == "__main__":
    main()
