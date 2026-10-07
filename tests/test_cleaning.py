"""Tests for the cleaning, validation and matching logic.

These tests never call the internet: they use small fake API results.
"""

import pandas as pd

from enrich_mangaupdates import classify, normalise, parse_profile, validate_row


def fake_result(title: str, kind: str) -> dict:
    """A fake MangaUpdates search result."""
    return {"hit_title": title, "record": {"title": title, "type": kind, "series_id": 1}}


# --- normalise ------------------------------------------------------------
def test_normalise_removes_noise():
    assert normalise("Dungeon Defense LN") == "dungeon defense"
    assert normalise("Talisman Emperor (Novel)") == "talisman emperor"
    assert normalise("  I Shall   Seal the Heavens!! ") == "i shall seal the heavens"


# --- validate_row ---------------------------------------------------------
def test_validate_accepts_good_row():
    row = pd.Series({"title": "Some Novel", "status": "Done", "rating": "MID"})
    assert validate_row(row) == []


def test_validate_rejects_bad_rating_and_status():
    row = pd.Series({"title": "Some Novel", "status": "Reading", "rating": "9"})
    problems = validate_row(row)
    assert len(problems) == 2


def test_validate_rejects_missing_title():
    row = pd.Series({"title": None, "status": "Done", "rating": "MID"})
    assert "missing title" in validate_row(row)


# --- classify (the matching rules) ---------------------------------------
def test_exact_novel_is_auto_matched():
    results = [fake_result("Talisman Emperor (Novel)", "Novel")]
    status, record, score = classify("Talisman emperor", results)
    assert status == "NOVEL_OK"
    assert score == 100


def test_partial_title_is_not_matched():
    # Real bug found in v1: "Power and Wealth" matched the 1994 manga "Power"
    results = [fake_result("Power", "Manga")]
    status, record, score = classify("Power and Wealth", results)
    assert status == "NO_MATCH"


def test_novel_is_preferred_over_manhua():
    results = [
        fake_result("Tales of Herding Gods", "Manhua"),
        fake_result("Tales of Herding Gods (Novel)", "Novel"),
    ]
    status, record, score = classify("Tales of Herding Gods", results)
    assert status == "NOVEL_OK"
    assert record["type"] == "Novel"


def test_manhua_only_is_flagged_as_adaptation():
    results = [fake_result("Strongest System", "Manhua")]
    status, record, score = classify("Strongest system", results)
    assert status == "ADAPTATION_ONLY"


def test_similar_title_goes_to_review():
    results = [fake_result("Dungeon Defense (Novel)", "Novel")]
    status, record, score = classify("Dantalian- Dungeon Defense LN", results)
    assert status == "NOVEL_REVIEW"


# --- parse_profile --------------------------------------------------------
PROFILE = {
    "year": "2019",
    "genres": [{"genre": "Action"}],
    "authors": [{"name": "Some Author"}],
    "bayesian_rating": 6.5,
    "rating_votes": 100,
}


def test_trusted_profile_keeps_rating_and_authors():
    out = parse_profile(PROFILE, trusted=True)
    assert out["public_rating"] == 6.5
    assert out["authors"] == "Some Author"


def test_adaptation_profile_drops_rating_and_authors():
    out = parse_profile(PROFILE, trusted=False)
    assert out["genres"] == "Action"
    assert out["public_rating"] is None
    assert out["authors"] is None


def test_zero_rating_is_treated_as_missing():
    out = parse_profile({**PROFILE, "bayesian_rating": 0}, trusted=True)
    assert out["public_rating"] is None
