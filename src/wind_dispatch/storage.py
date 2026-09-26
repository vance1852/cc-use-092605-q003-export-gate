"""供应服务的 SQLite 模式和事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS supply_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('planner','dispatcher','station','risk','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_index_quotes (
    quote_id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_index TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    close_cny TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    supersedes_quote_id INTEGER REFERENCES market_index_quotes(quote_id),
    recorded_by TEXT NOT NULL REFERENCES supply_users(user_id),
    recorded_at TEXT NOT NULL,
    UNIQUE(market_index, trade_date, source_revision)
);

CREATE INDEX IF NOT EXISTS idx_quotes_series
ON market_index_quotes(market_index, trade_date, quote_id);

CREATE TABLE IF NOT EXISTS facilities (
    facility_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    timezone TEXT NOT NULL,
    capacity_mwh TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS routes (
    route_id TEXT PRIMARY KEY,
    origin_id TEXT NOT NULL REFERENCES facilities(facility_id),
    destination_id TEXT NOT NULL REFERENCES facilities(facility_id),
    product TEXT NOT NULL,
    daily_capacity TEXT NOT NULL,
    loss_basis_points INTEGER NOT NULL,
    transit_hours INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','suspended','retired')),
    created_at TEXT NOT NULL,
    CHECK(origin_id <> destination_id)
);

CREATE TABLE IF NOT EXISTS route_outages (
    outage_id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id TEXT NOT NULL REFERENCES routes(route_id),
    starts_at TEXT NOT NULL,
    ends_at TEXT,
    capacity_percent TEXT NOT NULL,
    reason TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'announced' CHECK(state IN ('announced','active','closed','cancelled')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_outages_route_time
ON route_outages(route_id, starts_at, ends_at);

CREATE TABLE IF NOT EXISTS inventory_lots (
    lot_id TEXT PRIMARY KEY,
    facility_id TEXT NOT NULL REFERENCES facilities(facility_id),
    product TEXT NOT NULL,
    grade TEXT NOT NULL,
    quantity_mwh TEXT NOT NULL,
    available_mwh TEXT NOT NULL,
    unit_cost_cny TEXT NOT NULL,
    received_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_inventory_available
ON inventory_lots(facility_id, product, received_at);

CREATE TABLE IF NOT EXISTS inventory_adjustments (
    adjustment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id TEXT NOT NULL REFERENCES inventory_lots(lot_id),
    delta_mwh TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    note TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nominations (
    nomination_id TEXT PRIMARY KEY,
    route_id TEXT NOT NULL REFERENCES routes(route_id),
    shipper_id TEXT NOT NULL,
    service_date TEXT NOT NULL,
    requested_mwh TEXT NOT NULL,
    allocated_mwh TEXT NOT NULL DEFAULT '0',
    delivered_mwh TEXT NOT NULL DEFAULT '0',
    priority INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'submitted'
        CHECK(state IN ('submitted','allocated','in_transit','delivered','cancelled')),
    revision INTEGER NOT NULL DEFAULT 1,
    idempotency_key TEXT NOT NULL UNIQUE,
    submitted_by TEXT NOT NULL REFERENCES supply_users(user_id),
    submitted_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_nominations_schedule
ON nominations(route_id, service_date, priority, submitted_at);

CREATE TABLE IF NOT EXISTS allocation_runs (
    allocation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id TEXT NOT NULL REFERENCES routes(route_id),
    service_date TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    available_capacity TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(route_id, service_date, input_sha256)
);

CREATE TABLE IF NOT EXISTS transfers (
    transfer_id TEXT PRIMARY KEY,
    nomination_id TEXT NOT NULL UNIQUE REFERENCES nominations(nomination_id),
    inventory_lot_id TEXT NOT NULL REFERENCES inventory_lots(lot_id),
    generated_mwh TEXT NOT NULL,
    expected_delivered_mwh TEXT NOT NULL,
    departed_at TEXT NOT NULL,
    arrived_at TEXT,
    state TEXT NOT NULL DEFAULT 'in_transit' CHECK(state IN ('in_transit','delivered','disputed')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS supply_scenarios (
    scenario_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN ('draft','approved','retired')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scenario_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_id TEXT NOT NULL REFERENCES supply_scenarios(scenario_id),
    as_of_date TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(scenario_id, as_of_date, input_sha256)
);

CREATE TABLE IF NOT EXISTS transmission_boundaries (
    boundary_id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id TEXT NOT NULL REFERENCES routes(route_id),
    service_date TEXT NOT NULL,
    version INTEGER NOT NULL,
    channel_capacity_mw TEXT NOT NULL,
    cable_thermal_limit_mw TEXT NOT NULL,
    compensation_mvar TEXT NOT NULL,
    compensation_ratio TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_until TEXT NOT NULL,
    published_by TEXT NOT NULL REFERENCES supply_users(user_id),
    published_at TEXT NOT NULL,
    UNIQUE(route_id, service_date, version)
);

CREATE INDEX IF NOT EXISTS idx_boundaries_route_date
ON transmission_boundaries(route_id, service_date, version);

CREATE TABLE IF NOT EXISTS station_declarations (
    declaration_id TEXT PRIMARY KEY,
    route_id TEXT NOT NULL REFERENCES routes(route_id),
    service_date TEXT NOT NULL,
    installed_mw TEXT NOT NULL,
    availability_percent TEXT NOT NULL,
    ramp_json TEXT NOT NULL,
    reserve_mw TEXT NOT NULL,
    respond_by TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    submitted_by TEXT NOT NULL REFERENCES supply_users(user_id),
    submitted_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_declarations_route_date
ON station_declarations(route_id, service_date, submitted_at);

CREATE TABLE IF NOT EXISTS commitment_plans (
    plan_id TEXT PRIMARY KEY,
    declaration_id TEXT NOT NULL UNIQUE REFERENCES station_declarations(declaration_id),
    route_id TEXT NOT NULL REFERENCES routes(route_id),
    service_date TEXT NOT NULL,
    boundary_id INTEGER NOT NULL REFERENCES transmission_boundaries(boundary_id),
    boundary_version INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK(state IN ('pending','confirmed','settled','cancelled')),
    outcome TEXT NOT NULL CHECK(outcome IN ('approved','derated','exemption_required')),
    committed_mw TEXT NOT NULL,
    phases_json TEXT NOT NULL,
    evaluation_json TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_plans_route_date
ON commitment_plans(route_id, service_date, state);

CREATE TABLE IF NOT EXISTS exemptions (
    exemption_id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES commitment_plans(plan_id),
    authorized_by TEXT NOT NULL REFERENCES supply_users(user_id),
    reason TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_exemptions_plan
ON exemptions(plan_id, expires_at);

CREATE TABLE IF NOT EXISTS commitment_locks (
    lock_id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL UNIQUE REFERENCES commitment_plans(plan_id),
    route_id TEXT NOT NULL REFERENCES routes(route_id),
    service_date TEXT NOT NULL,
    channel_mw TEXT NOT NULL,
    compensation_mvar TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'held' CHECK(state IN ('held','released')),
    exemption_id INTEGER REFERENCES exemptions(exemption_id),
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL,
    released_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_locks_route_date
ON commitment_locks(route_id, service_date, state);

CREATE TABLE IF NOT EXISTS execution_receipts (
    receipt_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL UNIQUE REFERENCES commitment_plans(plan_id),
    idempotency_key TEXT NOT NULL UNIQUE,
    actuals_json TEXT NOT NULL,
    settled_mwh TEXT NOT NULL,
    shortfall_mwh TEXT NOT NULL,
    submitted_by TEXT NOT NULL REFERENCES supply_users(user_id),
    submitted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS supply_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS supply_audit_events (
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

CREATE INDEX IF NOT EXISTS idx_supply_audit_entity
ON supply_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10)
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


def row_dict(row: sqlite3.Row | None) -> dict[str, object] | None:
    return None if row is None else dict(row)
