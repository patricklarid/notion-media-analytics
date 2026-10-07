"""Enrich the Notion light novel list with MangaUpdates data.

Pipeline for each title:
1. Validate the Notion row (title, status, rating, duplicates)
2. Search MangaUpdates (responses are cached, so reruns are fast)
3. Classify the match: NOVEL_OK / NOVEL_REVIEW / ADAPTATION_ONLY / NO_MATCH
4. For safe matches, fetch the full profile (authors, genres, rating, year)
5. Write the enriched table, a review log and a quality report

Inputs : data/raw/notion_light_novels.csv
         data/overrides.csv (optional: columns notion_title, series_id)
Outputs: data/raw/enriched_light_novels.csv   (private, git-ignored)
         data/clean/review_log.csv            (titles needing a human look)
         data/clean/quality_report.txt        (match rates)

The Notion comments column is never read.
"""

import hashlib
import json
import re
import time
from pathlib import Path

import pandas as pd
import requests
from rapidfuzz import fuzz

# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
RAW_CSV = ROOT / "data" / "raw" / "notion_light_novels.csv"
CACHE_DIR = ROOT / "data" / "raw" / "cache" / "mangaupdates"
OVERRIDES_CSV = ROOT / "data" / "overrides.csv"
ENRICHED_CSV = ROOT / "data" / "raw" / "enriched_light_novels.csv"
REVIEW_LOG = ROOT / "data" / "clean" / "review_log.csv"
QUALITY_REPORT = ROOT / "data" / "clean" / "quality_report.txt"

API_BASE = "https://api.mangaupdates.com/v1"
PAUSE_SECONDS = 1.0  # polite delay between real (non-cached) requests

AUTO_THRESHOLD = 90    # score >= 90 -> accept automatically
REVIEW_THRESHOLD = 70  # 70-89 -> send to the review log

RATING_SCORES = {"TRASH": 1, "MID": 2, "TOP TIER": 3, "GOD TIER": 4}
VALID_STATUSES = {"Done", "Dropped", "Forgot about it", "In progress"}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def normalise(title: str) -> str:
    """Lowercase; drop '(novel)', 'LN', punctuation and extra spaces."""
    t = str(title).lower()
    t = re.sub(r"\((light )?novel\)", " ", t)
    t = re.sub(r"\b(ln|light novel|web novel)\b", " ", t)
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "")


def cached_request(cache_path: Path, request_fn):
    """Return cached JSON if we have it; otherwise call the API and cache it."""
    if cache_path.exists():
        return json.loads(cache_path.read_text(encoding="utf-8"))
    data = request_fn()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    time.sleep(PAUSE_SECONDS)
    return data


# --------------------------------------------------------------------------
# API calls
# --------------------------------------------------------------------------
def search_series(title: str) -> list[dict]:
    """Search by title; ask for Novel type first, then fall back to any type."""

    def _search():
        for payload in ({"search": title, "type": "Novel"}, {"search": title}):
            try:
                r = requests.post(f"{API_BASE}/series/search", json=payload, timeout=20)
                r.raise_for_status()
                return r.json().get("results", [])
            except requests.RequestException:
                continue
        raise RuntimeError("search failed")

    key = hashlib.sha1(title.encode("utf-8")).hexdigest()
    return cached_request(CACHE_DIR / "search" / f"{key}.json", _search)


def fetch_series(series_id: int) -> dict:
    def _fetch():
        r = requests.get(f"{API_BASE}/series/{series_id}", timeout=20)
        r.raise_for_status()
        return r.json()

    return cached_request(CACHE_DIR / "series" / f"{series_id}.json", _fetch)


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------
def best_candidate(title: str, results: list[dict], only_novels: bool):
    """Closest result by strict similarity. Returns (record, score)."""
    wanted = normalise(title)
    best, best_score = None, 0.0
    for item in results:
        record = item.get("record", {})
        if only_novels and record.get("type") != "Novel":
            continue
        names = [record.get("title", ""), strip_tags(item.get("hit_title", ""))]
        names = [n for n in names if n]
        if not names:
            continue
        score = max(fuzz.ratio(wanted, normalise(n)) for n in names)
        if score > best_score:
            best, best_score = record, score
    return best, best_score


def classify(title: str, results: list[dict]):
    """Return (match_status, record, score)."""
    novel, novel_score = best_candidate(title, results, only_novels=True)
    if novel and novel_score >= AUTO_THRESHOLD:
        return "NOVEL_OK", novel, novel_score
    if novel and novel_score >= REVIEW_THRESHOLD:
        return "NOVEL_REVIEW", novel, novel_score

    adapt, adapt_score = best_candidate(title, results, only_novels=False)
    if adapt and adapt_score >= AUTO_THRESHOLD:
        return "ADAPTATION_ONLY", adapt, adapt_score
    return "NO_MATCH", None, 0.0


# --------------------------------------------------------------------------
# Validation of the Notion row itself
# --------------------------------------------------------------------------
def validate_row(row: pd.Series) -> list[str]:
    problems = []
    if pd.isna(row["title"]) or not str(row["title"]).strip():
        problems.append("missing title")
    if row["status"] not in VALID_STATUSES:
        problems.append(f"unexpected status: {row['status']}")
    if row["rating"] not in RATING_SCORES:
        problems.append(f"unexpected rating: {row['rating']}")
    return problems


# --------------------------------------------------------------------------
# Turn an API profile into flat fields
# --------------------------------------------------------------------------
def parse_profile(detail: dict, trusted: bool) -> dict:
    """trusted=True for real novel matches; False for adaptation-only matches.

    For adaptations we keep genres only. A manhua's rating or authors are
    NOT the novel's, so copying them would be misleading.
    """
    genres = [g["genre"] for g in detail.get("genres", []) if g.get("genre")]
    out = {
        "mu_year": detail.get("year"),
        "genres": "; ".join(genres) or None,
        "authors": None,
        "public_rating": None,
        "rating_votes": None,
    }
    if trusted:
        authors = [a["name"] for a in detail.get("authors", []) if a.get("name")]
        rating = detail.get("bayesian_rating")
        out["authors"] = "; ".join(authors) or None
        out["public_rating"] = rating if rating not in (None, 0) else None
        out["rating_votes"] = detail.get("rating_votes")
    return out


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def load_overrides() -> dict[str, int]:
    if not OVERRIDES_CSV.exists():
        return {}
    df = pd.read_csv(OVERRIDES_CSV)
    return {normalise(t): int(i) for t, i in zip(df["notion_title"], df["series_id"])}


def main() -> None:
    df = pd.read_csv(RAW_CSV)
    overrides = load_overrides()
    seen_titles: set[str] = set()
    enriched_rows, review_rows = [], []

    for n, (_, row) in enumerate(df.iterrows(), start=1):
        title = row["title"]
        out = {
            "notion_id": row["notion_id"],
            "title": title,
            "rating": row["rating"],
            "rating_score": RATING_SCORES.get(row["rating"]),
            "status": row["status"],
            "chapters": row["chapters"],
            "match_status": None,
            "match_score": None,
            "mu_series_id": None,
            "mu_title": None,
            "mu_type": None,
            "mu_year": None,
            "genres": None,
            "authors": None,
            "public_rating": None,
            "rating_votes": None,
        }

        def log(reason: str, candidate: str = "", score=None):
            review_rows.append(
                {"title": title, "reason": reason, "best_candidate": candidate, "score": score}
            )

        # 1. validate the row
        problems = validate_row(row)
        for p in problems:
            log(p)
        if "missing title" in problems:
            out["match_status"] = "INVALID"
            enriched_rows.append(out)
            continue

        # 2. duplicates
        key = normalise(title)
        if key in seen_titles:
            out["match_status"] = "DUPLICATE"
            log("duplicate title")
            enriched_rows.append(out)
            continue
        seen_titles.add(key)

        # 3. manual override wins over everything
        if key in overrides:
            detail = fetch_series(overrides[key])
            out.update(
                match_status="MANUAL",
                match_score=100.0,
                mu_series_id=overrides[key],
                mu_title=detail.get("title"),
                mu_type=detail.get("type"),
            )
            out.update(parse_profile(detail, trusted=detail.get("type") == "Novel"))
            enriched_rows.append(out)
            continue

        # 4. search and classify
        try:
            results = search_series(title)
        except RuntimeError:
            out["match_status"] = "ERROR"
            log("API search failed (rerun the script)")
            enriched_rows.append(out)
            continue

        status, record, score = classify(title, results)
        out["match_status"] = status
        out["match_score"] = round(score, 1) if record else None

        if record:
            out["mu_series_id"] = record.get("series_id")
            out["mu_title"] = record.get("title")
            out["mu_type"] = record.get("type")

        # 5. enrich only safe matches
        if status in ("NOVEL_OK", "ADAPTATION_ONLY"):
            try:
                detail = fetch_series(record["series_id"])
                out.update(parse_profile(detail, trusted=status == "NOVEL_OK"))
            except requests.RequestException:
                log("profile fetch failed (rerun the script)", record.get("title", ""), score)
            if status == "ADAPTATION_ONLY":
                log("only a manhua/manga adaptation found", record.get("title", ""), score)
        elif status == "NOVEL_REVIEW":
            log("similar title, needs a human check", record.get("title", ""), score)
        else:
            log("no match found")

        enriched_rows.append(out)
        if n % 25 == 0:
            print(f"  processed {n}/{len(df)} titles...")

    # ----------------------------------------------------------------------
    # Save outputs
    # ----------------------------------------------------------------------
    enriched = pd.DataFrame(enriched_rows)
    for path in (ENRICHED_CSV, REVIEW_LOG, QUALITY_REPORT):
        path.parent.mkdir(parents=True, exist_ok=True)
    enriched.to_csv(ENRICHED_CSV, index=False, encoding="utf-8")
    pd.DataFrame(review_rows).to_csv(REVIEW_LOG, index=False, encoding="utf-8")

    total = len(enriched)
    counts = enriched["match_status"].value_counts()
    lines = [f"Total rows: {total}", "", "Match status:"]
    for status, count in counts.items():
        lines.append(f"  {status:16} {count:4}  ({count / total:.0%})")
    with_rating = int(enriched["public_rating"].notna().sum())
    with_genres = int(enriched["genres"].notna().sum())
    lines += [
        "",
        f"Rows with genres:        {with_genres} ({with_genres / total:.0%})",
        f"Rows with public rating: {with_rating} ({with_rating / total:.0%})",
        f"Rows in review log:      {len(review_rows)}",
    ]
    report = "\n".join(lines)
    QUALITY_REPORT.write_text(report, encoding="utf-8")
    print("\n" + report)


if __name__ == "__main__":
    main()
