"""SQLite storage for received events and their deliveries.

The idempotency guarantee lives here: `events.event_id` is UNIQUE, so a
provider that fires the same event twice (they all do) inserts once and the
second attempt is a no-op. That single constraint is what stops duplicate
Slack messages and duplicate spreadsheet rows.
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from app.config import BASE_BACKOFF_SECONDS, DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY,
    event_id        TEXT NOT NULL UNIQUE,
    source          TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    payload         TEXT NOT NULL,
    received_at     TEXT NOT NULL,
    -- How many times the provider sent this same event again. Worth keeping:
    -- it is the number that shows the dedupe key doing its job.
    duplicate_hits  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS deliveries (
    id              INTEGER PRIMARY KEY,
    event_id        TEXT NOT NULL,
    destination     TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    last_error      TEXT,
    delivered_at    TEXT,
    UNIQUE (event_id, destination)
);

CREATE INDEX IF NOT EXISTS idx_deliveries_due
    ON deliveries(status, next_attempt_at);
"""


def now():
    return datetime.now(timezone.utc)


def iso(moment):
    return moment.isoformat(timespec="seconds")


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init():
    with connect() as conn:
        conn.executescript(SCHEMA)

        # CREATE TABLE IF NOT EXISTS leaves an older database without newer
        # columns, so add them here rather than asking anyone to drop data.
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(events)").fetchall()
        }
        if "duplicate_hits" not in columns:
            conn.execute(
                "ALTER TABLE events ADD COLUMN duplicate_hits "
                "INTEGER NOT NULL DEFAULT 0"
            )


def record_event(conn, event_id, source, event_type, payload, destinations):
    """Insert an event and queue one delivery per destination.

    Returns True if this was a new event, False if it was a duplicate.
    """
    cur = conn.execute(
        "INSERT OR IGNORE INTO events (event_id, source, event_type, payload, "
        "received_at) VALUES (?, ?, ?, ?, ?)",
        (event_id, source, event_type, json.dumps(payload), iso(now())),
    )
    if cur.rowcount == 0:
        # Already seen. Count the repeat and do nothing else -- no second set
        # of deliveries, so no duplicate Slack message or spreadsheet row.
        conn.execute(
            "UPDATE events SET duplicate_hits = duplicate_hits + 1 "
            "WHERE event_id = ?",
            (event_id,),
        )
        return False

    for destination in destinations:
        conn.execute(
            "INSERT OR IGNORE INTO deliveries (event_id, destination, "
            "next_attempt_at) VALUES (?, ?, ?)",
            (event_id, destination, iso(now())),
        )
    return True


def due_deliveries(conn, limit=50):
    return conn.execute(
        """
        SELECT d.*, e.event_type, e.payload
        FROM deliveries d JOIN events e ON e.event_id = d.event_id
        WHERE d.status = 'pending' AND d.next_attempt_at <= ?
        ORDER BY d.id LIMIT ?
        """,
        (iso(now()), limit),
    ).fetchall()


def mark_delivered(conn, delivery_id):
    conn.execute(
        "UPDATE deliveries SET status = 'delivered', delivered_at = ?, "
        "attempts = attempts + 1, last_error = NULL WHERE id = ?",
        (iso(now()), delivery_id),
    )


def mark_failed(conn, delivery_id, attempts, max_attempts, error):
    """Back off and retry, or give up and park it as 'dead'."""
    attempts += 1
    if attempts >= max_attempts:
        conn.execute(
            "UPDATE deliveries SET status = 'dead', attempts = ?, "
            "last_error = ? WHERE id = ?",
            (attempts, error, delivery_id),
        )
        return "dead"

    delay = BASE_BACKOFF_SECONDS * (2 ** (attempts - 1))
    conn.execute(
        "UPDATE deliveries SET attempts = ?, last_error = ?, "
        "next_attempt_at = ? WHERE id = ?",
        (attempts, error, iso(now() + timedelta(seconds=delay)), delivery_id),
    )
    return "retry"


def requeue_dead(conn, event_id=None):
    """Put dead deliveries back in the queue. This is the 'fix the outage,
    then replay' button clients actually need."""
    if event_id:
        cur = conn.execute(
            "UPDATE deliveries SET status = 'pending', attempts = 0, "
            "next_attempt_at = ? WHERE status = 'dead' AND event_id = ?",
            (iso(now()), event_id),
        )
    else:
        cur = conn.execute(
            "UPDATE deliveries SET status = 'pending', attempts = 0, "
            "next_attempt_at = ? WHERE status = 'dead'",
            (iso(now()),),
        )
    return cur.rowcount


def counts(conn):
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM deliveries GROUP BY status"
    ).fetchall()
    return {row["status"]: row["n"] for row in rows}


def recent_events(conn, limit=50):
    return conn.execute(
        "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()


def events_with_delivery_state(conn, limit=50):
    """Events plus a rolled-up view of how their deliveries are going.

    One query with aggregates rather than a delivery lookup per event: the
    operations page should stay fast when the table has a month of traffic in
    it.
    """
    return conn.execute(
        """
        SELECT e.event_id, e.source, e.event_type, e.received_at,
               e.duplicate_hits,
               COUNT(d.id)                                        AS total,
               SUM(d.status = 'delivered')                        AS delivered,
               SUM(d.status = 'pending')                          AS pending,
               SUM(d.status = 'dead')                             AS dead,
               COALESCE(MAX(d.attempts), 0)                       AS attempts
        FROM events e LEFT JOIN deliveries d ON d.event_id = e.event_id
        GROUP BY e.event_id
        ORDER BY e.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def recent_deliveries(conn, limit=50):
    return conn.execute(
        """
        SELECT d.*, e.event_type
        FROM deliveries d JOIN events e ON e.event_id = d.event_id
        ORDER BY
            CASE d.status WHEN 'dead' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END,
            d.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def duplicate_count(conn):
    """Total repeat deliveries the unique key has absorbed."""
    row = conn.execute(
        "SELECT COALESCE(SUM(duplicate_hits), 0) AS n FROM events"
    ).fetchone()
    return row["n"]
