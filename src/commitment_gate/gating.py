"""送出边界与场站申报合并判断的确定性门禁计算。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Sequence

from .clock import parse_utc, utc_text
from .models import MaintenanceWindow, RampPoint


ZERO = Decimal("0")
HUNDRED = Decimal("100")

# 每 MW 有功送出所需的无功补偿（Mvar）。
COMPENSATION_RATIO = Decimal("0.5")
# 准备与并网阶段的固定时长（分钟）。
PREPARATION_MINUTES = 30
GRID_CONNECTION_MINUTES = 15

PHASE_LABELS = {
    "preparation": "准备",
    "grid_connection": "并网",
    "ramping": "爬坡",
    "steady_operation": "稳定运行",
}


def quantize_mw(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def overlapping_maintenance(
    windows: Sequence[MaintenanceWindow],
    window_start: datetime,
    window_end: datetime,
) -> list[MaintenanceWindow]:
    return [
        item
        for item in windows
        if parse_utc(item.starts_at) < window_end and parse_utc(item.ends_at) > window_start
    ]


def maintenance_capacity_percent(overlapping: Sequence[MaintenanceWindow]) -> Decimal:
    """检修叠加时按最严格的可用容量百分比降额。"""
    if not overlapping:
        return HUNDRED
    return min(item.capacity_percent for item in overlapping)


@dataclass(frozen=True, slots=True)
class GateEvaluation:
    decision: str  # approved | derated | exemption_required
    committed_mw: Decimal
    effective_capacity_mw: Decimal
    constraints: tuple[dict[str, object], ...]
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "decision": self.decision,
            "committed_mw": decimal_text(self.committed_mw),
            "effective_capacity_mw": decimal_text(self.effective_capacity_mw),
            "constraints": [dict(item) for item in self.constraints],
            "reasons": list(self.reasons),
        }


def evaluate_commitment(
    *,
    requested_mw: Decimal,
    installed_capacity_mw: Decimal,
    availability_percent: Decimal,
    reserve_mw: Decimal,
    channel_capacity_mw: Decimal,
    cable_thermal_limit_mw: Decimal,
    reactive_compensation_mvar: Decimal,
    maintenance_percent: Decimal,
    overlapping_windows: Sequence[MaintenanceWindow],
) -> GateEvaluation:
    """把日前请求、无功补偿、海缆热限额和检修计划合并成单一门禁结论。"""
    thermal_note = cable_thermal_limit_mw < channel_capacity_mw
    base_capacity = min(channel_capacity_mw, cable_thermal_limit_mw)
    effective = quantize_mw(base_capacity * maintenance_percent / HUNDRED)
    headroom = quantize_mw(max(ZERO, effective - reserve_mw))
    compensation_cap = quantize_mw(reactive_compensation_mvar / COMPENSATION_RATIO)
    availability_cap = quantize_mw(installed_capacity_mw * availability_percent / HUNDRED)
    committed = quantize_mw(max(ZERO, min(requested_mw, availability_cap, headroom, compensation_cap)))

    constraints: list[dict[str, object]] = [
        {
            "name": "channel_capacity",
            "label": "通道容量",
            "limit_mw": decimal_text(quantize_mw(channel_capacity_mw)),
            "required_mw": decimal_text(quantize_mw(requested_mw)),
            "binding": requested_mw > channel_capacity_mw,
        },
        {
            "name": "cable_thermal_limit",
            "label": "海缆热限额",
            "limit_mw": decimal_text(quantize_mw(cable_thermal_limit_mw)),
            "required_mw": decimal_text(quantize_mw(requested_mw)),
            "binding": thermal_note,
        },
        {
            "name": "maintenance_derate",
            "label": "检修降额",
            "capacity_percent": decimal_text(maintenance_percent),
            "effective_capacity_mw": decimal_text(effective),
            "binding": maintenance_percent < HUNDRED,
            "windows": [item.as_dict() for item in overlapping_windows],
        },
        {
            "name": "reserve_requirement",
            "label": "备用要求",
            "reserve_mw": decimal_text(quantize_mw(reserve_mw)),
            "headroom_mw": decimal_text(headroom),
            "binding": headroom < requested_mw,
        },
        {
            "name": "reactive_compensation",
            "label": "无功补偿",
            "limit_mvar": decimal_text(quantize_mw(reactive_compensation_mvar)),
            "supports_mw": decimal_text(compensation_cap),
            "required_mvar": decimal_text(quantize_mw(requested_mw * COMPENSATION_RATIO)),
            "binding": compensation_cap < requested_mw,
        },
        {
            "name": "unit_availability",
            "label": "机组可用率",
            "installed_capacity_mw": decimal_text(quantize_mw(installed_capacity_mw)),
            "availability_percent": decimal_text(availability_percent),
            "deliverable_mw": decimal_text(availability_cap),
            "binding": availability_cap < requested_mw,
        },
    ]

    reasons: list[str] = []
    if thermal_note:
        reasons.append(
            f"海缆热限额 {decimal_text(quantize_mw(cable_thermal_limit_mw))} MW 低于通道容量，"
            f"送出上限按热限额执行"
        )
    for item in overlapping_windows:
        reasons.append(
            f"检修窗口 {item.starts_at}~{item.ends_at}（{item.reason}）"
            f"将容量降至 {decimal_text(item.capacity_percent)}%"
        )
    if headroom < requested_mw:
        reasons.append(
            f"扣除备用 {decimal_text(quantize_mw(reserve_mw))} MW 后通道余量 "
            f"{decimal_text(headroom)} MW"
        )
    if compensation_cap < requested_mw:
        reasons.append(
            f"无功补偿 {decimal_text(quantize_mw(reactive_compensation_mvar))} Mvar "
            f"仅可支撑 {decimal_text(compensation_cap)} MW 有功"
        )
    if availability_cap < requested_mw:
        reasons.append(
            f"装机 {decimal_text(quantize_mw(installed_capacity_mw))} MW、"
            f"机组可用率 {decimal_text(availability_percent)}% 仅可兑现 "
            f"{decimal_text(availability_cap)} MW"
        )

    if committed >= requested_mw:
        decision = "approved"
        reasons.append("全部约束满足，按申报值获批")
    elif committed > ZERO:
        decision = "derated"
        reasons.append(f"按最严格约束降额至 {decimal_text(committed)} MW")
    else:
        decision = "exemption_required"
        reasons.append("有效送出能力为零，必须取得紧急保供豁免才能确认")
    return GateEvaluation(
        decision=decision,
        committed_mw=committed,
        effective_capacity_mw=effective,
        constraints=tuple(constraints),
        reasons=tuple(reasons),
    )


def build_phase_schedule(
    *,
    window_start: datetime,
    window_end: datetime,
    committed_mw: Decimal,
    ramp_curve: Sequence[RampPoint],
) -> tuple[list[dict[str, object]], Decimal]:
    """按申报爬坡曲线生成准备、并网、爬坡、稳定运行四阶段，并积分出计划电量。"""
    ramp_minutes = ramp_curve[-1].offset_minutes
    preparation_end = window_start + timedelta(minutes=PREPARATION_MINUTES)
    connection_end = preparation_end + timedelta(minutes=GRID_CONNECTION_MINUTES)
    ramp_end = connection_end + timedelta(minutes=ramp_minutes)
    if window_end <= ramp_end:
        raise ValueError("计划窗口不足以完成准备、并网与爬坡阶段")

    ramp_energy = ZERO
    checkpoints: list[dict[str, str]] = []
    for left, right in zip(ramp_curve, ramp_curve[1:]):
        hours = Decimal(right.offset_minutes - left.offset_minutes) / Decimal(60)
        average_percent = (left.output_percent + right.output_percent) / 2
        ramp_energy += committed_mw * average_percent / HUNDRED * hours
    for point in ramp_curve:
        at = connection_end + timedelta(minutes=point.offset_minutes)
        checkpoints.append(
            {
                "at": utc_text(at),
                "output_percent": decimal_text(point.output_percent),
                "target_mw": decimal_text(quantize_mw(committed_mw * point.output_percent / HUNDRED)),
            }
        )
    steady_hours = Decimal((window_end - ramp_end).total_seconds()) / Decimal(3600)
    steady_energy = committed_mw * steady_hours
    planned_mwh = quantize_mw(ramp_energy + steady_energy)

    phases = [
        {
            "phase": "preparation",
            "label": PHASE_LABELS["preparation"],
            "starts_at": utc_text(window_start),
            "ends_at": utc_text(preparation_end),
            "target_mw": "0",
        },
        {
            "phase": "grid_connection",
            "label": PHASE_LABELS["grid_connection"],
            "starts_at": utc_text(preparation_end),
            "ends_at": utc_text(connection_end),
            "target_mw": "0",
        },
        {
            "phase": "ramping",
            "label": PHASE_LABELS["ramping"],
            "starts_at": utc_text(connection_end),
            "ends_at": utc_text(ramp_end),
            "target_mw": decimal_text(quantize_mw(committed_mw)),
            "checkpoints": checkpoints,
        },
        {
            "phase": "steady_operation",
            "label": PHASE_LABELS["steady_operation"],
            "starts_at": utc_text(ramp_end),
            "ends_at": utc_text(window_end),
            "target_mw": decimal_text(quantize_mw(committed_mw)),
        },
    ]
    return phases, planned_mwh
