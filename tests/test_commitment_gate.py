from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from wind_dispatch.acceptance import run as acceptance_run
from wind_dispatch.api import JsonApplication
from wind_dispatch.clock import FrozenClock
from wind_dispatch.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from wind_dispatch.service import SupplyService
from wind_dispatch.storage import connect


ROOT = Path(__file__).resolve().parents[1]


class CommitmentGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = SupplyService(self.connection, self.clock)
        for user_id, role in (
            ("plan", "planner"),
            ("dispatch", "dispatcher"),
            ("station", "station"),
            ("risk", "risk"),
            ("audit", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.service.create_facility("plan", {"facility_id": "fanshi-one", "name": "北部海上风电场", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_mwh": "500000"})
        self.service.create_facility("plan", {"facility_id": "fanshi-two", "name": "帆石二场", "kind": "offshore-station", "timezone": "Asia/Shanghai", "capacity_mwh": "800000"})
        self.service.create_route("plan", {"route_id": "fanshi-export", "origin_id": "fanshi-one", "destination_id": "fanshi-two", "product": "turbine-18mw", "daily_capacity": "100000", "loss_basis_points": 25, "transit_hours": 36})

    def tearDown(self) -> None:
        self.connection.close()

    def boundary(self, **overrides: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "route_id": "fanshi-export",
            "service_date": "2026-09-26",
            "version": 1,
            "channel_capacity_mw": "500",
            "cable_thermal_limit_mw": "480",
            "compensation_mvar": "200",
            "compensation_ratio": "0.3",
            "effective_from": "2026-09-26T00:00:00Z",
            "effective_until": "2026-09-27T00:00:00Z",
        }
        payload.update(overrides)
        return self.service.publish_boundary("dispatch", payload)

    def declare(self, **overrides: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "declaration_id": "decl-1",
            "route_id": "fanshi-export",
            "service_date": "2026-09-26",
            "installed_mw": "400",
            "availability_percent": "90",
            "ramp_points": [
                {"offset_minutes": 60, "mw": "100"},
                {"offset_minutes": 240, "mw": "200"},
                {"offset_minutes": 300, "mw": "320"},
            ],
            "reserve_mw": "40",
            "respond_by": "2026-09-25T12:00:00Z",
            "idempotency_key": "decl-key-1",
        }
        payload.update(overrides)
        return self.service.submit_declaration("station", payload)

    def plan_id_of(self, declaration: dict[str, object]) -> str:
        plan = declaration["plan"]
        assert isinstance(plan, dict)
        return str(plan["plan_id"])

    def lock_count(self, plan_id: str) -> int:
        return self.connection.execute(
            "SELECT count(*) FROM commitment_locks WHERE plan_id=?", (plan_id,)
        ).fetchone()[0]

    def test_boundary_versions_must_increase(self) -> None:
        first = self.boundary()
        self.assertEqual(first["version"], 1)
        with self.assertRaises(Conflict):
            self.boundary()
        with self.assertRaises(Conflict):
            self.boundary(version=1)
        second = self.boundary(version=2, channel_capacity_mw="520")
        self.assertEqual(second["version"], 2)
        with self.assertRaises(ValidationFailed):
            self.boundary(version=3, effective_until="2026-09-26T00:00:00Z")

    def test_declaration_forms_four_phases(self) -> None:
        self.boundary()
        declaration = self.declare()
        self.assertEqual(declaration["state"], "planned")
        detail = self.service.commitment_plan("audit", self.plan_id_of(declaration))
        self.assertEqual(detail["outcome"], "approved")
        self.assertEqual(detail["committed_mw"], "320.000")
        phases = {phase["phase"]: phase for phase in detail["phases"]}
        self.assertEqual(
            list(phases), ["preparation", "grid_connection", "ramping", "stable_operation"]
        )
        self.assertEqual(phases["preparation"]["committed_mw"], "0.000")
        self.assertEqual(phases["preparation"]["ends_at"], "2026-09-26T01:00:00Z")
        self.assertEqual(phases["grid_connection"]["committed_mwh"], "300.000")
        self.assertEqual(phases["ramping"]["committed_mwh"], "200.000")
        self.assertEqual(phases["stable_operation"]["committed_mw"], "320.000")
        self.assertEqual(phases["stable_operation"]["committed_mwh"], "6080.000")
        self.assertEqual(phases["stable_operation"]["ends_at"], "2026-09-27T00:00:00Z")
        codes = [reason["code"] for reason in detail["evaluation"]["reasons"]]
        self.assertEqual(codes, ["WITHIN_ENVELOPE"])

    def test_availability_caps_requested_power(self) -> None:
        self.boundary()
        declaration = self.declare(availability_percent="50")
        detail = self.service.commitment_plan("audit", self.plan_id_of(declaration))
        self.assertEqual(detail["outcome"], "approved")
        self.assertEqual(detail["evaluation"]["capability_mw"], "200.000")
        self.assertEqual(detail["committed_mw"], "200.000")
        codes = [reason["code"] for reason in detail["evaluation"]["reasons"]]
        self.assertIn("CAPABILITY_CAPPED", codes)

    def test_reserve_headroom_derates_commitment(self) -> None:
        self.boundary()
        declaration = self.declare(reserve_mw="460")
        detail = self.service.commitment_plan("audit", self.plan_id_of(declaration))
        self.assertEqual(detail["outcome"], "derated")
        self.assertEqual(detail["committed_mw"], "20.000")
        codes = [reason["code"] for reason in detail["evaluation"]["reasons"]]
        self.assertIn("THERMAL_LIMIT", codes)
        self.assertIn("RESERVE_HEADROOM", codes)

    def test_maintenance_outage_merges_into_gate(self) -> None:
        self.service.announce_outage(
            "risk", "fanshi-export", "2026-09-26T00:00:00Z", "2026-09-27T00:00:00Z", "50", "海缆检修"
        )
        self.boundary()
        declaration = self.declare()
        detail = self.service.commitment_plan("audit", self.plan_id_of(declaration))
        self.assertEqual(detail["outcome"], "derated")
        self.assertEqual(detail["evaluation"]["channel_ceiling_mw"], "240.000")
        self.assertEqual(detail["committed_mw"], "200.000")
        codes = [reason["code"] for reason in detail["evaluation"]["reasons"]]
        self.assertIn("OUTAGE_DERATE", codes)

    def test_compensation_margin_can_bind(self) -> None:
        self.boundary(compensation_mvar="60")
        declaration = self.declare()
        detail = self.service.commitment_plan("audit", self.plan_id_of(declaration))
        self.assertEqual(detail["outcome"], "derated")
        self.assertEqual(detail["committed_mw"], "160.000")
        self.assertEqual(detail["evaluation"]["binding_constraint"], "COMPENSATION_LIMIT")

    def test_exemption_records_authorizer_reason_and_expiry(self) -> None:
        self.boundary()
        declaration = self.declare(reserve_mw="490")
        plan_id = self.plan_id_of(declaration)
        detail = self.service.commitment_plan("audit", plan_id)
        self.assertEqual(detail["outcome"], "exemption_required")
        self.assertTrue(detail["requires_exemption"])
        with self.assertRaises(InvalidState):
            self.service.confirm_plan("dispatch", plan_id)
        with self.assertRaises(Forbidden):
            self.service.grant_exemption(
                "dispatch", {"plan_id": plan_id, "reason": "越权尝试", "expires_at": "2026-09-25T00:00:00Z"}
            )
        granted = self.service.grant_exemption(
            "risk", {"plan_id": plan_id, "reason": "台风过境紧急保供", "expires_at": "2026-09-27T00:00:00Z"}
        )
        self.assertEqual(granted["authorized_by"], "risk")
        confirmed = self.service.confirm_plan("dispatch", plan_id)
        self.assertEqual(confirmed["exemption_id"], granted["exemption_id"])
        detail = self.service.commitment_plan("audit", plan_id)
        self.assertEqual(detail["state"], "confirmed")
        self.assertEqual(detail["exemptions"][0]["reason"], "台风过境紧急保供")
        self.assertEqual(detail["exemptions"][0]["expires_at"], "2026-09-27T00:00:00Z")
        self.assertTrue(detail["exemptions"][0]["active"])
        self.assertEqual(detail["locks"][0]["exemption_id"], granted["exemption_id"])
        events = [event["event_type"] for event in detail["decision_log"]]
        self.assertEqual(events, ["plan.formed", "exemption.granted", "plan.confirmed"])

    def test_expired_exemption_does_not_unlock_confirmation(self) -> None:
        self.boundary()
        declaration = self.declare(reserve_mw="490")
        plan_id = self.plan_id_of(declaration)
        self.service.grant_exemption(
            "risk", {"plan_id": plan_id, "reason": "短时保供", "expires_at": "2026-09-24T09:00:00Z"}
        )
        self.clock.advance(hours=2)
        with self.assertRaises(InvalidState):
            self.service.confirm_plan("dispatch", plan_id)
        detail = self.service.commitment_plan("audit", plan_id)
        self.assertFalse(detail["exemptions"][0]["active"])

    def test_overdue_response_requires_exemption(self) -> None:
        self.boundary()
        declaration = self.declare(respond_by="2026-09-24T09:00:00Z")
        plan_id = self.plan_id_of(declaration)
        self.clock.advance(hours=2)
        detail = self.service.commitment_plan("audit", plan_id)
        self.assertTrue(detail["response_overdue"])
        self.assertTrue(detail["requires_exemption"])
        with self.assertRaises(InvalidState):
            self.service.confirm_plan("dispatch", plan_id)
        self.service.grant_exemption(
            "risk", {"plan_id": plan_id, "reason": "保供期间允许迟确认", "expires_at": "2026-09-27T00:00:00Z"}
        )
        confirmed = self.service.confirm_plan("dispatch", plan_id)
        self.assertEqual(confirmed["state"], "confirmed")

    def test_late_revision_only_affects_unconfirmed_plans(self) -> None:
        self.boundary()
        plan_a = self.plan_id_of(self.declare())
        plan_b = self.plan_id_of(
            self.declare(
                declaration_id="decl-2",
                idempotency_key="decl-key-2",
                installed_mw="700",
                availability_percent="100",
                ramp_points=[
                    {"offset_minutes": 30, "mw": "200"},
                    {"offset_minutes": 120, "mw": "400"},
                    {"offset_minutes": 180, "mw": "620"},
                ],
                reserve_mw="30",
            )
        )
        self.assertEqual(self.service.commitment_plan("audit", plan_b)["committed_mw"], "450.000")
        self.service.confirm_plan("dispatch", plan_a)
        revised = self.boundary(version=2, channel_capacity_mw="650", cable_thermal_limit_mw="640")
        self.assertEqual(revised["reevaluated_plan_ids"], [plan_b])
        confirmed_detail = self.service.commitment_plan("audit", plan_a)
        self.assertEqual(confirmed_detail["boundary"]["version"], 1)
        self.assertEqual(confirmed_detail["state"], "confirmed")
        pending_detail = self.service.commitment_plan("audit", plan_b)
        self.assertEqual(pending_detail["boundary"]["version"], 2)
        self.assertEqual(pending_detail["outcome"], "derated")
        # plan-a 仍持有 320 MW 通道锁，v2 余量 640-320=320，预留 30 备用后降额至 290
        self.assertEqual(pending_detail["committed_mw"], "290.000")
        events = [event["event_type"] for event in pending_detail["decision_log"]]
        self.assertEqual(events, ["plan.formed", "plan.reevaluated"])

    def test_declaration_waits_for_boundary(self) -> None:
        declaration = self.declare()
        self.assertEqual(declaration["state"], "awaiting_boundary")
        self.assertIsNone(declaration["plan"])
        published = self.boundary()
        self.assertEqual(published["formed_plan_ids"], ["plan-decl-1"])
        detail = self.service.commitment_plan("audit", "plan-decl-1")
        self.assertEqual(detail["state"], "pending")

    def test_declaration_idempotency_replay_and_conflict(self) -> None:
        self.boundary()
        first = self.declare()
        self.assertEqual(first, self.declare())
        with self.assertRaises(Conflict):
            self.declare(reserve_mw="41")

    def test_failed_confirmation_leaves_no_partial_lock(self) -> None:
        self.boundary()
        plan_a = self.plan_id_of(self.declare(reserve_mw="0", installed_mw="500", availability_percent="100",
                                              ramp_points=[{"offset_minutes": 60, "mw": "200"},
                                                           {"offset_minutes": 300, "mw": "400"}]))
        plan_b = self.plan_id_of(self.declare(declaration_id="decl-2", idempotency_key="decl-key-2",
                                              reserve_mw="0", installed_mw="500", availability_percent="100",
                                              ramp_points=[{"offset_minutes": 60, "mw": "200"},
                                                           {"offset_minutes": 300, "mw": "400"}]))
        self.service.confirm_plan("dispatch", plan_a)
        self.assertEqual(self.lock_count(plan_a), 1)
        with self.assertRaises(Conflict):
            self.service.confirm_plan("dispatch", plan_b)
        self.assertEqual(self.lock_count(plan_b), 0)
        detail = self.service.commitment_plan("audit", plan_b)
        self.assertEqual(detail["state"], "pending")
        self.assertEqual(detail["locks"], [])

    def test_compensation_shortage_also_blocks_atomically(self) -> None:
        self.boundary(compensation_mvar="50")
        plan_a = self.plan_id_of(self.declare(reserve_mw="0", installed_mw="200",
                                              ramp_points=[{"offset_minutes": 60, "mw": "50"},
                                                           {"offset_minutes": 300, "mw": "100"}]))
        plan_b = self.plan_id_of(self.declare(declaration_id="decl-2", idempotency_key="decl-key-2",
                                              reserve_mw="0", installed_mw="200",
                                              ramp_points=[{"offset_minutes": 60, "mw": "50"},
                                                           {"offset_minutes": 300, "mw": "100"}]))
        self.service.confirm_plan("dispatch", plan_a)
        with self.assertRaises(Conflict):
            self.service.confirm_plan("dispatch", plan_b)
        self.assertEqual(self.lock_count(plan_b), 0)
        held = self.connection.execute(
            "SELECT sum(CAST(compensation_mvar AS REAL)) FROM commitment_locks WHERE state='held'"
        ).fetchone()[0]
        self.assertEqual(held, 30.0)

    def test_receipt_settles_by_actual_quantity(self) -> None:
        self.boundary()
        plan_id = self.plan_id_of(self.declare())
        self.service.confirm_plan("dispatch", plan_id)
        payload = {
            "receipt_id": "receipt-1",
            "idempotency_key": "receipt-key-1",
            "phase_actuals": {"preparation": "0", "grid_connection": "100", "ramping": "190", "stable_operation": "310"},
        }
        receipt = self.service.submit_receipt("station", plan_id, payload)
        self.assertEqual(receipt["settled_mwh"], "6380.000")
        self.assertEqual(receipt["shortfall_mwh"], "200.000")
        self.assertEqual(receipt["committed_mwh"], "6580.000")
        detail = self.service.commitment_plan("audit", plan_id)
        self.assertEqual(detail["state"], "settled")
        self.assertEqual(detail["locks"][0]["state"], "released")
        self.assertIsNotNone(detail["locks"][0]["released_at"])
        self.assertEqual(detail["receipt"]["receipt_id"], "receipt-1")
        replay = self.service.submit_receipt("station", plan_id, payload)
        self.assertEqual(replay, receipt)
        changed = dict(payload, phase_actuals={"preparation": "0", "grid_connection": "100", "ramping": "195", "stable_operation": "310"})
        with self.assertRaises(Conflict):
            self.service.submit_receipt("station", plan_id, changed)
        with self.assertRaises(InvalidState):
            self.service.submit_receipt("station", plan_id, dict(payload, receipt_id="receipt-2", idempotency_key="receipt-key-2"))

    def test_over_delivery_is_recorded_not_settled(self) -> None:
        self.boundary()
        plan_id = self.plan_id_of(self.declare())
        self.service.confirm_plan("dispatch", plan_id)
        receipt = self.service.submit_receipt("station", plan_id, {
            "receipt_id": "receipt-1",
            "idempotency_key": "receipt-key-1",
            "phase_actuals": {"preparation": "0", "grid_connection": "120", "ramping": "260", "stable_operation": "400"},
        })
        self.assertEqual(receipt["settled_mwh"], "6580.000")
        self.assertEqual(receipt["shortfall_mwh"], "0.000")
        self.assertEqual(receipt["over_delivery_mwh"], "1640.000")

    def test_role_separation(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.publish_boundary("station", {"route_id": "fanshi-export"})
        self.boundary()
        with self.assertRaises(Forbidden):
            self.service.submit_declaration("dispatch", {"declaration_id": "decl-x"})
        plan_id = self.plan_id_of(self.declare())
        with self.assertRaises(Forbidden):
            self.service.confirm_plan("station", plan_id)
        with self.assertRaises(NotFound):
            self.service.list_plans("dispatch-unknown", state="pending")
        self.assertEqual(self.service.list_plans("audit", state="pending")["plans"][0]["plan_id"], plan_id)

    def test_pending_state_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "gate.sqlite3"
            connection = connect(database)
            clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
            service = SupplyService(connection, clock)
            service.create_user("dispatch", "调度", "dispatcher")
            service.create_user("station", "场站", "station")
            service.create_user("plan", "计划", "planner")
            service.create_facility("plan", {"facility_id": "fanshi-one", "name": "北部海上风电场", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_mwh": "500000"})
            service.create_facility("plan", {"facility_id": "fanshi-two", "name": "帆石二场", "kind": "offshore-station", "timezone": "Asia/Shanghai", "capacity_mwh": "800000"})
            service.create_route("plan", {"route_id": "fanshi-export", "origin_id": "fanshi-one", "destination_id": "fanshi-two", "product": "turbine-18mw", "daily_capacity": "100000", "loss_basis_points": 25, "transit_hours": 36})
            service.publish_boundary("dispatch", {"route_id": "fanshi-export", "service_date": "2026-09-26", "version": 1, "channel_capacity_mw": "500", "cable_thermal_limit_mw": "480", "compensation_mvar": "200", "compensation_ratio": "0.3", "effective_from": "2026-09-26T00:00:00Z", "effective_until": "2026-09-27T00:00:00Z"})
            declaration = service.submit_declaration("station", {"declaration_id": "decl-1", "route_id": "fanshi-export", "service_date": "2026-09-26", "installed_mw": "400", "availability_percent": "90", "ramp_points": [{"offset_minutes": 60, "mw": "100"}, {"offset_minutes": 240, "mw": "200"}, {"offset_minutes": 300, "mw": "320"}], "reserve_mw": "40", "respond_by": "2099-01-01T00:00:00Z", "idempotency_key": "decl-key-1"})
            plan_id = declaration["plan"]["plan_id"]
            connection.close()

            reopened = SupplyService(connect(database))
            pending = reopened.list_plans("dispatch", state="pending", route_id="fanshi-export")
            self.assertEqual([plan["plan_id"] for plan in pending["plans"]], [plan_id])
            detail = reopened.commitment_plan("dispatch", plan_id)
            self.assertEqual(detail["outcome"], "approved")
            confirmed = reopened.confirm_plan("dispatch", plan_id)
            self.assertEqual(confirmed["state"], "confirmed")
            self.assertEqual(reopened.list_plans("dispatch", state="pending")["plans"], [])
            reopened.connection.close()

    def test_api_exposes_commitment_gate(self) -> None:
        app = JsonApplication(self.service)
        headers = {"X-Actor-Id": "dispatch"}
        response = app.handle("POST", "/boundaries", headers, json.dumps({
            "route_id": "fanshi-export", "service_date": "2026-09-26", "version": 1,
            "channel_capacity_mw": "500", "cable_thermal_limit_mw": "480",
            "compensation_mvar": "200", "compensation_ratio": "0.3",
            "effective_from": "2026-09-26T00:00:00Z", "effective_until": "2026-09-27T00:00:00Z",
        }).encode())
        self.assertEqual(response.status, 201)
        response = app.handle("POST", "/declarations", {"X-Actor-Id": "station"}, json.dumps({
            "declaration_id": "decl-1", "route_id": "fanshi-export", "service_date": "2026-09-26",
            "installed_mw": "400", "availability_percent": "90",
            "ramp_points": [{"offset_minutes": 60, "mw": "100"}, {"offset_minutes": 240, "mw": "200"}, {"offset_minutes": 300, "mw": "320"}],
            "reserve_mw": "40", "respond_by": "2026-09-25T12:00:00Z", "idempotency_key": "decl-key-1",
        }).encode())
        self.assertEqual(response.status, 201)
        plan_id = response.body["plan"]["plan_id"]
        response = app.handle("GET", "/plans?state=pending&route_id=fanshi-export", {"X-Actor-Id": "audit"})
        self.assertEqual([plan["plan_id"] for plan in response.body["plans"]], [plan_id])
        response = app.handle("POST", f"/plans/{plan_id}/confirm", headers, b"{}")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["locked"]["channel_mw"], "320.000")
        response = app.handle("POST", f"/plans/{plan_id}/receipts", {"X-Actor-Id": "station"}, json.dumps({
            "receipt_id": "receipt-1", "idempotency_key": "receipt-key-1",
            "phase_actuals": {"preparation": "0", "grid_connection": "100", "ramping": "200", "stable_operation": "320"},
        }).encode())
        self.assertEqual(response.status, 201)
        self.assertEqual(response.body["settled_mwh"], "6580.000")
        response = app.handle("GET", f"/plans/{plan_id}", {"X-Actor-Id": "audit"})
        self.assertEqual(response.body["state"], "settled")
        response = app.handle("GET", f"/plans/{plan_id}", {"X-Actor-Id": "nobody"})
        self.assertEqual(response.status, 404)


class AcceptanceGateTests(unittest.TestCase):
    def test_acceptance_covers_gate_outcomes(self) -> None:
        result = acceptance_run(ROOT)
        gate = result["commitment_gate"]
        self.assertEqual(gate["boundary_versions"], [1, 2])
        self.assertEqual(gate["approved_plan"]["settled_mwh"], "6380.000")
        self.assertEqual(gate["approved_plan"]["shortfall_mwh"], "200.000")
        self.assertEqual(gate["exemption_plan"]["outcome"], "exemption_required")
        self.assertEqual(gate["exemption_plan"]["authorized_by"], "risk")
        self.assertEqual(gate["derated_plan"]["outcome"], "derated")
        self.assertEqual(gate["derated_plan"]["boundary_version"], 2)
        self.assertEqual(gate["derated_plan"]["committed_mw"], "610.000")
        self.assertEqual(gate["pending_after_restart_view"][0]["state"], "pending")
        self.assertTrue(result["audit"]["valid"])


if __name__ == "__main__":
    unittest.main()
