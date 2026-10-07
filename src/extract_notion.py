"""Extract the light novel list from Notion into a clean CSV.

Steps:
1. Find the data source behind the database
2. Fetch ALL rows (pagination: Notion returns max 100 rows per call)
3. Flatten Notion's nested JSON into simple columns
4. Save to data/raw/notion_light_novels.csv

The "Pareri" (comments) column is deliberately NOT extracted.
"""

import os
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from notion_client import Client

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = PROJECT_ROOT / "data" / "raw" / "notion_light_novels.csv"


# --------------------------------------------------------------------------
# 1. Connect
# --------------------------------------------------------------------------
def get_client() -> Client:
    return Client(auth=os.environ["NOTION_TOKEN"])


def get_data_source_id(notion: Client, database_id: str) -> str:
    """A Notion database contains one or more data sources (the real tables)."""
    db = notion.databases.retrieve(database_id=database_id)
    return db["data_sources"][0]["id"]


# --------------------------------------------------------------------------
# 2. Fetch all rows with pagination
# --------------------------------------------------------------------------
def fetch_all_pages(notion: Client, data_source_id: str) -> list[dict]:
    """Keep asking for the next batch until Notion says there is no more."""
    pages = []
    cursor = None

    while True:
        response = notion.data_sources.query(
            data_source_id=data_source_id,
            page_size=100,
            start_cursor=cursor,
        ) if cursor else notion.data_sources.query(
            data_source_id=data_source_id,
            page_size=100,
        )

        pages.extend(response["results"])

        if not response["has_more"]:
            break
        cursor = response["next_cursor"]

    return pages


# --------------------------------------------------------------------------
# 3. Flatten: each Notion property type hides its value in a different place
# --------------------------------------------------------------------------
def get_title(prop: dict) -> str | None:
    parts = prop.get("title", [])
    text = "".join(part["plain_text"] for part in parts).strip()
    return text or None


def get_status(prop: dict) -> str | None:
    value = prop.get("status")
    return value["name"] if value else None


def get_number(prop: dict) -> float | None:
    return prop.get("number")


def get_url(prop: dict) -> str | None:
    return prop.get("url")


def flatten_page(page: dict) -> dict:
    """Turn one nested Notion page into one flat row.

    Columns are picked explicitly, so nothing private (like comments)
    can come through by accident.
    """
    props = page["properties"]
    return {
        "notion_id": page["id"],
        "title": get_title(props["Title"]),
        "rating": get_status(props["Rating"]),  # status type -> text label
        "status": get_status(props["Status"]),
        "chapters": get_number(props["Chapters"]),
        "url": get_url(props["URL"]),
        "last_edited_time": page["last_edited_time"],
    }


def pages_to_dataframe(pages: list[dict]) -> pd.DataFrame:
    rows = [
        flatten_page(p)
        for p in pages
        if not p.get("in_trash") and not p.get("archived")
    ]
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# 4. Run
# --------------------------------------------------------------------------
def main() -> None:
    notion = get_client()
    database_id = os.environ["NOTION_LN_DATABASE_ID"]

    data_source_id = get_data_source_id(notion, database_id)
    pages = fetch_all_pages(notion, data_source_id)
    df = pages_to_dataframe(pages)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUTPUT_PATH, index=False, encoding="utf-8")

    print(f"Rows extracted: {len(df)}")
    print(f"Saved to: {OUTPUT_PATH}\n")
    print("Rating values:")
    print(df["rating"].value_counts(dropna=False).to_string(), "\n")
    print("Status values:")
    print(df["status"].value_counts(dropna=False).to_string(), "\n")
    print("First 5 rows:")
    print(df.head().to_string())


if __name__ == "__main__":
    main()
