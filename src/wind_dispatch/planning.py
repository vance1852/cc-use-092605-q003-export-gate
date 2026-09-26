"""确定性的结算单价、能力与机组可用量计算。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Mapping, Sequence

from .clock import utc_text


ZERO = Decimal("0")
HUNDRED = Decimal("100")
BASIS_POINTS = Decimal("10000")


def quantize_volume(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def quantize_money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PricePoint:
    trade_date: str
    close: Decimal


@dataclass(frozen=True, slots=True)
class Streak:
    direction: str
    sessions: int
    start_date: str
    end_date: str
    start_close: Decimal
    end_close: Decimal
    percent_change: Decimal

    def as_dict(self) -> dict[str, object]:
        return {
            "direction": self.direction,
            "sessions": self.sessions,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "start_close": decimal_text(self.start_close),
            "end_close": decimal_text(self.end_close),
            "percent_change": decimal_text(self.percent_change),
        }


def latest_streak(points: Sequence[PricePoint]) -> Streak | None:
    ordered = sorted(points, key=lambda item: item.trade_date)
    if len(ordered) < 2:
        return None
    last = ordered[-1]
    previous = ordered[-2]
    if last.close == previous.close:
        return Streak("flat", 1, last.trade_date, last.trade_date, last.close, last.close, ZERO)
    direction = "down" if last.close < previous.close else "up"
    start_index = len(ordered) - 2
    while start_index > 0:
        left = ordered[start_index - 1]
        right = ordered[start_index]
        matches = right.close < left.close if direction == "down" else right.close > left.close
        if not matches:
            break
        start_index -= 1
    start = ordered[start_index]
    change = (last.close - start.close) / start.close * HUNDRED
    return Streak(
        direction=direction,
        sessions=len(ordered) - start_index,
        start_date=start.trade_date,
        end_date=last.trade_date,
        start_close=start.close,
        end_close=last.close,
        percent_change=change.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP),
    )


def moving_average(points: Sequence[PricePoint], sessions: int) -> Decimal | None:
    if sessions <= 0:
        raise ValueError("sessions 必须大于零")
    ordered = sorted(points, key=lambda item: item.trade_date)
    if len(ordered) < sessions:
        return None
    values = [item.close for item in ordered[-sessions:]]
    return quantize_money(sum(values, ZERO) / Decimal(len(values)))


def effective_capacity(
    nominal: Decimal,
    capacity_percentages: Iterable[Decimal],
) -> Decimal:
    result = nominal
    for percentage in capacity_percentages:
        bounded = max(ZERO, min(HUNDRED, percentage))
        result *= bounded / HUNDRED
    return quantize_volume(result)


@dataclass(frozen=True, slots=True)
class AllocationRequest:
    nomination_id: str
    requested: Decimal
    priority: int
    submitted_at: str


def allocate_capacity(
    available: Decimal,
    requests: Iterable[AllocationRequest],
) -> list[dict[str, str]]:
    if available < ZERO:
        raise ValueError("可用能力不能为负数")
    remaining = quantize_volume(available)
    result: list[dict[str, str]] = []
    ordered = sorted(requests, key=lambda item: (item.priority, item.submitted_at, item.nomination_id))
    for request in ordered:
        allocated = min(remaining, request.requested)
        allocated = quantize_volume(max(ZERO, allocated))
        remaining = quantize_volume(remaining - allocated)
        result.append({
            "nomination_id": request.nomination_id,
            "requested_mwh": decimal_text(request.requested),
            "allocated_mwh": decimal_text(allocated),
            "unfilled_mwh": decimal_text(quantize_volume(request.requested - allocated)),
        })
    return result


def delivered_after_loss(loaded: Decimal, loss_basis_points: int) -> Decimal:
    if not 0 <= loss_basis_points <= 1000:
        raise ValueError("损耗基点超出范围")
    retained = Decimal(1) - Decimal(loss_basis_points) / BASIS_POINTS
    return quantize_volume(loaded * retained)


def weighted_inventory_cost(lots: Iterable[Mapping[str, object]]) -> dict[str, str]:
    quantity = ZERO
    value = ZERO
    for lot in lots:
        available = Decimal(str(lot["available_mwh"]))
        unit_cost = Decimal(str(lot["unit_cost_cny"]))
        if available < ZERO or unit_cost < ZERO:
            raise ValueError("机组可用量数量和成本不能为负数")
        quantity += available
        value += available * unit_cost
    average = ZERO if quantity == ZERO else value / quantity
    return {
        "available_mwh": decimal_text(quantize_volume(quantity)),
        "inventory_value_cny": decimal_text(quantize_money(value)),
        "weighted_unit_cost_cny": decimal_text(quantize_money(average)),
    }


def reconcile_inventory(
    book_quantity: Decimal,
    measured_quantity: Decimal,
    tolerance_percent: Decimal,
) -> dict[str, object]:
    if book_quantity < ZERO or measured_quantity < ZERO:
        raise ValueError("机组可用量数量不能为负数")
    if tolerance_percent < ZERO:
        raise ValueError("容差不能为负数")
    delta = quantize_volume(measured_quantity - book_quantity)
    ratio = ZERO if book_quantity == ZERO else abs(delta) / book_quantity * HUNDRED
    return {
        "book_quantity": decimal_text(quantize_volume(book_quantity)),
        "measured_quantity": decimal_text(quantize_volume(measured_quantity)),
        "delta_mwh": decimal_text(delta),
        "variance_percent": decimal_text(ratio.quantize(Decimal("0.0001"))),
        "within_tolerance": ratio <= tolerance_percent,
    }


def scenario_projection(
    *,
    current_price: Decimal,
    market_index_drop_percent: Decimal,
    routes: Iterable[Mapping[str, object]],
    inventory: Iterable[Mapping[str, object]],
    route_capacity_changes: Mapping[str, Decimal],
    demand_changes: Mapping[str, Decimal],
) -> dict[str, object]:
    projected_price = current_price * (Decimal(1) - market_index_drop_percent / HUNDRED)
    route_rows: list[dict[str, str]] = []
    total_capacity = ZERO
    for route in sorted(routes, key=lambda item: str(item["route_id"])):
        route_id = str(route["route_id"])
        nominal = Decimal(str(route["daily_capacity"]))
        change = route_capacity_changes.get(route_id, ZERO)
        projected = max(ZERO, nominal * (Decimal(1) + change / HUNDRED))
        total_capacity += projected
        route_rows.append({
            "route_id": route_id,
            "base_capacity": decimal_text(quantize_volume(nominal)),
            "change_percent": decimal_text(change),
            "projected_capacity": decimal_text(quantize_volume(projected)),
        })
    inventory_rows: list[dict[str, str]] = []
    total_inventory = ZERO
    for row in sorted(inventory, key=lambda item: (str(item["facility_id"]), str(item["product"]))):
        key = f"{row['facility_id']}:{row['product']}"
        available = Decimal(str(row["available_mwh"]))
        demand_change = demand_changes.get(key, ZERO)
        days_factor = max(Decimal("0.01"), Decimal(1) + demand_change / HUNDRED)
        adjusted = available / days_factor
        total_inventory += adjusted
        inventory_rows.append({
            "inventory_key": key,
            "base_available": decimal_text(quantize_volume(available)),
            "demand_change_percent": decimal_text(demand_change),
            "demand_adjusted_inventory": decimal_text(quantize_volume(adjusted)),
        })
    return {
        "projected_market_index_cny": decimal_text(quantize_money(projected_price)),
        "total_projected_capacity": decimal_text(quantize_volume(total_capacity)),
        "demand_adjusted_inventory": decimal_text(quantize_volume(total_inventory)),
        "routes": route_rows,
        "inventory": inventory_rows,
    }


COMMITMENT_PHASES = ("preparation", "grid_connection", "ramping", "stable_operation")

BINDING_MESSAGES = {
    "CHANNEL_LIMIT": "受送出通道容量约束",
    "THERMAL_LIMIT": "受海缆热限额约束",
    "COMPENSATION_LIMIT": "受无功补偿余量约束",
}


@dataclass(frozen=True, slots=True)
class RampPoint:
    offset_minutes: int
    mw: Decimal


def evaluate_commitment(
    *,
    declared_target_mw: Decimal,
    installed_mw: Decimal,
    availability_percent: Decimal,
    reserve_mw: Decimal,
    channel_capacity_mw: Decimal,
    cable_thermal_limit_mw: Decimal,
    outage_percents: Iterable[Decimal],
    compensation_mvar: Decimal,
    compensation_ratio: Decimal,
    held_channel_mw: Decimal = ZERO,
    held_compensation_mvar: Decimal = ZERO,
) -> dict[str, object]:
    """合并日前申报、无功补偿、海缆热限额和检修计划，给出门禁结论。"""
    reasons: list[dict[str, str]] = []
    capability = quantize_volume(installed_mw * availability_percent / HUNDRED)
    requested = quantize_volume(declared_target_mw)
    if capability < requested:
        requested = capability
        reasons.append({
            "code": "CAPABILITY_CAPPED",
            "message": f"申报目标 {decimal_text(declared_target_mw)} MW 超出机组可用能力 "
            f"{decimal_text(capability)} MW，按可用能力参与门禁",
        })
    if channel_capacity_mw <= cable_thermal_limit_mw:
        base_ceiling = channel_capacity_mw
        base_binding = "CHANNEL_LIMIT"
    else:
        base_ceiling = cable_thermal_limit_mw
        base_binding = "THERMAL_LIMIT"
    percents = list(outage_percents)
    ceiling = effective_capacity(base_ceiling, percents)
    if percents and ceiling < base_ceiling:
        reasons.append({
            "code": "OUTAGE_DERATE",
            "message": f"检修计划与有效期重叠，有效上限由 {decimal_text(base_ceiling)} MW "
            f"降至 {decimal_text(ceiling)} MW",
        })
    remaining_channel = max(ZERO, ceiling - held_channel_mw)
    remaining_compensation = max(ZERO, compensation_mvar - held_compensation_mvar)
    if held_channel_mw > ZERO or held_compensation_mvar > ZERO:
        reasons.append({
            "code": "LOCK_CONTENTION",
            "message": f"已确认计划锁定通道 {decimal_text(held_channel_mw)} MW、"
            f"补偿 {decimal_text(held_compensation_mvar)} Mvar，剩余余量参与本次判断",
        })
    compensation_ceiling = None
    if compensation_ratio > ZERO:
        compensation_ceiling = quantize_volume(remaining_compensation / compensation_ratio)
    headroom = remaining_channel if compensation_ceiling is None else min(remaining_channel, compensation_ceiling)
    binding = base_binding
    if compensation_ceiling is not None and compensation_ceiling < remaining_channel:
        binding = "COMPENSATION_LIMIT"
    if reserve_mw > headroom:
        outcome = "exemption_required"
        committed = requested
        reasons.append({"code": binding, "message": BINDING_MESSAGES[binding]})
        reasons.append({
            "code": "RESERVE_EXCEEDS_ENVELOPE",
            "message": f"备用要求 {decimal_text(reserve_mw)} MW 超出送出边界余量 "
            f"{decimal_text(headroom)} MW，无法通过降额满足，需紧急保供豁免",
        })
    elif requested + reserve_mw > headroom:
        outcome = "derated"
        committed = quantize_volume(headroom - reserve_mw)
        reasons.append({"code": binding, "message": BINDING_MESSAGES[binding]})
        reasons.append({
            "code": "RESERVE_HEADROOM",
            "message": f"申报功率与备用合计超出边界余量，预留备用 {decimal_text(reserve_mw)} MW 后 "
            f"承诺功率降额至 {decimal_text(committed)} MW",
        })
    else:
        outcome = "approved"
        committed = requested
        reasons.append({
            "code": "WITHIN_ENVELOPE",
            "message": "申报功率与备用要求均在送出边界、热限额和补偿余量之内",
        })
    return {
        "outcome": outcome,
        "declared_target_mw": decimal_text(declared_target_mw),
        "capability_mw": decimal_text(capability),
        "requested_mw": decimal_text(requested),
        "committed_mw": decimal_text(committed),
        "reserve_mw": decimal_text(reserve_mw),
        "channel_ceiling_mw": decimal_text(ceiling),
        "compensation_ceiling_mw": None if compensation_ceiling is None else decimal_text(compensation_ceiling),
        "headroom_mw": decimal_text(headroom),
        "held_channel_mw": decimal_text(held_channel_mw),
        "held_compensation_mvar": decimal_text(held_compensation_mvar),
        "remaining_compensation_mvar": decimal_text(remaining_compensation),
        "compensation_need_mvar": decimal_text(quantize_volume(committed * compensation_ratio)),
        "binding_constraint": binding,
        "reasons": reasons,
    }


def form_commitment_phases(
    *,
    window_start: datetime,
    window_minutes: int,
    ramp_points: Sequence[RampPoint],
    committed_mw: Decimal,
) -> list[dict[str, object]]:
    """按申报爬坡曲线和承诺功率形成准备、并网、爬坡、稳定运行四个阶段。"""
    if window_minutes <= 0:
        raise ValueError("边界有效期窗口必须大于零")
    if len(ramp_points) < 2:
        raise ValueError("爬坡曲线至少需要两个点")
    offsets = [point.offset_minutes for point in ramp_points]
    if offsets[-1] >= window_minutes:
        raise ValueError("爬坡曲线超出边界有效期窗口")
    requested = ramp_points[-1].mw
    scale = ZERO if requested == ZERO else committed_mw / requested
    scaled = [quantize_volume(point.mw * scale) for point in ramp_points]
    scaled[-1] = quantize_volume(committed_mw)

    def stamp(offset: int) -> str:
        return utc_text(window_start + timedelta(minutes=offset))

    def energy(mw: Decimal, minutes: int) -> Decimal:
        return quantize_volume(mw * Decimal(minutes) / Decimal(60))

    ramp_energy = ZERO
    for index in range(1, len(offsets) - 1):
        ramp_energy += energy(scaled[index], offsets[index + 1] - offsets[index])
    zero_text = decimal_text(quantize_volume(ZERO))
    return [
        {
            "phase": "preparation",
            "starts_at": stamp(0),
            "ends_at": stamp(offsets[0]),
            "duration_minutes": offsets[0],
            "committed_mw": zero_text,
            "committed_mwh": zero_text,
            "profile": [],
        },
        {
            "phase": "grid_connection",
            "starts_at": stamp(offsets[0]),
            "ends_at": stamp(offsets[1]),
            "duration_minutes": offsets[1] - offsets[0],
            "committed_mw": decimal_text(scaled[0]),
            "committed_mwh": decimal_text(energy(scaled[0], offsets[1] - offsets[0])),
            "profile": [],
        },
        {
            "phase": "ramping",
            "starts_at": stamp(offsets[1]),
            "ends_at": stamp(offsets[-1]),
            "duration_minutes": offsets[-1] - offsets[1],
            "committed_mw": None,
            "committed_mwh": decimal_text(quantize_volume(ramp_energy)),
            "profile": [
                {"offset_minutes": offsets[index], "mw": decimal_text(scaled[index])}
                for index in range(1, len(offsets))
            ],
        },
        {
            "phase": "stable_operation",
            "starts_at": stamp(offsets[-1]),
            "ends_at": stamp(window_minutes),
            "duration_minutes": window_minutes - offsets[-1],
            "committed_mw": decimal_text(scaled[-1]),
            "committed_mwh": decimal_text(energy(scaled[-1], window_minutes - offsets[-1])),
            "profile": [],
        },
    ]


def settle_receipt(
    phases: Sequence[Mapping[str, object]],
    actuals: Mapping[str, Decimal],
) -> dict[str, object]:
    """按各阶段实际完成量结算，超出承诺部分只记录不结算。"""
    rows: list[dict[str, object]] = []
    total_committed = ZERO
    total_settled = ZERO
    total_shortfall = ZERO
    total_over = ZERO
    for phase in phases:
        name = str(phase["phase"])
        if name not in actuals:
            raise ValueError(f"执行回执缺少阶段 {name} 的实际完成量")
        actual_mw = actuals[name]
        if actual_mw < ZERO:
            raise ValueError("实际完成量不能为负数")
        hours = Decimal(int(phase["duration_minutes"])) / Decimal(60)
        committed_mwh = Decimal(str(phase["committed_mwh"]))
        actual_mwh = quantize_volume(actual_mw * hours)
        settled = min(actual_mwh, committed_mwh)
        shortfall = quantize_volume(max(ZERO, committed_mwh - actual_mwh))
        over = quantize_volume(max(ZERO, actual_mwh - committed_mwh))
        total_committed += committed_mwh
        total_settled += settled
        total_shortfall += shortfall
        total_over += over
        rows.append({
            "phase": name,
            "committed_mwh": decimal_text(committed_mwh),
            "actual_mwh": decimal_text(actual_mwh),
            "settled_mwh": decimal_text(settled),
            "shortfall_mwh": decimal_text(shortfall),
            "over_delivery_mwh": decimal_text(over),
        })
    return {
        "phases": rows,
        "committed_mwh": decimal_text(quantize_volume(total_committed)),
        "settled_mwh": decimal_text(quantize_volume(total_settled)),
        "shortfall_mwh": decimal_text(quantize_volume(total_shortfall)),
        "over_delivery_mwh": decimal_text(quantize_volume(total_over)),
    }
