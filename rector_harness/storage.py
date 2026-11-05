from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .events import HarnessEvent
from .model import AgentResult, HarnessState, Review


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    objective TEXT NOT NULL,
    status TEXT NOT NULL,
    state_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS turns (
    run_id TEXT NOT NULL,
    turn INTEGER NOT NULL,
    cycle INTEGER NOT NULL,
    role TEXT NOT NULL,
    status TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (run_id, turn, role)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    turn INTEGER NOT NULL,
    role TEXT,
    message TEXT NOT NULL DEFAULT '',
    data_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS agent_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    turn INTEGER NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    raw_artifact TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    turn INTEGER NOT NULL,
    verdict TEXT NOT NULL,
    severity TEXT NOT NULL,
    findings_json TEXT NOT NULL,
    confidence REAL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS human_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    turn INTEGER NOT NULL,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


class SQLiteStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def save_state(self, state: HarnessState) -> None:
        if not state.run_id:
            raise ValueError("state.run_id is required before saving to SQLite")
        payload = state.model_dump_json()
        self.connection.execute(
            """INSERT INTO runs(run_id, objective, status, state_json) VALUES (?, ?, ?, ?)
               ON CONFLICT(run_id) DO UPDATE SET objective=excluded.objective,
               status=excluded.status, state_json=excluded.state_json, updated_at=CURRENT_TIMESTAMP""",
            (state.run_id, state.objective, state.status, payload),
        )
        self.connection.commit()

    def record_event(self, event: HarnessEvent) -> None:
        self.connection.execute(
            "INSERT INTO events(run_id, at, kind, turn, role, message, data_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event.run_id, event.at, event.kind, event.turn, event.role, event.message, json.dumps(event.data, ensure_ascii=False)),
        )
        self.connection.commit()

    def record_agent(self, state: HarnessState, role: str, result: AgentResult, artifact: str | None = None) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO turns(run_id, turn, cycle, role, status, summary) VALUES (?, ?, ?, ?, ?, ?)",
            (state.run_id, state.turn, state.cycle, role, "completed" if not result.error else "failed", result.text),
        )
        self.connection.execute(
            "INSERT INTO agent_messages(run_id, turn, role, text, raw_artifact) VALUES (?, ?, ?, ?, ?)",
            (state.run_id, state.turn, role, result.text, artifact),
        )
        self.connection.commit()

    def record_review(self, state: HarnessState, review: Review) -> None:
        self.connection.execute(
            "INSERT INTO reviews(run_id, turn, verdict, severity, findings_json, confidence) VALUES (?, ?, ?, ?, ?, ?)",
            (state.run_id, state.turn, review.verdict, review.severity, json.dumps(review.findings, ensure_ascii=False), review.confidence),
        )
        self.connection.commit()

    def record_decision(self, state: HarnessState, question: str, answer: str) -> None:
        self.connection.execute(
            "INSERT INTO human_decisions(run_id, turn, question, answer) VALUES (?, ?, ?, ?)",
            (state.run_id, state.turn, question, answer),
        )
        self.connection.commit()

    def recent_events(self, limit: int = 100) -> list[sqlite3.Row]:
        return list(self.connection.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)))
