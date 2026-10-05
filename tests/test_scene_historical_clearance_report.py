"""Static + in-memory dynamic checks for the REPORT-ONLY historical clearance manifest.

The query targets production Postgres (Supabase) but is intentionally written in a
dialect subset that also runs on SQLite, so we validate it end-to-end here against
a minimal in-memory fixture of the three real tables it reads. No production data,
no network, no writes outside the test's own throwaway connection.
"""
import sqlite3
from pathlib import Path

import pytest

SQL_PATH = (
    Path(__file__).resolve().parent.parent
    / "docs" / "queries" / "scene_historical_clearance_report.sql"
)

SCHEMA = """
CREATE TABLE media_source (
  id TEXT PRIMARY KEY,
  gym_id TEXT NOT NULL,
  kind TEXT NOT NULL
);
CREATE TABLE media_asset (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,
  gym_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  review_status TEXT NOT NULL,
  content_hash TEXT,
  rendition_url TEXT,
  used_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE content_calendar (
  id TEXT PRIMARY KEY,
  gym_id TEXT NOT NULL,
  account TEXT,
  status TEXT NOT NULL,
  published_at TEXT,
  source_media_asset_id TEXT,
  source_media_url TEXT,
  image_url TEXT
);
"""


@pytest.fixture()
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    yield conn
    conn.close()


def seed(conn, sources=(), assets=(), posts=()):
    conn.executemany(
        "INSERT INTO media_source (id, gym_id, kind) VALUES (?, ?, 'gym_drive')",
        sources,
    )
    conn.executemany(
        "INSERT INTO media_asset (id, source_id, gym_id, kind, review_status,"
        " content_hash, rendition_url, used_count)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        assets,
    )
    conn.executemany(
        "INSERT INTO content_calendar (id, gym_id, account, status, published_at,"
        " source_media_asset_id, source_media_url, image_url)"
        " VALUES (?, ?, 'ig', ?, ?, ?, ?, NULL)",
        posts,
    )
    conn.commit()


def execute_all(conn):
    statements = []
    sql = SQL_PATH.read_text()
    buf = []
    for line in sql.splitlines():
        buf.append(line)
        if line.rstrip().endswith(";"):
            text = "\n".join(buf).strip()
            # strip leading comment lines for execution
            body = "\n".join(
                l for l in text.splitlines() if not l.strip().startswith("--")
            ).strip()
            if body:
                statements.append(body)
            buf = []
    results = [list(conn.execute(s)) for s in statements]
    assert len(statements) == 2, f"expected 2 statements, got {len(statements)}"
    return results



def _comment_stripped():
    """SQL with -- comment lines removed (static guards check code, not prose)."""
    lines = []
    for line in SQL_PATH.read_text().splitlines():
        # strip whole-line and trailing inline -- comments (no string literal
        # in this query contains "--", so a plain split is safe)
        lines.append(line.split("--", 1)[0])
    return "\n".join(lines)

# ---- static guards -----------------------------------------------------------

def test_file_exists_and_is_read_only():
    sql = _comment_stripped().lower()
    for forbidden in (
        "insert", "update", "delete", "drop", "alter", "create table",
        "truncate", "grant", "copy ", "pg_read_file", "lo_get",
    ):
        assert forbidden not in sql, f"forbidden write/DDL token: {forbidden}"
    assert "select" in sql


def test_only_real_tables_and_columns_referenced():
    sql = _comment_stripped()
    for table in ("media_asset", "media_source", "content_calendar"):
        assert table in sql
    for col in (
        "id", "source_id", "gym_id", "kind", "review_status", "content_hash",
        "rendition_url", "used_count", "status", "published_at",
        "source_media_asset_id", "source_media_url",
    ):
        assert col in sql, f"expected column referenced: {col}"
    # pHash is review-only and not in this schema — must not be invented
    assert "phash" not in sql.lower()


def test_status_values_and_never_cleared():
    sql = _comment_stripped()
    assert "KNOWN_USED_OR_HOLD" in sql
    assert "UNKNOWN_HELD" in sql
    body = sql.lower()
    assert "'cleared'" not in body and '"cleared"' not in body


# ---- dynamic behavior on the in-memory fixture -------------------------------

def test_one_row_per_approved_photo_no_duplicates(db):
    seed(
        db,
        sources=[("s1", "gym-a")],
        assets=[
            ("a1", "s1", "gym-a", "photo", "approved", "h1", "https://r/1", 0),
            ("a2", "s1", "gym-a", "photo", "approved", "h2", "https://r/2", 0),
            ("v1", "s1", "gym-a", "video", "approved", "h3", "https://r/3", 0),
            ("p1", "s1", "gym-a", "photo", "pending_review", "h4", None, 0),
        ],
        posts=[
            ("c1", "gym-a", "published", None, "a1", None),
            ("c2", "gym-a", "published", None, "a1", None),
            ("c3", "gym-a", "published", None, None, "https://r/2"),
        ],
    )
    rows = execute_all(db)[0]
    assert sorted(r["asset_id"] for r in rows) == ["a1", "a2"]
    by_id = {r["asset_id"]: r for r in rows}
    assert by_id["a1"]["same_tenant_published_asset_id_matches"] == 2
    assert by_id["a2"]["same_tenant_published_source_url_matches"] == 1
    assert by_id["a2"]["used_count_positive"] == 0


def test_status_rules_known_used_hold_and_unknown_held(db):
    seed(
        db,
        sources=[("s1", "gym-a"), ("s2", "gym-b")],
        assets=[
            ("a1", "s1", "gym-a", "photo", "approved", "h1", "https://r/1", 0),
            ("a2", "s2", "gym-b", "photo", "approved", None, None, 0),
            ("a3", "s1", "gym-a", "photo", "approved", "h3", "https://r/3", 5),
            ("a4", "s1", "gym-a", "photo", "approved", "h4", "https://r/4", 0),
        ],
        posts=[
            # same-tenant asset-id link -> a1 KNOWN_USED
            ("c1", "gym-a", "published", None, "a1", None),
            # cross-tenant link -> a1 would be hold too; use a4 for the hold case
            ("c2", "gym-x", "published", None, None, "https://r/4"),
        ],
    )
    rows = execute_all(db)[0]
    status = {r["asset_id"]: r["conservative_status"] for r in rows}
    assert status["a1"] == "KNOWN_USED_OR_HOLD"
    assert status["a3"] == "KNOWN_USED_OR_HOLD"  # used_count > 0 alone
    assert status["a4"] == "KNOWN_USED_OR_HOLD"  # cross-tenant match = hold
    assert status["a2"] == "UNKNOWN_HELD"        # nothing observable
    assert set(status.values()) <= {"KNOWN_USED_OR_HOLD", "UNKNOWN_HELD"}


def test_missing_history_cohort_counts(db):
    seed(
        db,
        sources=[("s1", "gym-a")],
        assets=[
            ("a1", "s1", "gym-a", "photo", "approved", None, None, 0),
            ("a2", "s1", "gym-a", "photo", "approved", "h2", None, 3),
        ],
        posts=[
            ("c1", "gym-a", "published", None, None, None),
            ("c2", "gym-a", "draft", None, "a2", None),
        ],
    )
    cohorts = execute_all(db)[1][0]
    assert cohorts["approved_photo_assets"] == 2
    assert cohorts["approved_photos_missing_content_hash"] == 1
    assert cohorts["approved_photos_source_ownership_mismatch"] == 0
    assert cohorts["approved_photos_used_count_zero"] == 1
    assert cohorts["published_or_dated_posts"] == 1
    assert cohorts["published_posts_missing_source_media_url"] == 1
    assert cohorts["published_posts_missing_source_media_asset_id"] == 1
    assert cohorts["approved_photos_no_observable_history"] == 1


def test_blank_urls_are_missing_and_never_exact_use_matches(db):
    seed(
        db,
        sources=[("s1", "gym-a")],
        assets=[("a1", "s1", "gym-a", "photo", "approved", "h1", "   ", 0)],
        posts=[("c1", "gym-a", "published", None, "   ", "   ")],
    )
    manifest, cohorts = execute_all(db)
    assert manifest[0]["same_tenant_published_source_url_matches"] == 0
    assert manifest[0]["conservative_status"] == "UNKNOWN_HELD"
    assert cohorts[0]["published_posts_missing_source_media_url"] == 1
    assert cohorts[0]["published_posts_missing_source_media_asset_id"] == 1
