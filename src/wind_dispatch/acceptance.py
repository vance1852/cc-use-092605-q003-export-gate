"""贯通结算单价、送出通道、机组可用量、提名和情景分析的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import SupplyService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = SupplyService(connection, FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)))
    for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("station", "station"), ("risk", "risk"), ("audit", "auditor")):
        service.create_user(user_id, user_id, role)
    for index, close in enumerate(("108", "105", "102", "100", "98", "96"), start=18):
        service.record_quote("plan", {"market_index": "PEAK_VALLEY", "trade_date": f"2026-09-{index}", "close_cny": close, "source_revision": f"rev-{index}", "observed_at": f"2026-09-{index}T21:00:00Z"})
    service.create_facility("plan", {"facility_id": "fanshi-one", "name": "北部海上风电场", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_mwh": "500000"})
    service.create_facility("plan", {"facility_id": "fanshi-two", "name": "帆石二场", "kind": "offshore-station", "timezone": "Asia/Shanghai", "capacity_mwh": "800000"})
    service.create_route("plan", {"route_id": "fanshi-export", "origin_id": "fanshi-one", "destination_id": "fanshi-two", "product": "turbine-18mw", "daily_capacity": "100000", "loss_basis_points": 25, "transit_hours": 36})
    service.add_inventory_lot("dispatch", {"lot_id": "lot-001", "facility_id": "fanshi-one", "product": "turbine-18mw", "grade": "PEAK_VALLEY", "quantity_mwh": "150000", "unit_cost_cny": "91.25", "received_at": "2026-09-24T06:00:00Z"})
    service.submit_nomination("dispatch", {"nomination_id": "nom-001", "route_id": "fanshi-export", "shipper_id": "station-east", "service_date": "2026-09-25", "requested_mwh": "80000", "priority": 10, "idempotency_key": "nom-key-001"})
    allocation = service.allocate("dispatch", "fanshi-export", "2026-09-25")
    transfer = service.dispatch_transfer("dispatch", "transfer-001", "nom-001", "lot-001", 2)
    service.create_scenario("plan", {"scenario_id": "grid-recovery", "name": "关键机组检修恢复与需求回落", "market_index_drop_percent": "9", "route_capacity_changes": {"fanshi-export": "20"}, "demand_changes": {"fanshi-one:turbine-18mw": "-5"}})
    service.approve_scenario("risk", "grid-recovery", 1)
    scenario = service.run_scenario("plan", "grid-recovery", "2026-09-23")
    gate = _run_commitment_gate(service)
    result = {"status": "ok", "price": service.price_summary("PEAK_VALLEY"), "allocation_id": allocation["allocation_id"], "transfer": transfer, "scenario_run_id": scenario["run_id"], "commitment_gate": gate, "audit": service.audit_chain("audit"), "workspace": workspace.name}
    connection.close()
    return result


def _run_commitment_gate(service) -> dict[str, object]:
    """演练边界发布、场站申报、分阶段门禁、豁免、确认锁定和按实际量结算。"""
    boundary_v1 = service.publish_boundary("dispatch", {"route_id": "fanshi-export", "service_date": "2026-09-26", "version": 1, "channel_capacity_mw": "500", "cable_thermal_limit_mw": "480", "compensation_mvar": "200", "compensation_ratio": "0.3", "effective_from": "2026-09-26T00:00:00Z", "effective_until": "2026-09-27T00:00:00Z"})
    declaration_a = service.submit_declaration("station", {"declaration_id": "decl-a", "route_id": "fanshi-export", "service_date": "2026-09-26", "installed_mw": "400", "availability_percent": "90", "ramp_points": [{"offset_minutes": 60, "mw": "100"}, {"offset_minutes": 240, "mw": "200"}, {"offset_minutes": 300, "mw": "320"}], "reserve_mw": "40", "respond_by": "2026-09-25T12:00:00Z", "idempotency_key": "decl-key-a"})
    plan_a = declaration_a["plan"]["plan_id"]
    service.confirm_plan("dispatch", plan_a)
    receipt_a = service.submit_receipt("station", plan_a, {"receipt_id": "receipt-a", "idempotency_key": "receipt-key-a", "phase_actuals": {"preparation": "0", "grid_connection": "100", "ramping": "190", "stable_operation": "310"}})
    declaration_b = service.submit_declaration("station", {"declaration_id": "decl-b", "route_id": "fanshi-export", "service_date": "2026-09-26", "installed_mw": "300", "availability_percent": "100", "ramp_points": [{"offset_minutes": 60, "mw": "150"}, {"offset_minutes": 240, "mw": "260"}, {"offset_minutes": 300, "mw": "260"}], "reserve_mw": "490", "respond_by": "2026-09-25T12:00:00Z", "idempotency_key": "decl-key-b"})
    plan_b = declaration_b["plan"]["plan_id"]
    exemption = service.grant_exemption("risk", {"plan_id": plan_b, "reason": "台风过境后负荷缺口，启动紧急保供", "expires_at": "2026-09-27T00:00:00Z"})
    service.confirm_plan("dispatch", plan_b)
    service.submit_receipt("station", plan_b, {"receipt_id": "receipt-b", "idempotency_key": "receipt-key-b", "phase_actuals": {"preparation": "0", "grid_connection": "150", "ramping": "260", "stable_operation": "260"}})
    declaration_c = service.submit_declaration("station", {"declaration_id": "decl-c", "route_id": "fanshi-export", "service_date": "2026-09-26", "installed_mw": "700", "availability_percent": "100", "ramp_points": [{"offset_minutes": 30, "mw": "200"}, {"offset_minutes": 120, "mw": "400"}, {"offset_minutes": 180, "mw": "620"}], "reserve_mw": "30", "respond_by": "2026-09-25T12:00:00Z", "idempotency_key": "decl-key-c"})
    plan_c = declaration_c["plan"]["plan_id"]
    boundary_v2 = service.publish_boundary("dispatch", {"route_id": "fanshi-export", "service_date": "2026-09-26", "version": 2, "channel_capacity_mw": "650", "cable_thermal_limit_mw": "640", "compensation_mvar": "260", "compensation_ratio": "0.3", "effective_from": "2026-09-26T00:00:00Z", "effective_until": "2026-09-27T00:00:00Z"})
    explanation = service.commitment_plan("audit", plan_c)
    pending = service.list_plans("audit", state="pending", route_id="fanshi-export", service_date="2026-09-26")
    return {
        "boundary_versions": [boundary_v1["version"], boundary_v2["version"]],
        "approved_plan": {"plan_id": plan_a, "outcome": "approved", "settled_mwh": receipt_a["settled_mwh"], "shortfall_mwh": receipt_a["shortfall_mwh"]},
        "exemption_plan": {"plan_id": plan_b, "outcome": "exemption_required", "exemption_id": exemption["exemption_id"], "authorized_by": exemption["authorized_by"]},
        "derated_plan": {"plan_id": plan_c, "outcome": explanation["outcome"], "committed_mw": explanation["committed_mw"], "boundary_version": explanation["boundary"]["version"], "reasons": [reason["code"] for reason in explanation["evaluation"]["reasons"]]},
        "pending_after_restart_view": pending["plans"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行海上风电场调度服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
