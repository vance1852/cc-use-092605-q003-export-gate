"""承诺门禁服务的 SQLite 模式和事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS gate_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('dispatcher','station','coordinator','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS delivery_boundaries (
    boundary_id TEXT PRIMARY KEY,
    corridor_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version > 0),
    effective_from TEXT NOT NULL,
    effective_to TEXT NOT NULL,
    channel_capacity_mw TEXT NOT NULL,
    reactive_compensation_mvar TEXT NOT NULL,
    cable_thermal_limit_mw TEXT NOT NULL,
    maintenance_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'published' CHECK(state IN ('published','superseded')),
    published_by TEXT NOT NULL REFERENCES gate_users(user_id),
    published_at TEXT NOT NULL,
    UNIQUE(corridor_id, version)
);

CREATE INDEX IF NOT EXISTS idx_boundaries_corridor
ON delivery_boundaries(corridor_id, state, version);

CREATE TABLE IF NOT EXISTS station_declarations (
    declaration_id TEXT PRIMARY KEY,
    corridor_id TEXT NOT NULL,
    station_id TEXT NOT NULL,
    installed_capacity_mw TEXT NOT NULL,
    availability_percent TEXT NOT NULL,
    ramp_curve_json TEXT NOT NULL,
    reserve_mw TEXT NOT NULL,
    latest_response_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'declared' CHECK(state IN ('declared','superseded')),
    declared_by TEXT NOT NULL REFERENCES gate_users(user_id),
    declared_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_declarations_station
ON station_declarations(corridor_id, station_id, state, declared_at);

CREATE TABLE IF NOT EXISTS commitment_plans (
    plan_id TEXT PRIMARY KEY,
    corridor_id TEXT NOT NULL,
    station_id TEXT NOT NULL,
    boundary_id TEXT NOT NULL REFERENCES delivery_boundaries(boundary_id),
    declaration_id TEXT NOT NULL REFERENCES station_declarations(declaration_id),
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    requested_mw TEXT NOT NULL,
    committed_mw TEXT NOT NULL,
    planned_mwh TEXT NOT NULL,
    phases_json TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('approved','derated','exemption_required')),
    decision_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending_confirmation'
        CHECK(state IN ('pending_confirmation','confirmed','executing','settled','cancelled')),
    exemption_id TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES gate_users(user_id),
    created_at TEXT NOT NULL,
    confirmed_at TEXT,
    settled_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_plans_corridor_state
ON commitment_plans(corridor_id, state, window_start);

CREATE TABLE IF NOT EXISTS emergency_exemptions (
    exemption_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES commitment_plans(plan_id),
    authorized_by TEXT NOT NULL REFERENCES gate_users(user_id),
    reason TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_exemptions_plan
ON emergency_exemptions(plan_id, created_at);

CREATE TABLE IF NOT EXISTS capacity_locks (
    lock_id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL UNIQUE REFERENCES commitment_plans(plan_id),
    corridor_id TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    locked_channel_mw TEXT NOT NULL,
    locked_compensation_mvar TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'held' CHECK(state IN ('held','released')),
    created_at TEXT NOT NULL,
    released_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_locks_corridor_window
ON capacity_locks(corridor_id, state, window_start, window_end);

CREATE TABLE IF NOT EXISTS execution_receipts (
    receipt_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL UNIQUE REFERENCES commitment_plans(plan_id),
    actual_mwh TEXT NOT NULL,
    planned_mwh TEXT NOT NULL,
    variance_mwh TEXT NOT NULL,
    received_by TEXT NOT NULL REFERENCES gate_users(user_id),
    received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gate_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_gate_audit_entity
ON gate_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    # check_same_thread=False：ThreadingHTTPServer 在 worker 线程中使用连接，
    # 并发请求由 JsonApplication 的锁串行化。
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
