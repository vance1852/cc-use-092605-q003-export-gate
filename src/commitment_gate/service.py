"""送出边界、场站申报、分阶段承诺计划与执行回执的事务用例。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal
from typing import Any, Mapping

from .clock import SystemClock, parse_utc, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .gating import (
    COMPENSATION_RATIO,
    build_phase_schedule,
    decimal_text,
    evaluate_commitment,
    maintenance_capacity_percent,
    overlapping_maintenance,
    quantize_mw,
)
from .models import (
    DeliveryBoundary,
    ExemptionRequest,
    MaintenanceWindow,
    PlanRequest,
    RampPoint,
    ReceiptRequest,
    StationDeclaration,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "dispatcher": {"boundary.publish", "execution.write", "receipt.write"},
    "station": {"declaration.write"},
    "coordinator": {"plan.create", "plan.confirm", "plan.cancel", "exemption.write", "report.read"},
    "auditor": {"report.read", "audit.read"},
}


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class CommitmentGateService:
    """在单个 SQLite 连接上提供承诺门禁的全部业务操作。"""

    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM gate_users WHERE user_id=?", (user_id,)
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
            "SELECT event_hash FROM gate_audit_events ORDER BY event_id DESC LIMIT 1"
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
        event_hash = hashlib.sha256(_canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO gate_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                _canonical_json(payload),
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
                    "INSERT INTO gate_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    # ------------------------------------------------------------------
    # 送出边界
    # ------------------------------------------------------------------

    def _boundary_row(self, boundary_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM delivery_boundaries WHERE boundary_id=?", (boundary_id,)
        ).fetchone()
        if row is None:
            raise NotFound("送出边界不存在")
        return row

    @staticmethod
    def _boundary_windows(row: sqlite3.Row) -> list[MaintenanceWindow]:
        return [
            MaintenanceWindow(
                starts_at=item["starts_at"],
                ends_at=item["ends_at"],
                capacity_percent=Decimal(item["capacity_percent"]),
                reason=item["reason"],
            )
            for item in json.loads(row["maintenance_json"])
        ]

    def publish_boundary(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """发布带版本和有效期的送出边界；迟到修订只重估尚未确认的计划。"""
        self._require(actor_id, "boundary.publish")
        boundary = DeliveryBoundary.from_dict(raw)
        latest = self.connection.execute(
            "SELECT MAX(version) AS max_version FROM delivery_boundaries WHERE corridor_id=?",
            (boundary.corridor_id,),
        ).fetchone()
        if latest["max_version"] is not None and boundary.version <= latest["max_version"]:
            raise Conflict("边界版本必须大于当前已发布版本")
        maintenance_json = _canonical_json([item.as_dict() for item in boundary.maintenance_windows])
        reevaluated: list[str] = []
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE delivery_boundaries SET state='superseded' "
                "WHERE corridor_id=? AND state='published'",
                (boundary.corridor_id,),
            )
            self.connection.execute(
                "INSERT INTO delivery_boundaries(boundary_id,corridor_id,version,effective_from,effective_to,"
                "channel_capacity_mw,reactive_compensation_mvar,cable_thermal_limit_mw,maintenance_json,"
                "state,published_by,published_at) VALUES(?,?,?,?,?,?,?,?,?,'published',?,?)",
                (
                    boundary.boundary_id,
                    boundary.corridor_id,
                    boundary.version,
                    boundary.effective_from,
                    boundary.effective_to,
                    decimal_text(boundary.channel_capacity_mw),
                    decimal_text(boundary.reactive_compensation_mvar),
                    decimal_text(boundary.cable_thermal_limit_mw),
                    maintenance_json,
                    actor_id,
                    self._now(),
                ),
            )
            self._audit(
                "boundary",
                boundary.boundary_id,
                "boundary.published",
                actor_id,
                {"corridor_id": boundary.corridor_id, "version": boundary.version},
            )
            boundary_row = self._boundary_row(boundary.boundary_id)
            pending = self.connection.execute(
                "SELECT * FROM commitment_plans WHERE corridor_id=? AND state='pending_confirmation' "
                "ORDER BY window_start,plan_id",
                (boundary.corridor_id,),
            ).fetchall()
            for plan in pending:
                if not self._window_within(boundary_row, plan["window_start"], plan["window_end"]):
                    continue
                self._reevaluate_plan(plan, boundary_row, actor_id)
                reevaluated.append(plan["plan_id"])
        return {
            "boundary_id": boundary.boundary_id,
            "corridor_id": boundary.corridor_id,
            "version": boundary.version,
            "state": "published",
            "reevaluated_plan_ids": reevaluated,
        }

    @staticmethod
    def _window_within(boundary_row: sqlite3.Row, window_start: str, window_end: str) -> bool:
        return boundary_row["effective_from"] <= window_start and boundary_row["effective_to"] >= window_end

    def current_boundary(self, corridor_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM delivery_boundaries WHERE corridor_id=? AND state='published' "
            "ORDER BY version DESC LIMIT 1",
            (corridor_id,),
        ).fetchone()
        if row is None:
            raise NotFound("走廊没有已发布的送出边界")
        result = dict(row)
        result["maintenance_windows"] = json.loads(row["maintenance_json"])
        return result

    # ------------------------------------------------------------------
    # 场站申报
    # ------------------------------------------------------------------

    def submit_declaration(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "declaration.write")
        declaration = StationDeclaration.from_dict(raw)
        curve_json = _canonical_json(
            [
                {"offset_minutes": point.offset_minutes, "output_percent": decimal_text(point.output_percent)}
                for point in declaration.ramp_curve
            ]
        )
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "UPDATE station_declarations SET state='superseded' "
                    "WHERE corridor_id=? AND station_id=? AND state='declared'",
                    (declaration.corridor_id, declaration.station_id),
                )
                self.connection.execute(
                    "INSERT INTO station_declarations(declaration_id,corridor_id,station_id,"
                    "installed_capacity_mw,availability_percent,ramp_curve_json,reserve_mw,"
                    "latest_response_at,state,declared_by,declared_at) VALUES(?,?,?,?,?,?,?,?,'declared',?,?)",
                    (
                        declaration.declaration_id,
                        declaration.corridor_id,
                        declaration.station_id,
                        decimal_text(declaration.installed_capacity_mw),
                        decimal_text(declaration.availability_percent),
                        curve_json,
                        decimal_text(declaration.reserve_mw),
                        declaration.latest_response_at,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit(
                    "declaration",
                    declaration.declaration_id,
                    "declaration.submitted",
                    actor_id,
                    {"corridor_id": declaration.corridor_id, "station_id": declaration.station_id},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("申报编号冲突") from exc
        return {"declaration_id": declaration.declaration_id, "state": "declared"}

    def _declaration_row(self, declaration_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM station_declarations WHERE declaration_id=?", (declaration_id,)
        ).fetchone()
        if row is None:
            raise NotFound("场站申报不存在")
        return row

    # ------------------------------------------------------------------
    # 承诺计划
    # ------------------------------------------------------------------

    def _evaluate(
        self,
        boundary_row: sqlite3.Row,
        declaration_row: sqlite3.Row,
        window_start: str,
        window_end: str,
        requested_mw: Decimal,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], Decimal]:
        windows = self._boundary_windows(boundary_row)
        start = parse_utc(window_start)
        end = parse_utc(window_end)
        overlapping = overlapping_maintenance(windows, start, end)
        evaluation = evaluate_commitment(
            requested_mw=requested_mw,
            installed_capacity_mw=Decimal(declaration_row["installed_capacity_mw"]),
            availability_percent=Decimal(declaration_row["availability_percent"]),
            reserve_mw=Decimal(declaration_row["reserve_mw"]),
            channel_capacity_mw=Decimal(boundary_row["channel_capacity_mw"]),
            cable_thermal_limit_mw=Decimal(boundary_row["cable_thermal_limit_mw"]),
            reactive_compensation_mvar=Decimal(boundary_row["reactive_compensation_mvar"]),
            maintenance_percent=maintenance_capacity_percent(overlapping),
            overlapping_windows=overlapping,
        )
        ramp_curve = [
            RampPoint(int(item["offset_minutes"]), Decimal(item["output_percent"]))
            for item in json.loads(declaration_row["ramp_curve_json"])
        ]
        schedule_mw = (
            requested_mw if evaluation.decision == "exemption_required" else evaluation.committed_mw
        )
        try:
            phases, planned_mwh = build_phase_schedule(
                window_start=start,
                window_end=end,
                committed_mw=schedule_mw,
                ramp_curve=ramp_curve,
            )
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        return evaluation.as_dict(), phases, planned_mwh

    def create_plan(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """合并边界与申报，形成准备、并网、爬坡、稳定运行四阶段的待确认计划。"""
        self._require(actor_id, "plan.create")
        request = PlanRequest.from_dict(raw)
        boundary = self.connection.execute(
            "SELECT * FROM delivery_boundaries WHERE corridor_id=? AND state='published' "
            "AND effective_from<=? AND effective_to>=? ORDER BY version DESC LIMIT 1",
            (request.corridor_id, request.window_start, request.window_end),
        ).fetchone()
        if boundary is None:
            raise ValidationFailed("没有覆盖计划窗口的有效送出边界")
        declaration = self.connection.execute(
            "SELECT * FROM station_declarations WHERE corridor_id=? AND station_id=? "
            "AND state='declared' ORDER BY declared_at DESC,declaration_id DESC LIMIT 1",
            (request.corridor_id, request.station_id),
        ).fetchone()
        if declaration is None:
            raise ValidationFailed("场站尚未申报机组可用率与爬坡曲线")
        decision, phases, planned_mwh = self._evaluate(
            boundary, declaration, request.window_start, request.window_end, request.requested_mw
        )
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO commitment_plans(plan_id,corridor_id,station_id,boundary_id,declaration_id,"
                    "window_start,window_end,requested_mw,committed_mw,planned_mwh,phases_json,decision,"
                    "decision_json,state,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        request.plan_id,
                        request.corridor_id,
                        request.station_id,
                        boundary["boundary_id"],
                        declaration["declaration_id"],
                        request.window_start,
                        request.window_end,
                        decimal_text(quantize_mw(request.requested_mw)),
                        decision["committed_mw"],
                        decimal_text(planned_mwh),
                        _canonical_json(phases),
                        decision["decision"],
                        _canonical_json(decision),
                        "pending_confirmation",
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit(
                    "plan",
                    request.plan_id,
                    "plan.created",
                    actor_id,
                    {
                        "decision": decision["decision"],
                        "committed_mw": decision["committed_mw"],
                        "boundary_id": boundary["boundary_id"],
                        "boundary_version": boundary["version"],
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("计划编号冲突") from exc
        return {
            "plan_id": request.plan_id,
            "state": "pending_confirmation",
            "decision": decision["decision"],
            "committed_mw": decision["committed_mw"],
            "planned_mwh": decimal_text(planned_mwh),
            "boundary_version": boundary["version"],
            "revision": 1,
        }

    def _plan_row(self, plan_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM commitment_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if row is None:
            raise NotFound("承诺计划不存在")
        return row

    def _reevaluate_plan(
        self, plan: sqlite3.Row, boundary_row: sqlite3.Row, actor_id: str
    ) -> None:
        declaration = self._declaration_row(plan["declaration_id"])
        requested = Decimal(plan["requested_mw"])
        decision, phases, planned_mwh = self._evaluate(
            boundary_row, declaration, plan["window_start"], plan["window_end"], requested
        )
        cursor = self.connection.execute(
            "UPDATE commitment_plans SET boundary_id=?,committed_mw=?,planned_mwh=?,phases_json=?,"
            "decision=?,decision_json=?,revision=revision+1 "
            "WHERE plan_id=? AND state='pending_confirmation'",
            (
                boundary_row["boundary_id"],
                decision["committed_mw"],
                decimal_text(planned_mwh),
                _canonical_json(phases),
                decision["decision"],
                _canonical_json(decision),
                plan["plan_id"],
            ),
        )
        if cursor.rowcount == 1:
            self._audit(
                "plan",
                plan["plan_id"],
                "plan.reevaluated",
                actor_id,
                {
                    "boundary_id": boundary_row["boundary_id"],
                    "boundary_version": boundary_row["version"],
                    "decision": decision["decision"],
                    "committed_mw": decision["committed_mw"],
                },
            )

    def plan(self, plan_id: str) -> dict[str, Any]:
        row = self._plan_row(plan_id)
        result = dict(row)
        result["phases"] = json.loads(row["phases_json"])
        result["decision_detail"] = json.loads(row["decision_json"])
        return result

    def pending_plans(self, actor_id: str, corridor_id: str | None = None) -> dict[str, Any]:
        """待确认计划视图：服务重启后直接读取 SQLite 即可恢复。"""
        self._require(actor_id, "report.read")
        if corridor_id is None:
            rows = self.connection.execute(
                "SELECT * FROM commitment_plans WHERE state='pending_confirmation' "
                "ORDER BY window_start,plan_id"
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM commitment_plans WHERE state='pending_confirmation' AND corridor_id=? "
                "ORDER BY window_start,plan_id",
                (corridor_id,),
            ).fetchall()
        return {
            "pending": [
                {
                    "plan_id": row["plan_id"],
                    "corridor_id": row["corridor_id"],
                    "station_id": row["station_id"],
                    "window_start": row["window_start"],
                    "window_end": row["window_end"],
                    "requested_mw": row["requested_mw"],
                    "committed_mw": row["committed_mw"],
                    "decision": row["decision"],
                    "revision": row["revision"],
                }
                for row in rows
            ]
        }

    # ------------------------------------------------------------------
    # 紧急保供豁免
    # ------------------------------------------------------------------

    def grant_exemption(self, actor_id: str, plan_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """紧急保供豁免：记录授权人、理由与失效时间。"""
        self._require(actor_id, "exemption.write")
        request = ExemptionRequest.from_dict(raw)
        plan = self._plan_row(plan_id)
        if plan["state"] != "pending_confirmation" or plan["decision"] != "exemption_required":
            raise InvalidState("只有需要豁免的待确认计划才能登记豁免")
        if parse_utc(request.expires_at) <= self.clock.now():
            raise ValidationFailed("豁免失效时间必须晚于当前时刻")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO emergency_exemptions(exemption_id,plan_id,authorized_by,reason,"
                    "expires_at,created_at) VALUES(?,?,?,?,?,?)",
                    (
                        request.exemption_id,
                        plan_id,
                        actor_id,
                        request.reason,
                        request.expires_at,
                        self._now(),
                    ),
                )
                self.connection.execute(
                    "UPDATE commitment_plans SET exemption_id=?,revision=revision+1 WHERE plan_id=?",
                    (request.exemption_id, plan_id),
                )
                self._audit(
                    "plan",
                    plan_id,
                    "exemption.granted",
                    actor_id,
                    {
                        "exemption_id": request.exemption_id,
                        "authorized_by": actor_id,
                        "reason": request.reason,
                        "expires_at": request.expires_at,
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("豁免编号冲突") from exc
        return {"exemption_id": request.exemption_id, "plan_id": plan_id, "state": "recorded"}

    def _attached_exemption(self, plan: sqlite3.Row) -> sqlite3.Row | None:
        if plan["exemption_id"] is None:
            return None
        return self.connection.execute(
            "SELECT * FROM emergency_exemptions WHERE exemption_id=?",
            (plan["exemption_id"],),
        ).fetchone()

    # ------------------------------------------------------------------
    # 确认、执行与结算
    # ------------------------------------------------------------------

    def confirm_plan(self, actor_id: str, plan_id: str, expected_revision: int) -> dict[str, Any]:
        """一次性锁定通道与补偿余量；任何检查失败都不留下部分冻结。"""
        self._require(actor_id, "plan.confirm")
        plan = self._plan_row(plan_id)
        if plan["state"] != "pending_confirmation" or plan["revision"] != expected_revision:
            raise InvalidState("计划不是待确认的当前版本")
        boundary = self._boundary_row(plan["boundary_id"])
        if boundary["state"] != "published":
            raise InvalidState("计划基于的边界版本已被修订，等待重估")
        declaration = self._declaration_row(plan["declaration_id"])
        now = self.clock.now()
        if now > parse_utc(declaration["latest_response_at"]):
            raise InvalidState("已超过场站最迟响应时刻")
        exemption = self._attached_exemption(plan)
        exemption_used = False
        if plan["decision"] == "exemption_required":
            if exemption is None:
                raise InvalidState("计划需要紧急保供豁免才能确认")
            if parse_utc(exemption["expires_at"]) <= now:
                raise InvalidState("紧急保供豁免已失效")
            confirm_level = Decimal(plan["requested_mw"])
            exemption_used = True
        else:
            confirm_level = Decimal(plan["committed_mw"])
        required_compensation = quantize_mw(confirm_level * COMPENSATION_RATIO)
        windows = self._boundary_windows(boundary)
        overlapping = overlapping_maintenance(
            windows, parse_utc(plan["window_start"]), parse_utc(plan["window_end"])
        )
        effective = quantize_mw(
            min(
                Decimal(boundary["channel_capacity_mw"]),
                Decimal(boundary["cable_thermal_limit_mw"]),
            )
            * maintenance_capacity_percent(overlapping)
            / Decimal("100")
        )
        with transaction(self.connection, immediate=True):
            current = self.connection.execute(
                "SELECT state,revision FROM commitment_plans WHERE plan_id=?", (plan_id,)
            ).fetchone()
            if current["state"] != "pending_confirmation" or current["revision"] != expected_revision:
                raise InvalidState("计划不是待确认的当前版本")
            if not exemption_used:
                held = self.connection.execute(
                    "SELECT locked_channel_mw,locked_compensation_mvar FROM capacity_locks "
                    "WHERE corridor_id=? AND state='held' AND window_start<? AND window_end>?",
                    (plan["corridor_id"], plan["window_end"], plan["window_start"]),
                ).fetchall()
                used_channel = sum((Decimal(row["locked_channel_mw"]) for row in held), Decimal("0"))
                used_compensation = sum(
                    (Decimal(row["locked_compensation_mvar"]) for row in held), Decimal("0")
                )
                if used_channel + confirm_level > effective:
                    raise Conflict("送出通道余量不足，未冻结任何资源")
                if used_compensation + required_compensation > Decimal(
                    boundary["reactive_compensation_mvar"]
                ):
                    raise Conflict("无功补偿余量不足，未冻结任何资源")
            self.connection.execute(
                "INSERT INTO capacity_locks(plan_id,corridor_id,window_start,window_end,"
                "locked_channel_mw,locked_compensation_mvar,state,created_at) "
                "VALUES(?,?,?,?,?,?, 'held',?)",
                (
                    plan_id,
                    plan["corridor_id"],
                    plan["window_start"],
                    plan["window_end"],
                    decimal_text(quantize_mw(confirm_level)),
                    decimal_text(required_compensation),
                    self._now(),
                ),
            )
            updated = self.connection.execute(
                "UPDATE commitment_plans SET state='confirmed',confirmed_at=?,revision=revision+1 "
                "WHERE plan_id=? AND state='pending_confirmation' AND revision=?",
                (self._now(), plan_id, expected_revision),
            )
            if updated.rowcount != 1:
                raise InvalidState("计划不是待确认的当前版本")
            self._audit(
                "plan",
                plan_id,
                "plan.confirmed",
                actor_id,
                {
                    "confirm_level_mw": decimal_text(quantize_mw(confirm_level)),
                    "locked_compensation_mvar": decimal_text(required_compensation),
                    "exemption_used": exemption_used,
                    "exemption_id": None if exemption is None else exemption["exemption_id"],
                },
            )
        return {
            "plan_id": plan_id,
            "state": "confirmed",
            "confirm_level_mw": decimal_text(quantize_mw(confirm_level)),
            "exemption_used": exemption_used,
            "revision": expected_revision + 1,
        }

    def cancel_plan(self, actor_id: str, plan_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "plan.cancel")
        plan = self._plan_row(plan_id)
        if plan["state"] not in ("pending_confirmation", "confirmed"):
            raise InvalidState("当前状态不可取消")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE capacity_locks SET state='released',released_at=? "
                "WHERE plan_id=? AND state='held'",
                (self._now(), plan_id),
            )
            updated = self.connection.execute(
                "UPDATE commitment_plans SET state='cancelled',revision=revision+1 "
                "WHERE plan_id=? AND state IN ('pending_confirmation','confirmed') AND revision=?",
                (plan_id, expected_revision),
            )
            if updated.rowcount != 1:
                raise InvalidState("计划不是可取消的当前版本")
            self._audit("plan", plan_id, "plan.cancelled", actor_id, {})
        return {"plan_id": plan_id, "state": "cancelled", "revision": expected_revision + 1}

    def begin_execution(self, actor_id: str, plan_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "execution.write")
        plan = self._plan_row(plan_id)
        if plan["state"] != "confirmed":
            raise InvalidState("只有已确认计划可以进入执行")
        if self.clock.now() < parse_utc(plan["window_start"]):
            raise InvalidState("尚未进入计划执行窗口")
        with transaction(self.connection, immediate=True):
            updated = self.connection.execute(
                "UPDATE commitment_plans SET state='executing',revision=revision+1 "
                "WHERE plan_id=? AND state='confirmed' AND revision=?",
                (plan_id, expected_revision),
            )
            if updated.rowcount != 1:
                raise InvalidState("计划不是已确认的当前版本")
            self._audit("plan", plan_id, "plan.execution_started", actor_id, {})
        return {"plan_id": plan_id, "state": "executing", "revision": expected_revision + 1}

    def submit_receipt(self, actor_id: str, plan_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """执行回执：按实际完成量结算并释放冻结的通道与补偿余量。"""
        self._require(actor_id, "receipt.write")
        request = ReceiptRequest.from_dict(raw)
        plan = self._plan_row(plan_id)
        if plan["state"] != "executing":
            raise InvalidState("只有执行中的计划可以登记回执")
        planned = Decimal(plan["planned_mwh"])
        actual = quantize_mw(request.actual_mwh)
        variance = quantize_mw(actual - planned)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO execution_receipts(receipt_id,plan_id,actual_mwh,planned_mwh,"
                    "variance_mwh,received_by,received_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        request.receipt_id,
                        plan_id,
                        decimal_text(actual),
                        decimal_text(quantize_mw(planned)),
                        decimal_text(variance),
                        actor_id,
                        self._now(),
                    ),
                )
                self.connection.execute(
                    "UPDATE capacity_locks SET state='released',released_at=? "
                    "WHERE plan_id=? AND state='held'",
                    (self._now(), plan_id),
                )
                updated = self.connection.execute(
                    "UPDATE commitment_plans SET state='settled',settled_at=?,revision=revision+1 "
                    "WHERE plan_id=? AND state='executing'",
                    (self._now(), plan_id),
                )
                if updated.rowcount != 1:
                    raise InvalidState("计划不在执行中")
                self._audit(
                    "plan",
                    plan_id,
                    "plan.settled",
                    actor_id,
                    {
                        "receipt_id": request.receipt_id,
                        "actual_mwh": decimal_text(actual),
                        "planned_mwh": decimal_text(quantize_mw(planned)),
                        "variance_mwh": decimal_text(variance),
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("回执编号冲突或计划已结算") from exc
        return {
            "receipt_id": request.receipt_id,
            "plan_id": plan_id,
            "state": "settled",
            "actual_mwh": decimal_text(actual),
            "planned_mwh": decimal_text(quantize_mw(planned)),
            "variance_mwh": decimal_text(variance),
        }

    # ------------------------------------------------------------------
    # 后台查询与审计
    # ------------------------------------------------------------------

    def plan_explanation(self, actor_id: str, plan_id: str) -> dict[str, Any]:
        """说明计划为何获批、降额或需要豁免。"""
        self._require(actor_id, "report.read")
        plan = self._plan_row(plan_id)
        boundary = self._boundary_row(plan["boundary_id"])
        declaration = self._declaration_row(plan["declaration_id"])
        decision = json.loads(plan["decision_json"])
        exemption_row = self._attached_exemption(plan)
        exemption = None
        if exemption_row is not None:
            exemption = {
                "exemption_id": exemption_row["exemption_id"],
                "authorized_by": exemption_row["authorized_by"],
                "reason": exemption_row["reason"],
                "expires_at": exemption_row["expires_at"],
                "expired": parse_utc(exemption_row["expires_at"]) <= self.clock.now(),
            }
        lock_row = self.connection.execute(
            "SELECT * FROM capacity_locks WHERE plan_id=?", (plan_id,)
        ).fetchone()
        lock = None
        if lock_row is not None:
            lock = {
                "locked_channel_mw": lock_row["locked_channel_mw"],
                "locked_compensation_mvar": lock_row["locked_compensation_mvar"],
                "state": lock_row["state"],
            }
        receipt_row = self.connection.execute(
            "SELECT * FROM execution_receipts WHERE plan_id=?", (plan_id,)
        ).fetchone()
        settlement = None
        if receipt_row is not None:
            settlement = {
                "receipt_id": receipt_row["receipt_id"],
                "actual_mwh": receipt_row["actual_mwh"],
                "planned_mwh": receipt_row["planned_mwh"],
                "variance_mwh": receipt_row["variance_mwh"],
            }
        return {
            "plan_id": plan_id,
            "corridor_id": plan["corridor_id"],
            "station_id": plan["station_id"],
            "state": plan["state"],
            "decision": plan["decision"],
            "requested_mw": plan["requested_mw"],
            "committed_mw": plan["committed_mw"],
            "planned_mwh": plan["planned_mwh"],
            "boundary": {
                "boundary_id": boundary["boundary_id"],
                "version": boundary["version"],
                "effective_from": boundary["effective_from"],
                "effective_to": boundary["effective_to"],
                "state": boundary["state"],
            },
            "declaration": {
                "declaration_id": declaration["declaration_id"],
                "installed_capacity_mw": declaration["installed_capacity_mw"],
                "availability_percent": declaration["availability_percent"],
                "reserve_mw": declaration["reserve_mw"],
                "latest_response_at": declaration["latest_response_at"],
            },
            "constraints": decision["constraints"],
            "reasons": decision["reasons"],
            "exemption": exemption,
            "lock": lock,
            "settlement": settlement,
        }

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM gate_audit_events ORDER BY event_id").fetchall()
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
            calculated = hashlib.sha256(_canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
