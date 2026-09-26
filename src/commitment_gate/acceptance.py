"""贯通送出边界、场站申报、分阶段承诺、豁免与执行结算的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import CommitmentGateService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    clock = FrozenClock(datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc))
    service = CommitmentGateService(connection, clock)
    for user_id, role in (
        ("dispatch", "dispatcher"),
        ("station", "station"),
        ("coord", "coordinator"),
        ("audit", "auditor"),
    ):
        service.create_user(user_id, user_id, role)

    service.publish_boundary("dispatch", {
        "boundary_id": "bnd-north-1",
        "corridor_id": "corridor-north",
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
    })
    service.submit_declaration("station", {
        "declaration_id": "decl-alpha-1",
        "corridor_id": "corridor-north",
        "station_id": "station-alpha",
        "installed_capacity_mw": "600",
        "availability_percent": "90",
        "ramp_curve": [
            {"offset_minutes": 0, "output_percent": "0"},
            {"offset_minutes": 30, "output_percent": "50"},
            {"offset_minutes": 60, "output_percent": "100"},
        ],
        "reserve_mw": "50",
        "latest_response_at": "2026-09-27T18:00:00Z",
    })

    approved = service.create_plan("coord", {
        "plan_id": "plan-alpha-1", "corridor_id": "corridor-north", "station_id": "station-alpha",
        "window_start": "2026-09-27T02:00:00Z", "window_end": "2026-09-27T08:00:00Z",
        "requested_mw": "500",
    })
    derated = service.create_plan("coord", {
        "plan_id": "plan-alpha-2", "corridor_id": "corridor-north", "station_id": "station-alpha",
        "window_start": "2026-09-27T09:00:00Z", "window_end": "2026-09-27T16:00:00Z",
        "requested_mw": "500",
    })
    blocked = service.create_plan("coord", {
        "plan_id": "plan-alpha-3", "corridor_id": "corridor-north", "station_id": "station-alpha",
        "window_start": "2026-09-27T19:00:00Z", "window_end": "2026-09-27T23:30:00Z",
        "requested_mw": "200",
    })

    service.confirm_plan("coord", "plan-alpha-1", 1)
    service.grant_exemption("coord", "plan-alpha-3", {
        "exemption_id": "exm-alpha-3",
        "reason": "晚高峰紧急保供",
        "expires_at": "2026-09-27T23:00:00Z",
    })
    exempted_confirm = service.confirm_plan("coord", "plan-alpha-3", 2)

    clock.advance(hours=19)  # 2026-09-27T03:00:00Z
    service.begin_execution("dispatch", "plan-alpha-1", 2)
    settlement = service.submit_receipt("dispatch", "plan-alpha-1", {
        "receipt_id": "rcp-alpha-1", "actual_mwh": "2300",
    })

    revised = service.publish_boundary("dispatch", {
        "boundary_id": "bnd-north-2",
        "corridor_id": "corridor-north",
        "version": 2,
        "effective_from": "2026-09-27T00:00:00Z",
        "effective_to": "2026-09-28T00:00:00Z",
        "channel_capacity_mw": "800",
        "reactive_compensation_mvar": "300",
        "cable_thermal_limit_mw": "750",
        "maintenance_windows": [],
    })
    reconfirmed = service.confirm_plan("coord", "plan-alpha-2", 2)

    result = {
        "status": "ok",
        "decisions": {
            "approved": approved["decision"],
            "derated_before_revision": derated["decision"],
            "exemption_required": blocked["decision"],
        },
        "exempted_confirm": exempted_confirm,
        "settlement": settlement,
        "revision_reevaluated": revised["reevaluated_plan_ids"],
        "reconfirmed": reconfirmed,
        "pending_after_close": service.pending_plans("coord")["pending"],
        "explanation_reasons": service.plan_explanation("coord", "plan-alpha-2")["reasons"],
        "audit": service.audit_chain("audit"),
        "workspace": workspace.name,
    }
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行承诺门禁服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
