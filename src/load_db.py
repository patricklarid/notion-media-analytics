"""Load the enriched light novel table into a SQLite database.

Input : data/raw/enriched_light_novels.csv
Output: data/media.db

Two tables:
  items          one row per light novel in my Notion list
  series_details extra info from MangaUpdates (only for trusted matches)

The load rebuilds the tables each time, so running it twice never
creates duplicates.
"""

import sqlite3
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
ENRICHED_CSV = ROOT / "data" / "raw" / "enriched_light_novels.csv"
DB_PATH = ROOT / "data" / "media.db"

# Matches we trust enough to join to MangaUpdates details
ACCEPTED = {"NOVEL_OK", "ADAPTATION_ONLY", "MANUAL"}
# Rows that should not be in the database at all
EXCLUDED = {"DUPLICATE", "INVALID"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    notion_id     TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    rating        TEXT,
    rating_score  INTEGER,
    status        TEXT,
    chapters      REAL,
    match_status  TEXT,
    match_score   REAL,
    mu_series_id  INTEGER
);

CREATE TABLE IF NOT EXISTS series_details (
    mu_series_id  INTEGER PRIMARY KEY,
    mu_title      TEXT,
    mu_type       TEXT,
    mu_year       TEXT,
    genres        TEXT,
    authors       TEXT,
    public_rating REAL,
    rating_votes  INTEGER
);
"""

ITEM_COLUMNS = [
    "notion_id", "title", "rating", "rating_score", "status",
    "chapters", "match_status", "match_score", "mu_series_id",
]
DETAIL_COLUMNS = [
    "mu_series_id", "mu_title", "mu_type", "mu_year",
    "genres", "authors", "public_rating", "rating_votes",
]


def to_records(df: pd.DataFrame, columns: list[str]) -> list[tuple]:
    """Turn a DataFrame into plain tuples, with None instead of NaN."""
    part = df[columns]
    clean = part.astype(object).where(part.notna(), None)
    return list(clean.itertuples(index=False, name=None))


def main() -> None:
    df = pd.read_csv(ENRICHED_CSV)
    df = df[~df["match_status"].isin(EXCLUDED)].copy()

    for col in ("mu_series_id", "rating_votes", "rating_score"):
        df[col] = df[col].astype("Int64")

    # Only keep the MangaUpdates id for matches we trust
    df.loc[~df["match_status"].isin(ACCEPTED), "mu_series_id"] = pd.NA

    items = to_records(df, ITEM_COLUMNS)

    details_df = (
        df[df["match_status"].isin(ACCEPTED)]
        .drop_duplicates("mu_series_id")
    )
    details = to_records(details_df, DETAIL_COLUMNS)

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)

    with conn:  # one transaction: all or nothing
        conn.execute("DELETE FROM items")
        conn.execute("DELETE FROM series_details")
        conn.executemany(
            f"INSERT INTO items VALUES ({','.join('?' * len(ITEM_COLUMNS))})", items
        )
        conn.executemany(
            f"INSERT INTO series_details VALUES ({','.join('?' * len(DETAIL_COLUMNS))})",
            details,
        )

    n_items = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    n_details = conn.execute("SELECT COUNT(*) FROM series_details").fetchone()[0]
    conn.close()

    print(f"Database saved to: {DB_PATH}")
    print(f"items:          {n_items} rows")
    print(f"series_details: {n_details} rows")


if __name__ == "__main__":
    main()
