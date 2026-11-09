"""SQLite tracker. One row per job; status moves through:

found -> filtered_out | scored -> shortlisted | rejected_by_score
      -> in_progress -> applied -> (interview | rejected | offer | ghosted)
      -> skipped
"""
from __future__ import annotations

import csv
import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,          -- "<ats>:<company>:<external id>"
    source        TEXT NOT NULL,             -- company | linkedin | naukri | other
    ats           TEXT,
    company       TEXT,
    title         TEXT,
    location      TEXT,
    url           TEXT,
    apply_url     TEXT,
    description   TEXT,
    posted_at     TEXT,
    found_at      TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'found',
    score         INTEGER,
    verdict       TEXT,
    reasons       TEXT,
    variant       TEXT,
    applied_at    TEXT,
    notes         TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE TABLE IF NOT EXISTS events (
    job_id  TEXT,
    at      TEXT,
    kind    TEXT,
    detail  TEXT
);
"""

VALID_STATUSES = {
    "found", "filtered_out", "scored", "shortlisted", "rejected_by_score",
    "in_progress", "applied", "skipped", "interview", "rejected", "offer", "ghosted",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def upsert_job(conn, job: dict) -> bool:
    """Insert a new job. Returns True if it was new."""
    cur = conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job["id"],))
    if cur.fetchone():
        return False
    cols = ["id", "source", "ats", "company", "title", "location", "url",
            "apply_url", "description", "posted_at", "found_at", "status"]
    row = {c: job.get(c) for c in cols}
    row["found_at"] = row["found_at"] or now()
    row["status"] = row["status"] or "found"
    conn.execute(
        f"INSERT INTO jobs ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
        [row[c] for c in cols],
    )
    log(conn, job["id"], "found", job.get("url", ""))
    return True


def update(conn, job_id: str, **fields):
    if "status" in fields and fields["status"] not in VALID_STATUSES:
        raise ValueError(f"unknown status {fields['status']}")
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE jobs SET {sets} WHERE id = ?", [*fields.values(), job_id])
    if "status" in fields:
        log(conn, job_id, "status", fields["status"])
    conn.commit()


def log(conn, job_id, kind, detail=""):
    if not isinstance(detail, str):
        detail = json.dumps(detail)
    conn.execute("INSERT INTO events VALUES (?,?,?,?)", (job_id, now(), kind, detail))


def get(conn, job_id: str):
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:  # allow unique prefix / short id
        rows = conn.execute("SELECT * FROM jobs WHERE id LIKE ?", (f"%{job_id}%",)).fetchall()
        if len(rows) == 1:
            row = rows[0]
    return row


def by_status(conn, *statuses, order="score DESC, found_at DESC", limit=None):
    q = f"SELECT * FROM jobs WHERE status IN ({','.join('?' * len(statuses))}) ORDER BY {order}"
    if limit:
        q += f" LIMIT {int(limit)}"
    return conn.execute(q, statuses).fetchall()


def applied_today(conn, source: str) -> int:
    today = date.today().isoformat()
    return conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE status='applied' AND source=? AND substr(applied_at,1,10)=?",
        (source, today),
    ).fetchone()[0]


def export_csv(conn, path: Path):
    rows = conn.execute(
        "SELECT id, status, score, company, title, location, source, ats, variant, "
        "url, posted_at, found_at, applied_at, verdict, notes FROM jobs ORDER BY found_at DESC"
    ).fetchall()
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(rows[0].keys() if rows else [])
        for r in rows:
            w.writerow(list(r))
    return len(rows)
