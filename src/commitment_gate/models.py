"""送出功率分阶段承诺门禁的领域输入契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from .clock import parse_utc
from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")


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


def percent_value(value: object, field: str, *, allow_zero: bool = True) -> Decimal:
    lower = Decimal("0") if allow_zero else Decimal("0.001")
    return decimal_value(value, field, minimum=lower, maximum=Decimal("100"))


def timestamp_text(value: object, field: str) -> str:
    text = required_text(value, field, 40)
    try:
        return parse_utc(text, field).isoformat().replace("+00:00", "Z")
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class MaintenanceWindow:
    starts_at: str
    ends_at: str
    capacity_percent: Decimal
    reason: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], index: int) -> "MaintenanceWindow":
        if not isinstance(raw, Mapping):
            raise ValidationFailed(f"maintenance_windows[{index}] 必须是对象")
        starts_at = timestamp_text(raw.get("starts_at"), f"maintenance_windows[{index}].starts_at")
        ends_at = timestamp_text(raw.get("ends_at"), f"maintenance_windows[{index}].ends_at")
        if parse_utc(ends_at) <= parse_utc(starts_at):
            raise ValidationFailed(f"maintenance_windows[{index}] 的 ends_at 必须晚于 starts_at")
        return cls(
            starts_at=starts_at,
            ends_at=ends_at,
            capacity_percent=percent_value(
                raw.get("capacity_percent"), f"maintenance_windows[{index}].capacity_percent"
            ),
            reason=required_text(raw.get("reason"), f"maintenance_windows[{index}].reason"),
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "starts_at": self.starts_at,
            "ends_at": self.ends_at,
            "capacity_percent": format(self.capacity_percent, "f"),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class DeliveryBoundary:
    """调度人员发布的带版本和有效期的送出边界。"""

    boundary_id: str
    corridor_id: str
    version: int
    effective_from: str
    effective_to: str
    channel_capacity_mw: Decimal
    reactive_compensation_mvar: Decimal
    cable_thermal_limit_mw: Decimal
    maintenance_windows: tuple[MaintenanceWindow, ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "DeliveryBoundary":
        version = raw.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
            raise ValidationFailed("version 必须是正整数")
        effective_from = timestamp_text(raw.get("effective_from"), "effective_from")
        effective_to = timestamp_text(raw.get("effective_to"), "effective_to")
        if parse_utc(effective_to) <= parse_utc(effective_from):
            raise ValidationFailed("effective_to 必须晚于 effective_from")
        windows_raw = raw.get("maintenance_windows", [])
        if not isinstance(windows_raw, Sequence) or isinstance(windows_raw, (str, bytes)):
            raise ValidationFailed("maintenance_windows 必须是数组")
        if len(windows_raw) > 24:
            raise ValidationFailed("maintenance_windows 不能超过 24 段")
        windows = tuple(
            MaintenanceWindow.from_dict(item, index) for index, item in enumerate(windows_raw)
        )
        return cls(
            boundary_id=identifier(raw.get("boundary_id"), "boundary_id"),
            corridor_id=identifier(raw.get("corridor_id"), "corridor_id"),
            version=version,
            effective_from=effective_from,
            effective_to=effective_to,
            channel_capacity_mw=decimal_value(
                raw.get("channel_capacity_mw"), "channel_capacity_mw", minimum=Decimal("0.001")
            ),
            reactive_compensation_mvar=decimal_value(
                raw.get("reactive_compensation_mvar"), "reactive_compensation_mvar", minimum=Decimal("0")
            ),
            cable_thermal_limit_mw=decimal_value(
                raw.get("cable_thermal_limit_mw"), "cable_thermal_limit_mw", minimum=Decimal("0.001")
            ),
            maintenance_windows=windows,
        )


@dataclass(frozen=True, slots=True)
class RampPoint:
    offset_minutes: int
    output_percent: Decimal


@dataclass(frozen=True, slots=True)
class StationDeclaration:
    """场站申报的机组可用率、爬坡曲线、备用要求与最迟响应时刻。"""

    declaration_id: str
    corridor_id: str
    station_id: str
    installed_capacity_mw: Decimal
    availability_percent: Decimal
    ramp_curve: tuple[RampPoint, ...]
    reserve_mw: Decimal
    latest_response_at: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "StationDeclaration":
        curve_raw = raw.get("ramp_curve")
        if not isinstance(curve_raw, Sequence) or isinstance(curve_raw, (str, bytes)) or not curve_raw:
            raise ValidationFailed("ramp_curve 必须是非空数组")
        if len(curve_raw) > 48:
            raise ValidationFailed("ramp_curve 不能超过 48 个折点")
        points: list[RampPoint] = []
        for index, item in enumerate(curve_raw):
            if not isinstance(item, Mapping):
                raise ValidationFailed(f"ramp_curve[{index}] 必须是对象")
            offset = item.get("offset_minutes")
            if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
                raise ValidationFailed(f"ramp_curve[{index}].offset_minutes 必须是非负整数")
            points.append(
                RampPoint(
                    offset_minutes=offset,
                    output_percent=percent_value(
                        item.get("output_percent"), f"ramp_curve[{index}].output_percent"
                    ),
                )
            )
        first, last = points[0], points[-1]
        if first.offset_minutes != 0 or first.output_percent != 0:
            raise ValidationFailed("ramp_curve 必须从 (0, 0%) 开始")
        if last.output_percent != Decimal("100"):
            raise ValidationFailed("ramp_curve 必须以 100% 结束")
        for left, right in zip(points, points[1:]):
            if right.offset_minutes <= left.offset_minutes:
                raise ValidationFailed("ramp_curve 的 offset_minutes 必须严格递增")
            if right.output_percent < left.output_percent:
                raise ValidationFailed("ramp_curve 的 output_percent 不能下降")
        return cls(
            declaration_id=identifier(raw.get("declaration_id"), "declaration_id"),
            corridor_id=identifier(raw.get("corridor_id"), "corridor_id"),
            station_id=identifier(raw.get("station_id"), "station_id"),
            installed_capacity_mw=decimal_value(
                raw.get("installed_capacity_mw"), "installed_capacity_mw", minimum=Decimal("0.001")
            ),
            availability_percent=percent_value(
                raw.get("availability_percent"), "availability_percent", allow_zero=False
            ),
            ramp_curve=tuple(points),
            reserve_mw=decimal_value(raw.get("reserve_mw"), "reserve_mw", minimum=Decimal("0")),
            latest_response_at=timestamp_text(raw.get("latest_response_at"), "latest_response_at"),
        )


@dataclass(frozen=True, slots=True)
class PlanRequest:
    plan_id: str
    corridor_id: str
    station_id: str
    window_start: str
    window_end: str
    requested_mw: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PlanRequest":
        window_start = timestamp_text(raw.get("window_start"), "window_start")
        window_end = timestamp_text(raw.get("window_end"), "window_end")
        if parse_utc(window_end) <= parse_utc(window_start):
            raise ValidationFailed("window_end 必须晚于 window_start")
        return cls(
            plan_id=identifier(raw.get("plan_id"), "plan_id"),
            corridor_id=identifier(raw.get("corridor_id"), "corridor_id"),
            station_id=identifier(raw.get("station_id"), "station_id"),
            window_start=window_start,
            window_end=window_end,
            requested_mw=decimal_value(
                raw.get("requested_mw"), "requested_mw", minimum=Decimal("0.001")
            ),
        )


@dataclass(frozen=True, slots=True)
class ExemptionRequest:
    """紧急保供豁免：必须记录授权人、理由与失效时间。"""

    exemption_id: str
    reason: str
    expires_at: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ExemptionRequest":
        return cls(
            exemption_id=identifier(raw.get("exemption_id"), "exemption_id"),
            reason=required_text(raw.get("reason"), "reason", 512),
            expires_at=timestamp_text(raw.get("expires_at"), "expires_at"),
        )


@dataclass(frozen=True, slots=True)
class ReceiptRequest:
    receipt_id: str
    actual_mwh: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ReceiptRequest":
        return cls(
            receipt_id=identifier(raw.get("receipt_id"), "receipt_id"),
            actual_mwh=decimal_value(raw.get("actual_mwh"), "actual_mwh", minimum=Decimal("0")),
        )
