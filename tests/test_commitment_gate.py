from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from commitment_gate.api import JsonApplication
from commitment_gate.clock import FrozenClock
from commitment_gate.errors import Conflict, Forbidden, InvalidState, ValidationFailed
from commitment_gate.gating import build_phase_schedule, evaluate_commitment
from commitment_gate.models import MaintenanceWindow, RampPoint
from commitment_gate.service import CommitmentGateService
from commitment_gate.storage import connect


RAMP = [
    {"offset_minutes": 0, "output_percent": "0"},
    {"offset_minutes": 30, "output_percent": "50"},
    {"offset_minutes": 60, "output_percent": "100"},
]


def boundary_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "boundary_id": "bnd-1",
        "corridor_id": "cor-1",
        "version": 1,
        "effective_from": "2026-09-27T00:00:00Z",
        "effective_to": "2026-09-28T00:00:00Z",
        "channel_capacity_mw": "800",
        "reactive_compensation_mvar": "300",
        "cable_thermal_limit_mw": "750",
        "maintenance_windows": [
            {"starts_at": "2026-09-27T10:00:00Z", "ends_at": "2026-09-27T12:00:00Z",
             "capacity_percent": "50", "reason": "海缆年检"},
            {"starts_at": "2026-09-27T20:00:00Z", "ends_at": "2026-09-27T23:00:00Z",
             "capacity_percent": "0", "reason": "海缆故障隔离演练"},
        ],
    }
    payload.update(overrides)
    return payload


def declaration_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "declaration_id": "decl-1",
        "corridor_id": "cor-1",
        "station_id": "st-1",
        "installed_capacity_mw": "600",
        "availability_percent": "90",
        "ramp_curve": RAMP,
        "reserve_mw": "50",
        "latest_response_at": "2026-09-27T18:00:00Z",
    }
    payload.update(overrides)
    return payload


def plan_payload(plan_id: str, window_start: str, window_end: str, requested: str) -> dict[str, object]:
    return {
        "plan_id": plan_id,
        "corridor_id": "cor-1",
        "station_id": "st-1",
        "window_start": window_start,
        "window_end": window_end,
        "requested_mw": requested,
    }


class GatingTests(unittest.TestCase):
    def evaluate(self, **overrides: object) -> object:
        kwargs = {
            "requested_mw": Decimal("500"),
            "installed_capacity_mw": Decimal("600"),
            "availability_percent": Decimal("90"),
            "reserve_mw": Decimal("50"),
            "channel_capacity_mw": Decimal("800"),
            "cable_thermal_limit_mw": Decimal("750"),
            "reactive_compensation_mvar": Decimal("300"),
            "maintenance_percent": Decimal("100"),
            "overlapping_windows": [],
        }
        kwargs.update(overrides)
        return evaluate_commitment(**kwargs)

    def test_approved_when_all_constraints_pass(self) -> None:
        result = self.evaluate()
        self.assertEqual(result.decision, "approved")
        self.assertEqual(result.committed_mw, Decimal("500.000"))

    def test_derated_by_reactive_compensation(self) -> None:
        result = self.evaluate(reactive_compensation_mvar=Decimal("200"))
        self.assertEqual(result.decision, "derated")
        self.assertEqual(result.committed_mw, Decimal("400.000"))
        compensation = [c for c in result.constraints if c["name"] == "reactive_compensation"][0]
        self.assertTrue(compensation["binding"])

    def test_derated_by_maintenance_and_reserve(self) -> None:
        window = MaintenanceWindow("2026-09-27T10:00:00Z", "2026-09-27T12:00:00Z", Decimal("50"), "年检")
        result = self.evaluate(maintenance_percent=Decimal("50"), overlapping_windows=[window])
        self.assertEqual(result.decision, "derated")
        self.assertEqual(result.committed_mw, Decimal("325.000"))
        self.assertTrue(any("检修窗口" in reason for reason in result.reasons))

    def test_exemption_required_when_capacity_is_zero(self) -> None:
        result = self.evaluate(maintenance_percent=Decimal("0"))
        self.assertEqual(result.decision, "exemption_required")
        self.assertEqual(result.committed_mw, Decimal("0.000"))

    def test_phase_schedule_integrates_planned_energy(self) -> None:
        phases, planned_mwh = build_phase_schedule(
            window_start=datetime(2026, 9, 27, 2, 0, tzinfo=timezone.utc),
            window_end=datetime(2026, 9, 27, 8, 0, tzinfo=timezone.utc),
            committed_mw=Decimal("500"),
            ramp_curve=[RampPoint(0, Decimal("0")), RampPoint(30, Decimal("50")), RampPoint(60, Decimal("100"))],
        )
        self.assertEqual([phase["phase"] for phase in phases],
                         ["preparation", "grid_connection", "ramping", "steady_operation"])
        self.assertEqual(phases[2]["starts_at"], "2026-09-27T02:45:00Z")
        self.assertEqual(phases[3]["starts_at"], "2026-09-27T03:45:00Z")
        self.assertEqual(planned_mwh, Decimal("2375.000"))

    def test_phase_schedule_rejects_short_window(self) -> None:
        with self.assertRaises(ValueError):
            build_phase_schedule(
                window_start=datetime(2026, 9, 27, 2, 0, tzinfo=timezone.utc),
                window_end=datetime(2026, 9, 27, 3, 0, tzinfo=timezone.utc),
                committed_mw=Decimal("500"),
                ramp_curve=[RampPoint(0, Decimal("0")), RampPoint(60, Decimal("100"))],
            )


class GateServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc))
        self.service = CommitmentGateService(self.connection, self.clock)
        for user_id, role in (
            ("dispatch", "dispatcher"),
            ("station", "station"),
            ("coord", "coordinator"),
            ("audit", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.service.publish_boundary("dispatch", boundary_payload())
        self.service.submit_declaration("station", declaration_payload())

    def tearDown(self) -> None:
        self.connection.close()

    def locks(self, plan_id: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM capacity_locks WHERE plan_id=?", (plan_id,)
        ).fetchall()

    def test_approved_plan_full_lifecycle(self) -> None:
        created = self.service.create_plan("coord", plan_payload(
            "plan-1", "2026-09-27T02:00:00Z", "2026-09-27T08:00:00Z", "500"))
        self.assertEqual(created["decision"], "approved")
        self.assertEqual(created["planned_mwh"], "2375.000")
        confirmed = self.service.confirm_plan("coord", "plan-1", 1)
        self.assertEqual(confirmed["state"], "confirmed")
        self.assertFalse(confirmed["exemption_used"])
        lock = self.locks("plan-1")[0]
        self.assertEqual(lock["locked_channel_mw"], "500.000")
        self.assertEqual(lock["locked_compensation_mvar"], "250.000")
        self.clock.advance(hours=19)  # 2026-09-27T03:00:00Z
        self.service.begin_execution("dispatch", "plan-1", 2)
        settled = self.service.submit_receipt("dispatch", "plan-1", {
            "receipt_id": "rcp-1", "actual_mwh": "2300"})
        self.assertEqual(settled["variance_mwh"], "-75.000")
        self.assertEqual(self.locks("plan-1")[0]["state"], "released")
        explanation = self.service.plan_explanation("coord", "plan-1")
        self.assertEqual(explanation["state"], "settled")
        self.assertEqual(explanation["settlement"]["actual_mwh"], "2300.000")
        with self.assertRaises(InvalidState):
            self.service.submit_receipt("dispatch", "plan-1", {
                "receipt_id": "rcp-2", "actual_mwh": "2300"})

    def test_derated_plan_explains_binding_constraints(self) -> None:
        created = self.service.create_plan("coord", plan_payload(
            "plan-1", "2026-09-27T09:00:00Z", "2026-09-27T16:00:00Z", "500"))
        self.assertEqual(created["decision"], "derated")
        self.assertEqual(created["committed_mw"], "325.000")
        explanation = self.service.plan_explanation("audit", "plan-1")
        maintenance = [c for c in explanation["constraints"] if c["name"] == "maintenance_derate"][0]
        self.assertTrue(maintenance["binding"])
        self.assertTrue(any("检修窗口" in reason for reason in explanation["reasons"]))
        self.assertTrue(any("备用" in reason for reason in explanation["reasons"]))

    def test_exemption_records_authorizer_reason_and_expiry(self) -> None:
        created = self.service.create_plan("coord", plan_payload(
            "plan-1", "2026-09-27T19:00:00Z", "2026-09-27T23:30:00Z", "200"))
        self.assertEqual(created["decision"], "exemption_required")
        with self.assertRaises(InvalidState):
            self.service.confirm_plan("coord", "plan-1", 1)
        with self.assertRaises(ValidationFailed):
            self.service.grant_exemption("coord", "plan-1", {
                "exemption_id": "exm-past", "reason": "已过期", "expires_at": "2026-09-26T07:00:00Z"})
        self.service.grant_exemption("coord", "plan-1", {
            "exemption_id": "exm-1", "reason": "晚高峰紧急保供", "expires_at": "2026-09-27T23:00:00Z"})
        confirmed = self.service.confirm_plan("coord", "plan-1", 2)
        self.assertTrue(confirmed["exemption_used"])
        self.assertEqual(confirmed["confirm_level_mw"], "200.000")
        explanation = self.service.plan_explanation("coord", "plan-1")
        self.assertEqual(explanation["exemption"]["authorized_by"], "coord")
        self.assertEqual(explanation["exemption"]["reason"], "晚高峰紧急保供")
        self.assertEqual(explanation["exemption"]["expires_at"], "2026-09-27T23:00:00Z")
        self.assertFalse(explanation["exemption"]["expired"])

    def test_expired_exemption_blocks_confirm(self) -> None:
        self.service.create_plan("coord", plan_payload(
            "plan-1", "2026-09-27T19:00:00Z", "2026-09-27T23:30:00Z", "200"))
        self.service.grant_exemption("coord", "plan-1", {
            "exemption_id": "exm-1", "reason": "临时保供", "expires_at": "2026-09-27T10:00:00Z"})
        self.clock.advance(hours=27)  # 2026-09-27T11:00:00Z
        with self.assertRaises(InvalidState):
            self.service.confirm_plan("coord", "plan-1", 2)

    def test_exemption_only_for_blocked_plans(self) -> None:
        self.service.create_plan("coord", plan_payload(
            "plan-1", "2026-09-27T02:00:00Z", "2026-09-27T08:00:00Z", "500"))
        with self.assertRaises(InvalidState):
            self.service.grant_exemption("coord", "plan-1", {
                "exemption_id": "exm-1", "reason": "不需要", "expires_at": "2026-09-27T23:00:00Z"})

    def test_late_revision_reevaluates_only_unconfirmed_plans(self) -> None:
        derated = self.service.create_plan("coord", plan_payload(
            "plan-1", "2026-09-27T09:00:00Z", "2026-09-27T16:00:00Z", "500"))
        self.assertEqual(derated["decision"], "derated")
        self.service.create_plan("coord", plan_payload(
            "plan-2", "2026-09-27T02:00:00Z", "2026-09-27T08:00:00Z", "500"))
        self.service.confirm_plan("coord", "plan-2", 1)
        revised = self.service.publish_boundary("dispatch", boundary_payload(
            boundary_id="bnd-2", version=2, maintenance_windows=[]))
        self.assertEqual(revised["reevaluated_plan_ids"], ["plan-1"])
        reevaluated = self.service.plan("plan-1")
        self.assertEqual(reevaluated["decision"], "approved")
        self.assertEqual(reevaluated["committed_mw"], "500.000")
        self.assertEqual(reevaluated["boundary_id"], "bnd-2")
        self.assertEqual(reevaluated["revision"], 2)
        confirmed = self.service.plan("plan-2")
        self.assertEqual(confirmed["boundary_id"], "bnd-1")
        self.assertEqual(confirmed["state"], "confirmed")
        self.service.confirm_plan("coord", "plan-1", 2)

    def test_boundary_version_must_increase(self) -> None:
        with self.assertRaises(Conflict):
            self.service.publish_boundary("dispatch", boundary_payload(boundary_id="bnd-1b", version=1))

    def test_failed_confirm_leaves_no_partial_freeze(self) -> None:
        self.service.create_plan("coord", plan_payload(
            "plan-big", "2026-09-27T02:00:00Z", "2026-09-27T08:00:00Z", "600"))
        self.service.confirm_plan("coord", "plan-big", 1)
        self.service.create_plan("coord", plan_payload(
            "plan-small", "2026-09-27T02:00:00Z", "2026-09-27T08:00:00Z", "100"))
        with self.assertRaises(Conflict):
            self.service.confirm_plan("coord", "plan-small", 1)  # 补偿余量不足
        self.assertEqual(self.locks("plan-small"), [])
        self.assertEqual(self.service.plan("plan-small")["state"], "pending_confirmation")
        self.assertEqual(self.locks("plan-big")[0]["state"], "held")
        self.service.create_plan("coord", plan_payload(
            "plan-wide", "2026-09-27T02:00:00Z", "2026-09-27T08:00:00Z", "300"))
        with self.assertRaises(Conflict):
            self.service.confirm_plan("coord", "plan-wide", 1)  # 通道余量不足
        self.assertEqual(self.locks("plan-wide"), [])
        self.assertEqual(self.service.plan("plan-wide")["state"], "pending_confirmation")

    def test_confirm_after_latest_response_rejected(self) -> None:
        self.service.create_plan("coord", plan_payload(
            "plan-1", "2026-09-27T02:00:00Z", "2026-09-27T08:00:00Z", "500"))
        self.clock.advance(hours=35)  # 2026-09-27T19:00:00Z
        with self.assertRaises(InvalidState):
            self.service.confirm_plan("coord", "plan-1", 1)

    def test_confirm_under_superseded_boundary_rejected(self) -> None:
        self.service.create_plan("coord", plan_payload(
            "plan-1", "2026-09-27T02:00:00Z", "2026-09-27T08:00:00Z", "500"))
        self.service.publish_boundary("dispatch", boundary_payload(
            boundary_id="bnd-2", version=2,
            effective_from="2026-09-28T00:00:00Z", effective_to="2026-09-29T00:00:00Z",
            maintenance_windows=[]))
        with self.assertRaises(InvalidState):
            self.service.confirm_plan("coord", "plan-1", 1)

    def test_cancel_releases_lock(self) -> None:
        self.service.create_plan("coord", plan_payload(
            "plan-1", "2026-09-27T02:00:00Z", "2026-09-27T08:00:00Z", "500"))
        self.service.confirm_plan("coord", "plan-1", 1)
        self.service.cancel_plan("coord", "plan-1", 2)
        self.assertEqual(self.locks("plan-1")[0]["state"], "released")
        self.service.create_plan("coord", plan_payload(
            "plan-2", "2026-09-27T02:00:00Z", "2026-09-27T08:00:00Z", "500"))
        self.service.confirm_plan("coord", "plan-2", 1)

    def test_window_too_short_for_phases(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.create_plan("coord", plan_payload(
                "plan-1", "2026-09-27T02:00:00Z", "2026-09-27T02:45:00Z", "100"))

    def test_declaration_requires_monotonic_full_ramp_curve(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.submit_declaration("station", declaration_payload(
                declaration_id="decl-bad-1",
                ramp_curve=[{"offset_minutes": 10, "output_percent": "0"},
                            {"offset_minutes": 60, "output_percent": "100"}]))
        with self.assertRaises(ValidationFailed):
            self.service.submit_declaration("station", declaration_payload(
                declaration_id="decl-bad-2",
                ramp_curve=[{"offset_minutes": 0, "output_percent": "0"},
                            {"offset_minutes": 60, "output_percent": "80"}]))

    def test_permissions_are_enforced(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.publish_boundary("station", boundary_payload(boundary_id="bnd-9", version=2))
        with self.assertRaises(Forbidden):
            self.service.create_plan("dispatch", plan_payload(
                "plan-1", "2026-09-27T02:00:00Z", "2026-09-27T08:00:00Z", "100"))
        with self.assertRaises(Forbidden):
            self.service.pending_plans("dispatch")

    def test_audit_chain_detects_tampering(self) -> None:
        self.service.create_plan("coord", plan_payload(
            "plan-1", "2026-09-27T02:00:00Z", "2026-09-27T08:00:00Z", "500"))
        self.assertTrue(self.service.audit_chain("audit")["valid"])
        self.connection.execute("UPDATE gate_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("audit")["valid"])


class RestartRecoveryTests(unittest.TestCase):
    def test_pending_confirmation_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gate.sqlite3"
            clock = FrozenClock(datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc))
            first = CommitmentGateService(connect(path), clock)
            for user_id, role in (("dispatch", "dispatcher"), ("station", "station"), ("coord", "coordinator")):
                first.create_user(user_id, user_id, role)
            first.publish_boundary("dispatch", boundary_payload())
            first.submit_declaration("station", declaration_payload())
            first.create_plan("coord", plan_payload(
                "plan-1", "2026-09-27T02:00:00Z", "2026-09-27T08:00:00Z", "500"))
            first.connection.close()

            second = CommitmentGateService(connect(path), clock)
            pending = second.pending_plans("coord")["pending"]
            self.assertEqual([item["plan_id"] for item in pending], ["plan-1"])
            self.assertEqual(pending[0]["decision"], "approved")
            confirmed = second.confirm_plan("coord", "plan-1", 1)
            self.assertEqual(confirmed["state"], "confirmed")
            explanation = second.plan_explanation("coord", "plan-1")
            self.assertEqual(explanation["lock"]["locked_channel_mw"], "500.000")
            second.connection.close()


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc))
        self.service = CommitmentGateService(self.connection, self.clock)
        self.app = JsonApplication(self.service)
        for user_id, role in (("dispatch", "dispatcher"), ("station", "station"), ("coord", "coordinator")):
            self.service.create_user(user_id, user_id, role)

    def tearDown(self) -> None:
        self.connection.close()

    def post(self, path: str, actor: str, payload: dict[str, object]):
        import json
        return self.app.handle("POST", path, {"X-Actor-Id": actor}, json.dumps(payload).encode("utf-8"))

    def test_boundary_plan_and_pending_flow(self) -> None:
        self.assertEqual(self.app.handle("GET", "/health").status, 200)
        response = self.post("/boundaries", "dispatch", boundary_payload())
        self.assertEqual(response.status, 201)
        self.assertEqual(self.app.handle("GET", "/boundaries/cor-1", {"X-Actor-Id": "coord"}).status, 200)
        self.assertEqual(self.post("/declarations", "station", declaration_payload()).status, 201)
        created = self.post("/plans", "coord", plan_payload(
            "plan-1", "2026-09-27T02:00:00Z", "2026-09-27T08:00:00Z", "500"))
        self.assertEqual(created.status, 201)
        self.assertEqual(created.body["decision"], "approved")
        pending = self.app.handle("GET", "/plans/pending", {"X-Actor-Id": "coord"})
        self.assertEqual([item["plan_id"] for item in pending.body["pending"]], ["plan-1"])
        confirmed = self.post("/plans/plan-1/confirm", "coord", {"expected_revision": 1})
        self.assertEqual(confirmed.status, 200)
        explanation = self.app.handle("GET", "/plans/plan-1/explanation", {"X-Actor-Id": "coord"})
        self.assertEqual(explanation.body["lock"]["state"], "held")

    def test_missing_actor_and_unknown_route(self) -> None:
        response = self.app.handle("POST", "/boundaries", {}, b"{}")
        self.assertEqual(response.status, 422)
        response = self.app.handle("GET", "/no-such-route", {"X-Actor-Id": "coord"})
        self.assertEqual(response.status, 404)
        forbidden = self.post("/boundaries", "station", boundary_payload())
        self.assertEqual(forbidden.status, 403)


if __name__ == "__main__":
    unittest.main()
