"""海上风电场调度领域输入契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .clock import parse_utc
from .errors import ValidationFailed
from .planning import COMMITMENT_PHASES, RampPoint


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
POWER_PRICE_INDEXES = {"PEAK_VALLEY", "MARKET_SETTLED", "GRID_COMMITTED", "DAY_AHEAD", "REGULATED", "CUSTOM"}
PRODUCTS = {"turbine-18mw", "turbine-16mw", "turbine-14mw", "reactive-compensator", "subsea-cable", "maintenance-vessel"}
ROUTE_KINDS = {"export-corridor", "offshore-station", "station", "storage", "compensation-station"}


def required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field} 格式不正确")
    return result


def decimal_value(
    value: object,
    field: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool):
        raise ValidationFailed(f"{field} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationFailed(f"{field} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationFailed(f"{field} 必须是有限数值")
    if minimum is not None and result < minimum:
        raise ValidationFailed(f"{field} 不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ValidationFailed(f"{field} 不能大于 {maximum}")
    return result


def positive_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationFailed(f"{field} 必须是正整数")
    return value


def date_text(value: object, field: str) -> str:
    result = required_text(value, field, 10)
    try:
        return date.fromisoformat(result).isoformat()
    except ValueError as exc:
        raise ValidationFailed(f"{field} 必须是 YYYY-MM-DD 日期") from exc


@dataclass(frozen=True, slots=True)
class IndexQuote:
    market_index: str
    trade_date: str
    close_cny: Decimal
    source_revision: str
    observed_at: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "IndexQuote":
        market_index = required_text(raw.get("market_index"), "market_index", 16).upper()
        if market_index not in POWER_PRICE_INDEXES - {"CUSTOM"}:
            raise ValidationFailed("market_index 必须是 PEAK_VALLEY、MARKET_SETTLED、GRID_COMMITTED、DAY_AHEAD 或 REGULATED")
        observed_at = required_text(raw.get("observed_at"), "observed_at", 40)
        try:
            parse_utc(observed_at, "observed_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        return cls(
            market_index=market_index,
            trade_date=date_text(raw.get("trade_date"), "trade_date"),
            close_cny=decimal_value(raw.get("close_cny"), "close_cny", minimum=Decimal("0.01")),
            source_revision=identifier(raw.get("source_revision"), "source_revision"),
            observed_at=observed_at,
        )


@dataclass(frozen=True, slots=True)
class Facility:
    facility_id: str
    name: str
    kind: str
    timezone: str
    capacity_mwh: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Facility":
        kind = required_text(raw.get("kind"), "kind", 24)
        if kind not in ROUTE_KINDS:
            raise ValidationFailed("kind 不是受支持的设施类型")
        timezone = required_text(raw.get("timezone"), "timezone", 64)
        if "/" not in timezone and timezone != "UTC":
            raise ValidationFailed("timezone 必须是 IANA 时区或 UTC")
        return cls(
            facility_id=identifier(raw.get("facility_id"), "facility_id"),
            name=required_text(raw.get("name"), "name"),
            kind=kind,
            timezone=timezone,
            capacity_mwh=decimal_value(
                raw.get("capacity_mwh"), "capacity_mwh", minimum=Decimal("0")
            ),
        )


@dataclass(frozen=True, slots=True)
class Route:
    route_id: str
    origin_id: str
    destination_id: str
    product: str
    daily_capacity: Decimal
    loss_basis_points: int
    transit_hours: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Route":
        product = required_text(raw.get("product"), "product", 32)
        if product not in PRODUCTS:
            raise ValidationFailed("product 不是受支持的机组类型")
        loss = raw.get("loss_basis_points", 0)
        if isinstance(loss, bool) or not isinstance(loss, int) or not 0 <= loss <= 1000:
            raise ValidationFailed("loss_basis_points 必须是 0 到 1000 的整数")
        origin = identifier(raw.get("origin_id"), "origin_id")
        destination = identifier(raw.get("destination_id"), "destination_id")
        if origin == destination:
            raise ValidationFailed("送出通道起点和终点不能相同")
        return cls(
            route_id=identifier(raw.get("route_id"), "route_id"),
            origin_id=origin,
            destination_id=destination,
            product=product,
            daily_capacity=decimal_value(
                raw.get("daily_capacity"), "daily_capacity", minimum=Decimal("0.001")
            ),
            loss_basis_points=loss,
            transit_hours=positive_integer(raw.get("transit_hours"), "transit_hours"),
        )


@dataclass(frozen=True, slots=True)
class InventoryLot:
    lot_id: str
    facility_id: str
    product: str
    grade: str
    quantity_mwh: Decimal
    unit_cost_cny: Decimal
    received_at: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "InventoryLot":
        product = required_text(raw.get("product"), "product", 32)
        if product not in PRODUCTS:
            raise ValidationFailed("product 不是受支持的机组类型")
        received_at = required_text(raw.get("received_at"), "received_at", 40)
        try:
            parse_utc(received_at, "received_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        return cls(
            lot_id=identifier(raw.get("lot_id"), "lot_id"),
            facility_id=identifier(raw.get("facility_id"), "facility_id"),
            product=product,
            grade=required_text(raw.get("grade"), "grade", 32).upper(),
            quantity_mwh=decimal_value(
                raw.get("quantity_mwh"), "quantity_mwh", minimum=Decimal("0.001")
            ),
            unit_cost_cny=decimal_value(
                raw.get("unit_cost_cny"), "unit_cost_cny", minimum=Decimal("0")
            ),
            received_at=received_at,
        )


@dataclass(frozen=True, slots=True)
class NominationRequest:
    nomination_id: str
    route_id: str
    shipper_id: str
    service_date: str
    requested_mwh: Decimal
    priority: int
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "NominationRequest":
        priority = raw.get("priority", 100)
        if isinstance(priority, bool) or not isinstance(priority, int) or not 1 <= priority <= 999:
            raise ValidationFailed("priority 必须是 1 到 999 的整数")
        return cls(
            nomination_id=identifier(raw.get("nomination_id"), "nomination_id"),
            route_id=identifier(raw.get("route_id"), "route_id"),
            shipper_id=identifier(raw.get("shipper_id"), "shipper_id"),
            service_date=date_text(raw.get("service_date"), "service_date"),
            requested_mwh=decimal_value(
                raw.get("requested_mwh"), "requested_mwh", minimum=Decimal("0.001")
            ),
            priority=priority,
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )


@dataclass(frozen=True, slots=True)
class SupplyScenario:
    scenario_id: str
    name: str
    market_index_drop_percent: Decimal
    route_capacity_changes: Mapping[str, Decimal]
    demand_changes: Mapping[str, Decimal]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SupplyScenario":
        route_changes = raw.get("route_capacity_changes", {})
        demand_changes = raw.get("demand_changes", {})
        if not isinstance(route_changes, Mapping) or not isinstance(demand_changes, Mapping):
            raise ValidationFailed("情景变化必须是对象")
        parsed_routes = {
            identifier(key, "route_capacity_changes 键"): decimal_value(
                value, f"route_capacity_changes.{key}", minimum=Decimal("-100"), maximum=Decimal("500")
            )
            for key, value in route_changes.items()
        }
        parsed_demand = {
            identifier(key, "demand_changes 键"): decimal_value(
                value, f"demand_changes.{key}", minimum=Decimal("-100"), maximum=Decimal("500")
            )
            for key, value in demand_changes.items()
        }
        return cls(
            scenario_id=identifier(raw.get("scenario_id"), "scenario_id"),
            name=required_text(raw.get("name"), "name"),
            market_index_drop_percent=decimal_value(
                raw.get("market_index_drop_percent", 0),
                "market_index_drop_percent",
                minimum=Decimal("-500"),
                maximum=Decimal("100"),
            ),
            route_capacity_changes=parsed_routes,
            demand_changes=parsed_demand,
        )


@dataclass(frozen=True, slots=True)
class TransmissionBoundary:
    route_id: str
    service_date: str
    version: int
    channel_capacity_mw: Decimal
    cable_thermal_limit_mw: Decimal
    compensation_mvar: Decimal
    compensation_ratio: Decimal
    effective_from: str
    effective_until: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "TransmissionBoundary":
        version = raw.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ValidationFailed("version 必须是不小于 1 的整数")
        effective_from = required_text(raw.get("effective_from"), "effective_from", 40)
        effective_until = required_text(raw.get("effective_until"), "effective_until", 40)
        try:
            start = parse_utc(effective_from, "effective_from")
            end = parse_utc(effective_until, "effective_until")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        if end <= start:
            raise ValidationFailed("effective_until 必须晚于 effective_from")
        return cls(
            route_id=identifier(raw.get("route_id"), "route_id"),
            service_date=date_text(raw.get("service_date"), "service_date"),
            version=version,
            channel_capacity_mw=decimal_value(
                raw.get("channel_capacity_mw"), "channel_capacity_mw", minimum=Decimal("0.001")
            ),
            cable_thermal_limit_mw=decimal_value(
                raw.get("cable_thermal_limit_mw"), "cable_thermal_limit_mw", minimum=Decimal("0.001")
            ),
            compensation_mvar=decimal_value(
                raw.get("compensation_mvar"), "compensation_mvar", minimum=Decimal("0")
            ),
            compensation_ratio=decimal_value(
                raw.get("compensation_ratio", "0"),
                "compensation_ratio",
                minimum=Decimal("0"),
                maximum=Decimal("10"),
            ),
            effective_from=effective_from,
            effective_until=effective_until,
        )


@dataclass(frozen=True, slots=True)
class StationDeclaration:
    declaration_id: str
    route_id: str
    service_date: str
    installed_mw: Decimal
    availability_percent: Decimal
    ramp_points: tuple[RampPoint, ...]
    reserve_mw: Decimal
    respond_by: str
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "StationDeclaration":
        points_raw = raw.get("ramp_points")
        if not isinstance(points_raw, list) or not 2 <= len(points_raw) <= 24:
            raise ValidationFailed("ramp_points 必须包含 2 到 24 个爬坡点")
        points: list[RampPoint] = []
        last_offset = -1
        for index, item in enumerate(points_raw):
            if not isinstance(item, Mapping):
                raise ValidationFailed("ramp_points 元素必须是对象")
            offset = item.get("offset_minutes")
            if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 10080:
                raise ValidationFailed("offset_minutes 必须是 0 到 10080 的整数")
            if offset <= last_offset:
                raise ValidationFailed("ramp_points 的 offset_minutes 必须严格递增")
            last_offset = offset
            points.append(
                RampPoint(offset, decimal_value(item.get("mw"), f"ramp_points[{index}].mw", minimum=Decimal("0")))
            )
        if points[-1].mw <= 0:
            raise ValidationFailed("爬坡曲线终点功率必须大于零")
        respond_by = required_text(raw.get("respond_by"), "respond_by", 40)
        try:
            parse_utc(respond_by, "respond_by")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        return cls(
            declaration_id=identifier(raw.get("declaration_id"), "declaration_id"),
            route_id=identifier(raw.get("route_id"), "route_id"),
            service_date=date_text(raw.get("service_date"), "service_date"),
            installed_mw=decimal_value(raw.get("installed_mw"), "installed_mw", minimum=Decimal("0.001")),
            availability_percent=decimal_value(
                raw.get("availability_percent"),
                "availability_percent",
                minimum=Decimal("0"),
                maximum=Decimal("100"),
            ),
            ramp_points=tuple(points),
            reserve_mw=decimal_value(raw.get("reserve_mw"), "reserve_mw", minimum=Decimal("0")),
            respond_by=respond_by,
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )


@dataclass(frozen=True, slots=True)
class ExemptionRequest:
    plan_id: str
    reason: str
    expires_at: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ExemptionRequest":
        expires_at = required_text(raw.get("expires_at"), "expires_at", 40)
        try:
            parse_utc(expires_at, "expires_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        return cls(
            plan_id=identifier(raw.get("plan_id"), "plan_id"),
            reason=required_text(raw.get("reason"), "reason", 512),
            expires_at=expires_at,
        )


@dataclass(frozen=True, slots=True)
class ReceiptSubmission:
    receipt_id: str
    idempotency_key: str
    actuals: Mapping[str, Decimal]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ReceiptSubmission":
        actuals_raw = raw.get("phase_actuals")
        if not isinstance(actuals_raw, Mapping) or not actuals_raw:
            raise ValidationFailed("phase_actuals 必须是非空对象")
        actuals: dict[str, Decimal] = {}
        for phase, value in actuals_raw.items():
            if phase not in COMMITMENT_PHASES:
                raise ValidationFailed(f"未知执行阶段 {phase}")
            actuals[phase] = decimal_value(value, f"phase_actuals.{phase}", minimum=Decimal("0"))
        missing = [phase for phase in COMMITMENT_PHASES if phase not in actuals]
        if missing:
            raise ValidationFailed(f"phase_actuals 缺少阶段 {','.join(missing)}")
        return cls(
            receipt_id=identifier(raw.get("receipt_id"), "receipt_id"),
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
            actuals=actuals,
        )
