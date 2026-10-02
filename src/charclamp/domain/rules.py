"""炭窑焖烧志业务规则。"""

from __future__ import annotations

import math

from charclamp.domain.models import BurnShift, Clamp

MIN_PEAK_TEMP_FOR_DRAWN = 400.0
MIN_PEAK_TEMP_INPUT = 0.0  # 峰值温度必须为正数（0/负数无物理意义，不得入库）


class RuleError(ValueError):
    """业务规则校验失败。"""


def latest_shift_for_clamp(clamp: Clamp) -> BurnShift | None:
    if not clamp.shifts:
        return None
    return max(clamp.shifts, key=lambda s: s.started_at)


def can_mark_clamp_drawn(clamp: Clamp) -> tuple[bool, str]:
    """
    炭窑转为「已出炭」(drawn) 的前提：
    最近一条焖烧班次的峰值温度已记录，且 >= 400℃。
    """
    latest = latest_shift_for_clamp(clamp)
    if latest is None:
        return False, "该窑尚无焖烧班次，不能标记为已出炭"
    if latest.peak_temp_c is None:
        return False, "最近班次尚未记录峰值温度，不能标记为已出炭"
    if latest.peak_temp_c < MIN_PEAK_TEMP_FOR_DRAWN:
        return (
            False,
            f"最近班次峰值温度 {latest.peak_temp_c}℃ 低于 {MIN_PEAK_TEMP_FOR_DRAWN:.0f}℃，不能标记为已出炭",
        )
    return True, ""


def assert_can_set_clamp_status(clamp: Clamp, new_status: str) -> None:
    allowed = {Clamp.STATUS_STACKED, Clamp.STATUS_BURNING, Clamp.STATUS_DRAWN}
    if new_status not in allowed:
        raise RuleError(f"无效状态：{new_status}")
    if new_status == Clamp.STATUS_DRAWN:
        ok, msg = can_mark_clamp_drawn(clamp)
        if not ok:
            raise RuleError(msg)


def parse_peak_temp(raw: str | float | None) -> float | None:
    """解析表单提交的峰值温度。

    空值 -> None（表示「未测」，允许）；否则必须是有限正数，
    非数字、NaN/Inf、0 或负数一律抛 RuleError，不得入库。
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if text == "":
        return None
    try:
        value = float(text)
    except (TypeError, ValueError):
        raise RuleError("峰值温度必须是数字")
    if not math.isfinite(value):
        raise RuleError("峰值温度必须是有限数字")
    if value <= MIN_PEAK_TEMP_INPUT:
        raise RuleError("峰值温度必须为正数，不能为 0 或负数")
    return value
