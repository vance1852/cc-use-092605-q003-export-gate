"""结算单价、机组可用量、送出通道和提名的事务用例。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .clock import SystemClock, parse_utc, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import (
    ExemptionRequest,
    IndexQuote,
    Facility,
    InventoryLot,
    NominationRequest,
    ReceiptSubmission,
    Route,
    StationDeclaration,
    SupplyScenario,
    TransmissionBoundary,
)
from .planning import (
    AllocationRequest,
    PricePoint,
    RampPoint,
    allocate_capacity,
    canonical_json,
    decimal_text,
    delivered_after_loss,
    digest,
    effective_capacity,
    evaluate_commitment,
    form_commitment_phases,
    latest_streak,
    moving_average,
    quantize_volume,
    scenario_projection,
    settle_receipt,
    weighted_inventory_cost,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "planner": {"quote.write", "catalog.write", "scenario.write", "scenario.run", "plan.read"},
    "dispatcher": {
        "nomination.write",
        "allocation.run",
        "transfer.write",
        "inventory.write",
        "boundary.write",
        "plan.confirm",
        "plan.read",
    },
    "station": {"declaration.write", "receipt.write", "plan.read"},
    "risk": {"outage.write", "scenario.approve", "report.read", "exemption.write", "plan.read"},
    "auditor": {"report.read", "audit.read", "plan.read"},
}


class SupplyService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM supply_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM supply_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO supply_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO supply_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    def record_quote(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "quote.write")
        quote = IndexQuote.from_dict(raw)
        previous = self.connection.execute(
            "SELECT quote_id,source_revision FROM market_index_quotes WHERE market_index=? AND trade_date=? "
            "ORDER BY quote_id DESC LIMIT 1",
            (quote.market_index, quote.trade_date),
        ).fetchone()
        if previous is not None and previous["source_revision"] == quote.source_revision:
            raise Conflict("同一来源修订已登记")
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO market_index_quotes(market_index,trade_date,close_cny,source_revision,observed_at,"
                    "supersedes_quote_id,recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        quote.market_index,
                        quote.trade_date,
                        decimal_text(quote.close_cny),
                        quote.source_revision,
                        quote.observed_at,
                        None if previous is None else previous["quote_id"],
                        actor_id,
                        self._now(),
                    ),
                )
                quote_id = int(cursor.lastrowid)
                self._audit(
                    "quote",
                    str(quote_id),
                    "quote.recorded",
                    actor_id,
                    {"market_index": quote.market_index, "trade_date": quote.trade_date},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("结算单价版本冲突") from exc
        return {"quote_id": quote_id, "market_index": quote.market_index, "trade_date": quote.trade_date}

    def price_summary(self, market_index: str, sessions: int = 20) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT q.trade_date,q.close_cny FROM market_index_quotes q "
            "JOIN (SELECT trade_date,max(quote_id) quote_id FROM market_index_quotes "
            "WHERE market_index=? GROUP BY trade_date) latest ON latest.quote_id=q.quote_id "
            "ORDER BY q.trade_date DESC LIMIT ?",
            (market_index.upper(), sessions),
        ).fetchall()
        points = [PricePoint(row["trade_date"], Decimal(row["close_cny"])) for row in rows]
        if not points:
            raise NotFound("没有基准结算单价")
        streak = latest_streak(points)
        average = moving_average(points, min(5, len(points)))
        latest = max(points, key=lambda item: item.trade_date)
        return {
            "market_index": market_index.upper(),
            "latest": {"trade_date": latest.trade_date, "close_cny": decimal_text(latest.close)},
            "latest_streak": None if streak is None else streak.as_dict(),
            "moving_average": None if average is None else decimal_text(average),
            "observations": len(points),
        }

    def create_facility(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        facility = Facility.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO facilities(facility_id,name,kind,timezone,capacity_mwh,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        facility.facility_id,
                        facility.name,
                        facility.kind,
                        facility.timezone,
                        decimal_text(facility.capacity_mwh),
                        self._now(),
                    ),
                )
                self._audit("facility", facility.facility_id, "facility.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("设施编号已经存在") from exc
        return dict(raw)

    def create_route(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        route = Route.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO routes(route_id,origin_id,destination_id,product,daily_capacity,"
                    "loss_basis_points,transit_hours,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        route.route_id,
                        route.origin_id,
                        route.destination_id,
                        route.product,
                        decimal_text(route.daily_capacity),
                        route.loss_basis_points,
                        route.transit_hours,
                        self._now(),
                    ),
                )
                self._audit("route", route.route_id, "route.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("送出通道编号冲突或设施不存在") from exc
        return self.route(route.route_id)

    def route(self, route_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM routes WHERE route_id=?", (route_id,)).fetchone()
        if row is None:
            raise NotFound("送出通道不存在")
        return dict(row)

    def announce_outage(
        self,
        actor_id: str,
        route_id: str,
        starts_at: str,
        ends_at: str | None,
        capacity_percent: object,
        reason: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "outage.write")
        self.route(route_id)
        try:
            start = parse_utc(starts_at, "starts_at")
            end = None if ends_at is None else parse_utc(ends_at, "ends_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        if end is not None and end <= start:
            raise ValidationFailed("ends_at 必须晚于 starts_at")
        percentage = Decimal(str(capacity_percent))
        if percentage < 0 or percentage > 100:
            raise ValidationFailed("capacity_percent 必须在 0 到 100 之间")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO route_outages(route_id,starts_at,ends_at,capacity_percent,reason,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (route_id, utc_text(start), None if end is None else utc_text(end), decimal_text(percentage), reason, actor_id, self._now()),
            )
            outage_id = int(cursor.lastrowid)
            self._audit("route", route_id, "outage.announced", actor_id, {"outage_id": outage_id})
        return {"outage_id": outage_id, "route_id": route_id, "state": "announced"}

    def add_inventory_lot(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "inventory.write")
        lot = InventoryLot.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO inventory_lots(lot_id,facility_id,product,grade,quantity_mwh,available_mwh,"
                    "unit_cost_cny,received_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        lot.lot_id,
                        lot.facility_id,
                        lot.product,
                        lot.grade,
                        decimal_text(lot.quantity_mwh),
                        decimal_text(lot.quantity_mwh),
                        decimal_text(lot.unit_cost_cny),
                        lot.received_at,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("inventory_lot", lot.lot_id, "inventory.received", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("风机资源批次冲突或设施不存在") from exc
        return self.inventory_lot(lot.lot_id)

    def inventory_lot(self, lot_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM inventory_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if row is None:
            raise NotFound("风机资源批次不存在")
        return dict(row)

    def inventory_summary(self, facility_id: str, product: str) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT * FROM inventory_lots WHERE facility_id=? AND product=? ORDER BY received_at,lot_id",
            (facility_id, product),
        ).fetchall()
        return {"facility_id": facility_id, "product": product, **weighted_inventory_cost(rows)}

    def submit_nomination(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "nomination.write")
        nomination = NominationRequest.from_dict(raw)
        request_digest = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM supply_idempotency WHERE scope='nomination' AND idempotency_key=?",
            (nomination.idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同提名内容")
            return json.loads(stored["response_json"])
        route = self.route(nomination.route_id)
        if route["state"] != "active":
            raise InvalidState("送出通道当前不可提名")
        response = {
            "nomination_id": nomination.nomination_id,
            "route_id": nomination.route_id,
            "state": "submitted",
            "revision": 1,
        }
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO nominations(nomination_id,route_id,shipper_id,service_date,requested_mwh,"
                    "priority,idempotency_key,submitted_by,submitted_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        nomination.nomination_id,
                        nomination.route_id,
                        nomination.shipper_id,
                        nomination.service_date,
                        decimal_text(nomination.requested_mwh),
                        nomination.priority,
                        nomination.idempotency_key,
                        actor_id,
                        self._now(),
                    ),
                )
                self.connection.execute(
                    "INSERT INTO supply_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                    "VALUES('nomination',?,?,?,?)",
                    (nomination.idempotency_key, request_digest, canonical_json(response), self._now()),
                )
                self._audit("nomination", nomination.nomination_id, "nomination.submitted", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("提名编号或幂等键冲突") from exc
        return response

    def _capacity_for_date(self, route: sqlite3.Row, service_date: str) -> Decimal:
        start = service_date + "T00:00:00Z"
        end = service_date + "T23:59:59Z"
        rows = self.connection.execute(
            "SELECT capacity_percent FROM route_outages WHERE route_id=? AND state IN ('announced','active') "
            "AND starts_at<=? AND (ends_at IS NULL OR ends_at>=?) ORDER BY outage_id",
            (route["route_id"], end, start),
        ).fetchall()
        percentages = [Decimal(row["capacity_percent"]) for row in rows]
        return effective_capacity(Decimal(route["daily_capacity"]), percentages)

    def allocate(self, actor_id: str, route_id: str, service_date: str) -> dict[str, Any]:
        self._require(actor_id, "allocation.run")
        route = self.connection.execute("SELECT * FROM routes WHERE route_id=?", (route_id,)).fetchone()
        if route is None:
            raise NotFound("送出通道不存在")
        nominations = self.connection.execute(
            "SELECT * FROM nominations WHERE route_id=? AND service_date=? AND state='submitted' "
            "ORDER BY priority,submitted_at,nomination_id",
            (route_id, service_date),
        ).fetchall()
        if not nominations:
            raise InvalidState("没有待分配提名")
        requests = [
            AllocationRequest(
                row["nomination_id"],
                Decimal(row["requested_mwh"]),
                int(row["priority"]),
                row["submitted_at"],
            )
            for row in nominations
        ]
        available = self._capacity_for_date(route, service_date)
        input_value = [dict(row) for row in nominations]
        input_sha256 = digest({"route": dict(route), "nominations": input_value, "capacity": str(available)})
        result_rows = allocate_capacity(available, requests)
        result = {
            "route_id": route_id,
            "service_date": service_date,
            "available_capacity": decimal_text(available),
            "allocations": result_rows,
        }
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO allocation_runs(route_id,service_date,input_sha256,available_capacity,result_json,"
                "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (route_id, service_date, input_sha256, decimal_text(available), canonical_json(result), actor_id, self._now()),
            )
            for item in result_rows:
                state = "allocated" if Decimal(item["allocated_mwh"]) > 0 else "cancelled"
                self.connection.execute(
                    "UPDATE nominations SET allocated_mwh=?,state=?,revision=revision+1 "
                    "WHERE nomination_id=? AND state='submitted'",
                    (item["allocated_mwh"], state, item["nomination_id"]),
                )
            allocation_id = int(cursor.lastrowid)
            self._audit("route", route_id, "allocation.completed", actor_id, {"allocation_id": allocation_id})
        return {"allocation_id": allocation_id, **result}

    def dispatch_transfer(
        self,
        actor_id: str,
        transfer_id: str,
        nomination_id: str,
        lot_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        self._require(actor_id, "transfer.write")
        nomination = self.connection.execute(
            "SELECT n.*,r.loss_basis_points,r.transit_hours,r.origin_id FROM nominations n "
            "JOIN routes r ON r.route_id=n.route_id WHERE n.nomination_id=?",
            (nomination_id,),
        ).fetchone()
        if nomination is None:
            raise NotFound("提名不存在")
        if nomination["state"] != "allocated" or nomination["revision"] != expected_revision:
            raise InvalidState("提名不是当前可并网版本")
        lot = self.connection.execute("SELECT * FROM inventory_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if lot is None:
            raise NotFound("风机资源批次不存在")
        allocated = Decimal(nomination["allocated_mwh"])
        available = Decimal(lot["available_mwh"])
        if lot["facility_id"] != nomination["origin_id"] or lot["product"] != self.route(nomination["route_id"])["product"]:
            raise Conflict("风机资源批次与送出通道起点或机组类型不匹配")
        if available < allocated:
            raise Conflict("机组可用量不足以完成分配")
        expected_delivery = delivered_after_loss(allocated, int(nomination["loss_basis_points"]))
        departed_at = self._now()
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE inventory_lots SET available_mwh=?,revision=revision+1 WHERE lot_id=? AND revision=?",
                (decimal_text(quantize_volume(available - allocated)), lot_id, lot["revision"]),
            )
            self.connection.execute(
                "UPDATE nominations SET state='in_transit',revision=revision+1 WHERE nomination_id=? AND revision=?",
                (nomination_id, expected_revision),
            )
            self.connection.execute(
                "INSERT INTO transfers(transfer_id,nomination_id,inventory_lot_id,generated_mwh,"
                "expected_delivered_mwh,departed_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    transfer_id,
                    nomination_id,
                    lot_id,
                    decimal_text(allocated),
                    decimal_text(expected_delivery),
                    departed_at,
                    actor_id,
                    departed_at,
                ),
            )
            self._audit("transfer", transfer_id, "transfer.dispatched", actor_id, {"nomination_id": nomination_id})
        return {
            "transfer_id": transfer_id,
            "state": "in_transit",
            "generated_mwh": decimal_text(allocated),
            "expected_delivered_mwh": decimal_text(expected_delivery),
            "expected_arrival": utc_text(parse_utc(departed_at) + timedelta(hours=int(nomination["transit_hours"]))),
        }

    def create_scenario(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "scenario.write")
        scenario = SupplyScenario.from_dict(raw)
        definition = canonical_json(raw)
        content_sha256 = hashlib.sha256(definition.encode("utf-8")).hexdigest()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO supply_scenarios(scenario_id,name,definition_json,content_sha256,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (scenario.scenario_id, scenario.name, definition, content_sha256, actor_id, self._now()),
                )
                self._audit("scenario", scenario.scenario_id, "scenario.created", actor_id, {"sha256": content_sha256})
        except sqlite3.IntegrityError as exc:
            raise Conflict("情景编号或内容已经存在") from exc
        return {"scenario_id": scenario.scenario_id, "state": "draft", "sha256": content_sha256}

    def approve_scenario(self, actor_id: str, scenario_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "scenario.approve")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE supply_scenarios SET state='approved',revision=revision+1 "
                "WHERE scenario_id=? AND state='draft' AND revision=?",
                (scenario_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("情景不是当前草稿版本")
            self._audit("scenario", scenario_id, "scenario.approved", actor_id, {})
        return {"scenario_id": scenario_id, "state": "approved", "revision": expected_revision + 1}

    def run_scenario(self, actor_id: str, scenario_id: str, as_of_date: str) -> dict[str, Any]:
        self._require(actor_id, "scenario.run")
        row = self.connection.execute(
            "SELECT * FROM supply_scenarios WHERE scenario_id=?", (scenario_id,)
        ).fetchone()
        if row is None:
            raise NotFound("情景不存在")
        if row["state"] != "approved":
            raise InvalidState("只有已批准情景可以运行")
        scenario = SupplyScenario.from_dict(json.loads(row["definition_json"]))
        price_row = self.connection.execute(
            "SELECT close_cny FROM market_index_quotes WHERE trade_date<=? ORDER BY trade_date DESC,quote_id DESC LIMIT 1",
            (as_of_date,),
        ).fetchone()
        if price_row is None:
            raise InvalidState("截止日期没有可用结算单价")
        routes = self.connection.execute("SELECT * FROM routes WHERE state='active' ORDER BY route_id").fetchall()
        inventory = self.connection.execute(
            "SELECT facility_id,product,sum(CAST(available_mwh AS REAL)) available_mwh "
            "FROM inventory_lots GROUP BY facility_id,product ORDER BY facility_id,product"
        ).fetchall()
        input_value = {
            "scenario_sha256": row["content_sha256"],
            "as_of_date": as_of_date,
            "price": price_row["close_cny"],
            "routes": [dict(item) for item in routes],
            "inventory": [dict(item) for item in inventory],
        }
        input_sha256 = digest(input_value)
        existing = self.connection.execute(
            "SELECT run_id,result_json FROM scenario_runs WHERE scenario_id=? AND as_of_date=? AND input_sha256=?",
            (scenario_id, as_of_date, input_sha256),
        ).fetchone()
        if existing is not None:
            return {"run_id": existing["run_id"], **json.loads(existing["result_json"]), "replayed": True}
        result = scenario_projection(
            current_price=Decimal(price_row["close_cny"]),
            market_index_drop_percent=scenario.market_index_drop_percent,
            routes=routes,
            inventory=inventory,
            route_capacity_changes=scenario.route_capacity_changes,
            demand_changes=scenario.demand_changes,
        )
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO scenario_runs(scenario_id,as_of_date,input_sha256,result_json,created_by,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (scenario_id, as_of_date, input_sha256, canonical_json(result), actor_id, self._now()),
            )
            run_id = int(cursor.lastrowid)
            self._audit("scenario", scenario_id, "scenario.executed", actor_id, {"run_id": run_id})
        return {"run_id": run_id, **result, "replayed": False}

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM supply_audit_events ORDER BY event_id").fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}

    # ---- 分阶段送出承诺门禁 ----

    def _plan_row(self, plan_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM commitment_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if row is None:
            raise NotFound("承诺计划不存在")
        return row

    def _plan_summary(self, plan_id: str) -> dict[str, Any]:
        row = self._plan_row(plan_id)
        return {
            "plan_id": row["plan_id"],
            "state": row["state"],
            "outcome": row["outcome"],
            "committed_mw": row["committed_mw"],
            "boundary_version": row["boundary_version"],
        }

    def _held_locks(self, route_id: str, service_date: str) -> tuple[Decimal, Decimal]:
        rows = self.connection.execute(
            "SELECT channel_mw,compensation_mvar FROM commitment_locks "
            "WHERE route_id=? AND service_date=? AND state='held'",
            (route_id, service_date),
        ).fetchall()
        channel = sum((Decimal(row["channel_mw"]) for row in rows), Decimal("0"))
        compensation = sum((Decimal(row["compensation_mvar"]) for row in rows), Decimal("0"))
        return channel, compensation

    def _boundary_outage_percents(self, boundary: sqlite3.Row) -> list[Decimal]:
        rows = self.connection.execute(
            "SELECT capacity_percent FROM route_outages WHERE route_id=? AND state IN ('announced','active') "
            "AND starts_at<? AND (ends_at IS NULL OR ends_at>?) ORDER BY outage_id",
            (boundary["route_id"], boundary["effective_until"], boundary["effective_from"]),
        ).fetchall()
        return [Decimal(row["capacity_percent"]) for row in rows]

    def _boundary_channel_ceiling(self, boundary: sqlite3.Row) -> Decimal:
        base = min(Decimal(boundary["channel_capacity_mw"]), Decimal(boundary["cable_thermal_limit_mw"]))
        return effective_capacity(base, self._boundary_outage_percents(boundary))

    def _build_evaluation(
        self, declaration: sqlite3.Row, boundary: sqlite3.Row
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        ramp_points = [
            RampPoint(int(point["offset_minutes"]), Decimal(str(point["mw"])))
            for point in json.loads(declaration["ramp_json"])
        ]
        start = parse_utc(boundary["effective_from"], "effective_from")
        end = parse_utc(boundary["effective_until"], "effective_until")
        window_minutes = int((end - start).total_seconds() // 60)
        if ramp_points[-1].offset_minutes >= window_minutes:
            raise ValidationFailed("爬坡曲线超出边界有效期窗口")
        held_channel, held_compensation = self._held_locks(boundary["route_id"], boundary["service_date"])
        evaluation = evaluate_commitment(
            declared_target_mw=ramp_points[-1].mw,
            installed_mw=Decimal(declaration["installed_mw"]),
            availability_percent=Decimal(declaration["availability_percent"]),
            reserve_mw=Decimal(declaration["reserve_mw"]),
            channel_capacity_mw=Decimal(boundary["channel_capacity_mw"]),
            cable_thermal_limit_mw=Decimal(boundary["cable_thermal_limit_mw"]),
            outage_percents=self._boundary_outage_percents(boundary),
            compensation_mvar=Decimal(boundary["compensation_mvar"]),
            compensation_ratio=Decimal(boundary["compensation_ratio"]),
            held_channel_mw=held_channel,
            held_compensation_mvar=held_compensation,
        )
        phases = form_commitment_phases(
            window_start=start,
            window_minutes=window_minutes,
            ramp_points=ramp_points,
            committed_mw=Decimal(evaluation["committed_mw"]),
        )
        return evaluation, phases

    def _form_plan(self, declaration: sqlite3.Row, boundary: sqlite3.Row, actor_id: str) -> str:
        evaluation, phases = self._build_evaluation(declaration, boundary)
        plan_id = f"plan-{declaration['declaration_id']}"
        now = self._now()
        self.connection.execute(
            "INSERT INTO commitment_plans(plan_id,declaration_id,route_id,service_date,boundary_id,"
            "boundary_version,state,outcome,committed_mw,phases_json,evaluation_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,'pending',?,?,?,?,?,?)",
            (
                plan_id,
                declaration["declaration_id"],
                declaration["route_id"],
                declaration["service_date"],
                boundary["boundary_id"],
                boundary["version"],
                evaluation["outcome"],
                evaluation["committed_mw"],
                canonical_json(phases),
                canonical_json(evaluation),
                now,
                now,
            ),
        )
        self._audit(
            "commitment_plan",
            plan_id,
            "plan.formed",
            actor_id,
            {
                "boundary_version": boundary["version"],
                "outcome": evaluation["outcome"],
                "committed_mw": evaluation["committed_mw"],
            },
        )
        return plan_id

    def _reevaluate_plan(self, plan: sqlite3.Row, boundary: sqlite3.Row, actor_id: str) -> None:
        declaration = self.connection.execute(
            "SELECT * FROM station_declarations WHERE declaration_id=?", (plan["declaration_id"],)
        ).fetchone()
        evaluation, phases = self._build_evaluation(declaration, boundary)
        self.connection.execute(
            "UPDATE commitment_plans SET boundary_id=?,boundary_version=?,outcome=?,committed_mw=?,"
            "phases_json=?,evaluation_json=?,revision=revision+1,updated_at=? WHERE plan_id=? AND state='pending'",
            (
                boundary["boundary_id"],
                boundary["version"],
                evaluation["outcome"],
                evaluation["committed_mw"],
                canonical_json(phases),
                canonical_json(evaluation),
                self._now(),
                plan["plan_id"],
            ),
        )
        self._audit(
            "commitment_plan",
            plan["plan_id"],
            "plan.reevaluated",
            actor_id,
            {
                "previous_boundary_version": plan["boundary_version"],
                "boundary_version": boundary["version"],
                "outcome": evaluation["outcome"],
                "committed_mw": evaluation["committed_mw"],
            },
        )

    def publish_boundary(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "boundary.write")
        boundary = TransmissionBoundary.from_dict(raw)
        self.route(boundary.route_id)
        latest = self.connection.execute(
            "SELECT max(version) AS max_version FROM transmission_boundaries WHERE route_id=? AND service_date=?",
            (boundary.route_id, boundary.service_date),
        ).fetchone()
        max_version = latest["max_version"]
        if max_version is not None and boundary.version <= int(max_version):
            raise Conflict("边界版本必须大于当前已发布版本")
        formed: list[str] = []
        reevaluated: list[str] = []
        skipped: list[str] = []
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO transmission_boundaries(route_id,service_date,version,channel_capacity_mw,"
                "cable_thermal_limit_mw,compensation_mvar,compensation_ratio,effective_from,effective_until,"
                "published_by,published_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    boundary.route_id,
                    boundary.service_date,
                    boundary.version,
                    decimal_text(boundary.channel_capacity_mw),
                    decimal_text(boundary.cable_thermal_limit_mw),
                    decimal_text(boundary.compensation_mvar),
                    decimal_text(boundary.compensation_ratio),
                    utc_text(parse_utc(boundary.effective_from, "effective_from")),
                    utc_text(parse_utc(boundary.effective_until, "effective_until")),
                    actor_id,
                    self._now(),
                ),
            )
            boundary_id = int(cursor.lastrowid)
            boundary_row = self.connection.execute(
                "SELECT * FROM transmission_boundaries WHERE boundary_id=?", (boundary_id,)
            ).fetchone()
            self._audit(
                "transmission_boundary",
                str(boundary_id),
                "boundary.published",
                actor_id,
                {
                    "route_id": boundary.route_id,
                    "service_date": boundary.service_date,
                    "version": boundary.version,
                },
            )
            declarations = self.connection.execute(
                "SELECT * FROM station_declarations WHERE route_id=? AND service_date=? AND declaration_id NOT IN "
                "(SELECT declaration_id FROM commitment_plans) ORDER BY submitted_at,declaration_id",
                (boundary.route_id, boundary.service_date),
            ).fetchall()
            for declaration in declarations:
                try:
                    formed.append(self._form_plan(declaration, boundary_row, actor_id))
                except ValidationFailed:
                    skipped.append(declaration["declaration_id"])
            pending = self.connection.execute(
                "SELECT * FROM commitment_plans WHERE route_id=? AND service_date=? AND state='pending' "
                "AND boundary_id<>? ORDER BY plan_id",
                (boundary.route_id, boundary.service_date, boundary_id),
            ).fetchall()
            for plan in pending:
                try:
                    self._reevaluate_plan(plan, boundary_row, actor_id)
                    reevaluated.append(plan["plan_id"])
                except ValidationFailed:
                    skipped.append(plan["plan_id"])
        return {
            "boundary_id": boundary_id,
            "route_id": boundary.route_id,
            "service_date": boundary.service_date,
            "version": boundary.version,
            "formed_plan_ids": formed,
            "reevaluated_plan_ids": reevaluated,
            "skipped_ids": skipped,
        }

    def submit_declaration(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "declaration.write")
        declaration = StationDeclaration.from_dict(raw)
        request_digest = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM supply_idempotency WHERE scope='declaration' AND idempotency_key=?",
            (declaration.idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同申报内容")
            return json.loads(stored["response_json"])
        route = self.route(declaration.route_id)
        if route["state"] != "active":
            raise InvalidState("送出通道当前不可申报")
        boundary = self.connection.execute(
            "SELECT * FROM transmission_boundaries WHERE route_id=? AND service_date=? "
            "ORDER BY version DESC LIMIT 1",
            (declaration.route_id, declaration.service_date),
        ).fetchone()
        ramp_json = canonical_json(
            [{"offset_minutes": point.offset_minutes, "mw": decimal_text(point.mw)} for point in declaration.ramp_points]
        )
        respond_by = utc_text(parse_utc(declaration.respond_by, "respond_by"))
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO station_declarations(declaration_id,route_id,service_date,installed_mw,"
                    "availability_percent,ramp_json,reserve_mw,respond_by,idempotency_key,submitted_by,submitted_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        declaration.declaration_id,
                        declaration.route_id,
                        declaration.service_date,
                        decimal_text(declaration.installed_mw),
                        decimal_text(declaration.availability_percent),
                        ramp_json,
                        decimal_text(declaration.reserve_mw),
                        respond_by,
                        declaration.idempotency_key,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit(
                    "station_declaration",
                    declaration.declaration_id,
                    "declaration.submitted",
                    actor_id,
                    {"route_id": declaration.route_id, "service_date": declaration.service_date},
                )
                plan_summary = None
                if boundary is not None:
                    declaration_row = self.connection.execute(
                        "SELECT * FROM station_declarations WHERE declaration_id=?",
                        (declaration.declaration_id,),
                    ).fetchone()
                    plan_summary = self._plan_summary(self._form_plan(declaration_row, boundary, actor_id))
                response = {
                    "declaration_id": declaration.declaration_id,
                    "route_id": declaration.route_id,
                    "service_date": declaration.service_date,
                    "state": "planned" if plan_summary is not None else "awaiting_boundary",
                    "plan": plan_summary,
                }
                self.connection.execute(
                    "INSERT INTO supply_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                    "VALUES('declaration',?,?,?,?)",
                    (declaration.idempotency_key, request_digest, canonical_json(response), self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("申报编号或幂等键冲突") from exc
        return response

    def grant_exemption(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "exemption.write")
        request = ExemptionRequest.from_dict(raw)
        plan = self._plan_row(request.plan_id)
        if plan["state"] != "pending":
            raise InvalidState("只有待确认计划可以登记豁免")
        expires_at = utc_text(parse_utc(request.expires_at, "expires_at"))
        if parse_utc(expires_at, "expires_at") <= self.clock.now():
            raise ValidationFailed("expires_at 必须晚于当前时间")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO exemptions(plan_id,authorized_by,reason,expires_at,created_at) VALUES(?,?,?,?,?)",
                (request.plan_id, actor_id, request.reason, expires_at, self._now()),
            )
            exemption_id = int(cursor.lastrowid)
            self._audit(
                "commitment_plan",
                request.plan_id,
                "exemption.granted",
                actor_id,
                {"exemption_id": exemption_id, "reason": request.reason, "expires_at": expires_at},
            )
        return {
            "exemption_id": exemption_id,
            "plan_id": request.plan_id,
            "authorized_by": actor_id,
            "reason": request.reason,
            "expires_at": expires_at,
        }

    def _active_exemption(self, plan_id: str) -> sqlite3.Row | None:
        rows = self.connection.execute(
            "SELECT * FROM exemptions WHERE plan_id=? ORDER BY exemption_id DESC", (plan_id,)
        ).fetchall()
        now = self.clock.now()
        for row in rows:
            if parse_utc(row["expires_at"], "expires_at") > now:
                return row
        return None

    def confirm_plan(self, actor_id: str, plan_id: str) -> dict[str, Any]:
        self._require(actor_id, "plan.confirm")
        plan = self._plan_row(plan_id)
        if plan["state"] != "pending":
            raise InvalidState("计划不是待确认状态")
        declaration = self.connection.execute(
            "SELECT * FROM station_declarations WHERE declaration_id=?", (plan["declaration_id"],)
        ).fetchone()
        boundary = self.connection.execute(
            "SELECT * FROM transmission_boundaries WHERE boundary_id=?", (plan["boundary_id"],)
        ).fetchone()
        overdue = self.clock.now() > parse_utc(declaration["respond_by"], "respond_by")
        exemption_needed = plan["outcome"] == "exemption_required" or overdue
        exemption = self._active_exemption(plan_id) if exemption_needed else None
        if exemption_needed and exemption is None:
            if overdue:
                raise InvalidState("已超过最迟响应时刻，需要紧急保供豁免才能确认")
            raise InvalidState("计划需要紧急保供豁免才能确认")
        committed = Decimal(plan["committed_mw"])
        compensation_need = quantize_volume(committed * Decimal(boundary["compensation_ratio"]))
        with transaction(self.connection, immediate=True):
            if exemption is None:
                ceiling = self._boundary_channel_ceiling(boundary)
                held_channel, held_compensation = self._held_locks(plan["route_id"], plan["service_date"])
                if held_channel + committed > ceiling:
                    raise Conflict("送出通道余量不足，确认失败且未锁定任何资源")
                if held_compensation + compensation_need > Decimal(boundary["compensation_mvar"]):
                    raise Conflict("无功补偿余量不足，确认失败且未锁定任何资源")
            cursor = self.connection.execute(
                "INSERT INTO commitment_locks(plan_id,route_id,service_date,channel_mw,compensation_mvar,state,"
                "exemption_id,created_by,created_at) VALUES(?,?,?,?,?,'held',?,?,?)",
                (
                    plan_id,
                    plan["route_id"],
                    plan["service_date"],
                    decimal_text(committed),
                    decimal_text(compensation_need),
                    None if exemption is None else exemption["exemption_id"],
                    actor_id,
                    self._now(),
                ),
            )
            lock_id = int(cursor.lastrowid)
            updated = self.connection.execute(
                "UPDATE commitment_plans SET state='confirmed',revision=revision+1,updated_at=? "
                "WHERE plan_id=? AND state='pending'",
                (self._now(), plan_id),
            )
            if updated.rowcount != 1:
                raise InvalidState("计划不是待确认状态")
            self._audit(
                "commitment_plan",
                plan_id,
                "plan.confirmed",
                actor_id,
                {
                    "lock_id": lock_id,
                    "channel_mw": decimal_text(committed),
                    "compensation_mvar": decimal_text(compensation_need),
                    "exemption_id": None if exemption is None else exemption["exemption_id"],
                },
            )
        return {
            "plan_id": plan_id,
            "state": "confirmed",
            "lock_id": lock_id,
            "locked": {
                "channel_mw": decimal_text(committed),
                "compensation_mvar": decimal_text(compensation_need),
            },
            "exemption_id": None if exemption is None else exemption["exemption_id"],
        }

    def submit_receipt(self, actor_id: str, plan_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "receipt.write")
        receipt = ReceiptSubmission.from_dict(raw)
        request_digest = digest({"plan_id": plan_id, "payload": raw})
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM supply_idempotency WHERE scope='receipt' AND idempotency_key=?",
            (receipt.idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同回执内容")
            return json.loads(stored["response_json"])
        plan = self._plan_row(plan_id)
        if plan["state"] != "confirmed":
            raise InvalidState("计划不是已确认状态，不能登记执行回执")
        phases = json.loads(plan["phases_json"])
        settlement = settle_receipt(phases, receipt.actuals)
        response = {
            "receipt_id": receipt.receipt_id,
            "plan_id": plan_id,
            "state": "settled",
            **settlement,
        }
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO execution_receipts(receipt_id,plan_id,idempotency_key,actuals_json,settled_mwh,"
                    "shortfall_mwh,submitted_by,submitted_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        receipt.receipt_id,
                        plan_id,
                        receipt.idempotency_key,
                        canonical_json({phase: decimal_text(value) for phase, value in receipt.actuals.items()}),
                        settlement["settled_mwh"],
                        settlement["shortfall_mwh"],
                        actor_id,
                        self._now(),
                    ),
                )
                self.connection.execute(
                    "UPDATE commitment_locks SET state='released',released_at=? WHERE plan_id=? AND state='held'",
                    (self._now(), plan_id),
                )
                self.connection.execute(
                    "UPDATE commitment_plans SET state='settled',revision=revision+1,updated_at=? WHERE plan_id=?",
                    (self._now(), plan_id),
                )
                self._audit(
                    "commitment_plan",
                    plan_id,
                    "plan.settled",
                    actor_id,
                    {
                        "receipt_id": receipt.receipt_id,
                        "settled_mwh": settlement["settled_mwh"],
                        "shortfall_mwh": settlement["shortfall_mwh"],
                    },
                )
                self.connection.execute(
                    "INSERT INTO supply_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                    "VALUES('receipt',?,?,?,?)",
                    (receipt.idempotency_key, request_digest, canonical_json(response), self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("回执编号或幂等键冲突") from exc
        return response

    def commitment_plan(self, actor_id: str, plan_id: str) -> dict[str, Any]:
        self._require(actor_id, "plan.read")
        plan = self._plan_row(plan_id)
        declaration = self.connection.execute(
            "SELECT * FROM station_declarations WHERE declaration_id=?", (plan["declaration_id"],)
        ).fetchone()
        boundary = self.connection.execute(
            "SELECT * FROM transmission_boundaries WHERE boundary_id=?", (plan["boundary_id"],)
        ).fetchone()
        locks = self.connection.execute(
            "SELECT * FROM commitment_locks WHERE plan_id=? ORDER BY lock_id", (plan_id,)
        ).fetchall()
        exemptions = self.connection.execute(
            "SELECT * FROM exemptions WHERE plan_id=? ORDER BY exemption_id", (plan_id,)
        ).fetchall()
        receipt = self.connection.execute(
            "SELECT * FROM execution_receipts WHERE plan_id=?", (plan_id,)
        ).fetchone()
        events = self.connection.execute(
            "SELECT event_type,actor_id,payload_json,created_at FROM supply_audit_events "
            "WHERE entity_type='commitment_plan' AND entity_id=? ORDER BY event_id",
            (plan_id,),
        ).fetchall()
        now = self.clock.now()
        response_overdue = plan["state"] == "pending" and now > parse_utc(
            declaration["respond_by"], "respond_by"
        )
        requires_exemption = plan["state"] == "pending" and (
            plan["outcome"] == "exemption_required" or response_overdue
        )
        return {
            "plan_id": plan["plan_id"],
            "state": plan["state"],
            "outcome": plan["outcome"],
            "route_id": plan["route_id"],
            "service_date": plan["service_date"],
            "revision": plan["revision"],
            "committed_mw": plan["committed_mw"],
            "boundary": {
                "boundary_id": boundary["boundary_id"],
                "version": boundary["version"],
                "channel_capacity_mw": boundary["channel_capacity_mw"],
                "cable_thermal_limit_mw": boundary["cable_thermal_limit_mw"],
                "compensation_mvar": boundary["compensation_mvar"],
                "compensation_ratio": boundary["compensation_ratio"],
                "effective_from": boundary["effective_from"],
                "effective_until": boundary["effective_until"],
            },
            "declaration": {
                "declaration_id": declaration["declaration_id"],
                "installed_mw": declaration["installed_mw"],
                "availability_percent": declaration["availability_percent"],
                "reserve_mw": declaration["reserve_mw"],
                "respond_by": declaration["respond_by"],
                "submitted_by": declaration["submitted_by"],
            },
            "phases": json.loads(plan["phases_json"]),
            "evaluation": json.loads(plan["evaluation_json"]),
            "requires_exemption": requires_exemption,
            "response_overdue": response_overdue,
            "exemptions": [
                {
                    "exemption_id": row["exemption_id"],
                    "authorized_by": row["authorized_by"],
                    "reason": row["reason"],
                    "expires_at": row["expires_at"],
                    "active": parse_utc(row["expires_at"], "expires_at") > now,
                }
                for row in exemptions
            ],
            "locks": [
                {
                    "lock_id": row["lock_id"],
                    "channel_mw": row["channel_mw"],
                    "compensation_mvar": row["compensation_mvar"],
                    "state": row["state"],
                    "exemption_id": row["exemption_id"],
                    "created_at": row["created_at"],
                    "released_at": row["released_at"],
                }
                for row in locks
            ],
            "receipt": None
            if receipt is None
            else {
                "receipt_id": receipt["receipt_id"],
                "settled_mwh": receipt["settled_mwh"],
                "shortfall_mwh": receipt["shortfall_mwh"],
                "submitted_by": receipt["submitted_by"],
                "submitted_at": receipt["submitted_at"],
            },
            "decision_log": [
                {
                    "event_type": row["event_type"],
                    "actor_id": row["actor_id"],
                    "payload": json.loads(row["payload_json"]),
                    "created_at": row["created_at"],
                }
                for row in events
            ],
        }

    def list_plans(
        self,
        actor_id: str,
        *,
        state: str | None = None,
        route_id: str | None = None,
        service_date: str | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "plan.read")
        clauses: list[str] = []
        params: list[str] = []
        if state is not None:
            if state not in ("pending", "confirmed", "settled", "cancelled"):
                raise ValidationFailed("state 不是受支持的计划状态")
            clauses.append("state=?")
            params.append(state)
        if route_id is not None:
            clauses.append("route_id=?")
            params.append(route_id)
        if service_date is not None:
            clauses.append("service_date=?")
            params.append(service_date)
        where = "" if not clauses else " WHERE " + " AND ".join(clauses)
        rows = self.connection.execute(
            "SELECT plan_id FROM commitment_plans" + where + " ORDER BY service_date,route_id,plan_id",
            params,
        ).fetchall()
        return {"plans": [self._plan_summary(row["plan_id"]) for row in rows]}
