"""Tiny SQLite persistence layer for meetings.

For the POC we keep this dead simple: one table, the path comes from DB_PATH
(default /tmp/rooms.db, which is the writable directory on Vercel). We store a
compact "log" of each meeting (the meaningful events, minus the token spam) plus
the final facilitator decision so finished meetings can be replayed from the
sidebar.

Caveat for serverless: /tmp lives on the function instance, so history survives
as long as that instance stays warm but is lost on a cold start. Set DB_PATH to
a durable database (e.g. a mounted volume or a libSQL/Turso URL via a small
adapter) for permanent history.
"""

import os
import json
import time
import sqlite3
import threading

DB_PATH = os.environ.get("DB_PATH", "/tmp/rooms.db")
_lock = threading.Lock()


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init():
    with _lock, _conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS meetings(
                id       TEXT PRIMARY KEY,
                topic    TEXT,
                details  TEXT,
                total    INTEGER,
                model    TEXT,
                status   TEXT,
                created  REAL,
                log      TEXT,
                decision TEXT
            )"""
        )


def create(m):
    with _lock, _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO meetings"
            "(id,topic,details,total,model,status,created,log,decision)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (m.id, m.topic, m.details, m.total, m.model, "pending",
             m.created, "[]", ""),
        )


def save(meeting_id, log, status, decision):
    with _lock, _conn() as c:
        c.execute(
            "UPDATE meetings SET log=?, status=?, decision=? WHERE id=?",
            (json.dumps(log, ensure_ascii=False), status, decision, meeting_id),
        )


def get(meeting_id):
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT * FROM meetings WHERE id=?", (meeting_id,)
        ).fetchone()
    return dict(row) if row else None


def list_all(limit=100):
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT id,topic,status,created,log FROM meetings"
            " ORDER BY created DESC LIMIT ?",
            (limit,),
        ).fetchall()
    out = []
    for r in rows:
        log = json.loads(r["log"] or "[]")
        turns = sum(1 for e in log if e["event"] == "turn_end")
        last = ""
        for e in log:
            if e["event"] == "turn_end":
                last = (e["data"].get("text") or "")[:60]
        out.append({
            "id": r["id"], "topic": r["topic"], "status": r["status"],
            "created": r["created"], "rounds_done": turns, "last": last,
        })
    return out


# Create the table as soon as the module is imported.
init()
