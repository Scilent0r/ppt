import os
import sqlite3
import pandas as pd
import requests
import streamlit as st
from datetime import datetime

# Page config
st.set_page_config(page_title="🥛 Protein Dairy Prices", layout="wide")
st.title("🥛 Protein Dairy Prices")
st.caption("Latest data pulled from GitHub every time you load this app.")

# URL and path
db_url = "https://github.com/Scilent0r/ppt/raw/refs/heads/main/dairy_prices.db"
db_path = "./sqlite-tools/dairy_prices.db"

# Ensure directory exists
os.makedirs(os.path.dirname(db_path), exist_ok=True)

# Rename existing DB
if os.path.isfile(db_path):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{db_path.rstrip('.db')}_{timestamp}.db"
    os.rename(db_path, backup_path)
    st.info(f"Old DB backed up as `{os.path.basename(backup_path)}`")

# Download DB
st.write("🔄 Downloading the latest protein dairy database...")
response = requests.get(db_url)
if response.status_code == 200:
    with open(db_path, "wb") as f:
        f.write(response.content)
    st.success("✅ Database downloaded successfully.")
else:
    st.error(f"❌ Failed to download database. Status code: {response.status_code}")
    st.stop()

# Load data
conn = sqlite3.connect(db_path)
df = pd.read_sql_query(
    """
    SELECT name, category, price, price_per_kg, protein_per_kg,
           calories_per_kg, calories_per_gram_protein, source_unit,
           url, last_updated
    FROM dairy_products
    """,
    conn,
)
conn.close()

if df.empty:
    st.warning("No data in the database yet.")
    st.stop()

# --- Sorting controls ---
SORT_PRICE = "Price per kilo (cheapest first)"
SORT_EFFICIENCY = "Kcal per gram of protein (most efficient first)"

sort_choice = st.radio(
    "Sort by",
    [SORT_PRICE, SORT_EFFICIENCY],
    horizontal=True,
)

# Primary = whichever the user picked, secondary = the other one, so ties
# resolve sensibly. Default radio selection is SORT_PRICE, which gives the
# requested default order: price per kilo, then kcal per gram of protein.
if sort_choice == SORT_PRICE:
    sort_cols = ["price_per_kg", "calories_per_gram_protein"]
else:
    sort_cols = ["calories_per_gram_protein", "price_per_kg"]

display_df = df.sort_values(
    by=sort_cols, ascending=[True, True], na_position="last"
).reset_index(drop=True)

# --- Category filter (keeps the table manageable) ---
categories = sorted(display_df["category"].dropna().unique())
selected_categories = st.multiselect("Category", categories, default=categories)
display_df = display_df[display_df["category"].isin(selected_categories)]

# --- Formatting for display ---
show_df = display_df.copy()
show_df["price"] = show_df["price"].map(lambda x: f"{x:.2f}" if pd.notna(x) else "-")
show_df["price_per_kg"] = show_df["price_per_kg"].map(lambda x: f"{x:.2f}" if pd.notna(x) else "-")
show_df["protein_per_kg"] = show_df["protein_per_kg"].map(lambda x: f"{x:.1f}" if pd.notna(x) else "-")
show_df["calories_per_kg"] = show_df["calories_per_kg"].map(lambda x: f"{x:.0f}" if pd.notna(x) else "-")
show_df["calories_per_gram_protein"] = show_df["calories_per_gram_protein"].map(
    lambda x: f"{x:.3f}" if pd.notna(x) else "-"
)

show_df = show_df.rename(
    columns={
        "name": "Product",
        "category": "Category",
        "price": "Price (€)",
        "price_per_kg": "€/kg",
        "protein_per_kg": "Protein g/kg",
        "calories_per_kg": "kcal/kg",
        "calories_per_gram_protein": "kcal per g protein",
        "source_unit": "Priced per",
        "url": "Link",
        "last_updated": "Updated",
    }
)

column_order = [
    "Product",
    "Category",
    "Price (€)",
    "€/kg",
    "Protein g/kg",
    "kcal/kg",
    "kcal per g protein",
    "Priced per",
    "Updated",
    "Link",
]

st.dataframe(
    show_df[column_order],
    use_container_width=True,
    hide_index=True,
    column_config={
        "Link": st.column_config.LinkColumn("Link", display_text="Open"),
    },
)

st.caption(f"{len(display_df)} products shown, sorted by {sort_choice.lower()}.")
