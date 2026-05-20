"""
local_buffer.py
================
SQLite buffer — saves data locally when server is offline.
Auto-flushes to server when connection restored.
"""

import sqlite3
import json
import os
import sys
from datetime import datetime, timezone


def _get_db_path() -> str:
    """Resolve buffer.db path correctly for both compiled .exe and dev mode.
    Compiled onefile exe: __file__ points to temp extraction dir — unusable.
    Use ProgramData instead so the DB survives reboots and upgrades."""
    is_exe = os.path.splitext(sys.executable)[1].lower() == ".exe"
    if is_exe:
        data_dir = os.path.join(
            os.environ.get("PROGRAMDATA", "C:\\ProgramData"), "FanucIoTEdge"
        )
        os.makedirs(data_dir, exist_ok=True)
        return os.path.join(data_dir, "buffer.db")
    else:
        base = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(base, "buffer.db")


DB_PATH = _get_db_path()


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.execute("""
        CREATE TABLE IF NOT EXISTS buffer (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            machine   TEXT,
            ts        TEXT,
            payload   TEXT,
            sent      INTEGER DEFAULT 0
        )
    """)
    c.commit()
    return c


def save(machine_id: str, payload: dict):
    """Save one record to local buffer."""
    c = _conn()
    c.execute(
        "INSERT INTO buffer (machine, ts, payload) VALUES (?, ?, ?)",
        (machine_id, datetime.now(timezone.utc).isoformat(), json.dumps(payload))
    )
    c.commit()
    c.close()


def get_unsent(machine_id: str = None, limit: int = 100) -> list:
    """Return unsent records, optionally filtered by machine."""
    c = _conn()
    if machine_id:
        rows = c.execute(
            "SELECT id, payload FROM buffer WHERE sent=0 AND machine=? ORDER BY id LIMIT ?",
            (machine_id, limit)
        ).fetchall()
    else:
        rows = c.execute(
            "SELECT id, payload FROM buffer WHERE sent=0 ORDER BY id LIMIT ?",
            (limit,)
        ).fetchall()
    c.close()
    return [(row[0], json.loads(row[1])) for row in rows]


def mark_sent(ids: list):
    """Mark records as sent."""
    if not ids:
        return
    c = _conn()
    c.execute(
        f"UPDATE buffer SET sent=1 WHERE id IN ({','.join('?'*len(ids))})",
        ids
    )
    c.commit()
    c.close()


def pending_count(machine_id: str = None) -> int:
    c = _conn()
    if machine_id:
        count = c.execute(
            "SELECT COUNT(*) FROM buffer WHERE sent=0 AND machine=?", (machine_id,)
        ).fetchone()[0]
    else:
        count = c.execute(
            "SELECT COUNT(*) FROM buffer WHERE sent=0"
        ).fetchone()[0]
    c.close()
    return count


def cleanup(keep_days: int = 3):
    """Delete old sent records."""
    c = _conn()
    c.execute(
        "DELETE FROM buffer WHERE sent=1 AND ts < datetime('now', ?)",
        (f"-{keep_days} days",)
    )
    c.commit()
    c.close()
