"""原始 JSON 输入校验。

故意不使用 Pydantic 的隐式转换：坐标和尺寸必须是 JSON 整数，
3.5、"5"、true 都应被拒绝并逐字段报错。

可选工件编号（label）：缺省或 null 视为未填写；字符串去首尾空格后
长度 ≤ 24，且同一批提交内不得重复。编号非法同样按行给出字段错误。

可选定位步长（grid_step）：缺省按 1 毫米处理（旧客户端）；
必须是 1/5/10 之一，非法时顶层 422。给定步长后，每扇开窗的位置与
尺寸还必须对齐刻度，否则按行逐字段报错。
"""
from __future__ import annotations

from typing import Any

from .engine import field_errors, grid_field_errors, normalize_grid_step

MAX_WINDOWS = 500
MAX_LABEL_LENGTH = 24


def _is_json_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def normalize_label(raw: Any) -> str | None:
    """把通过校验的编号规范化为落库值：去首尾空格，空串视为未填写。

    非字符串（含 None）一律返回 None——只有 validate_payload 放行后的
    提交体才应走到这里，非法类型已在校验阶段被逐字段拒绝。
    """
    if not isinstance(raw, str):
        return None
    return raw.strip() or None


def validate_payload(body: Any) -> tuple[list[dict[str, dict[str, str]]], int]:
    """校验提交体，返回 (每个非法开窗的逐字段错误, 规范化后的步长)。

    返回结构：[{"index": int, "fields": {字段: 信息}}, ...]，按行号升序。
    顶层结构错误或 grid_step 非法抛出 ValueError（由路由转成 422）。
    未携带 grid_step 的旧客户端按 1 毫米处理。
    """
    if not isinstance(body, dict):
        raise ValueError("请求体必须是 JSON 对象")
    if "windows" not in body:
        raise ValueError("缺少 windows 字段")
    raw_windows = body["windows"]
    if not isinstance(raw_windows, list):
        raise ValueError("windows 必须是数组")
    if len(raw_windows) > MAX_WINDOWS:
        raise ValueError(f"一次最多提交 {MAX_WINDOWS} 个开窗")

    # grid_step 是顶层可选项：缺省/ null 按 1 毫米，其余非法值整批拒绝
    grid_step = normalize_grid_step(body.get("grid_step"))
    if grid_step is None:
        raise ValueError("grid_step 必须是 1、5 或 10 之一")

    fields_by_index: dict[int, dict[str, str]] = {}
    # 合法编号（去空格、非空、未超长）→ 行号列表，循环结束后统一判重
    label_rows: dict[str, list[int]] = {}

    def add_error(index: int, key: str, message: str) -> None:
        fields_by_index.setdefault(index, {})[key] = message

    for index, item in enumerate(raw_windows):
        if not isinstance(item, dict):
            add_error(index, "_", "每个开窗必须是包含 x/y/w/h 的对象")
            continue

        missing = [key for key in ("x", "y", "w", "h") if key not in item]
        for key in missing:
            add_error(index, key, "缺少该字段")
        if missing:
            continue

        x, y, w, h = item["x"], item["y"], item["w"], item["h"]
        coords_ok = all(_is_json_int(v) for v in (x, y, w, h))
        if not coords_ok:
            for key, v in (("x", x), ("y", y), ("w", w), ("h", h)):
                if not _is_json_int(v):
                    add_error(index, key, "必须为整数")
        else:
            for key, message in field_errors(x, y, w, h).items():
                add_error(index, key, message)
            # 刻度对齐：一个字段只保留一条错误，
            # 越界/非正尺寸等更基础的错误不被刻度信息覆盖。
            for key, message in grid_field_errors(x, y, w, h, grid_step).items():
                fields_by_index.setdefault(index, {}).setdefault(key, message)

        # --- 可选工件编号 ---
        raw_label = item.get("label")
        if raw_label is None:
            continue  # 未携带编号（旧客户端）或显式 null：按空值处理
        if not isinstance(raw_label, str):
            add_error(index, "label", "编号必须为字符串")
            continue
        trimmed = raw_label.strip()
        if len(trimmed) > MAX_LABEL_LENGTH:
            add_error(index, "label", f"编号长度不能超过 {MAX_LABEL_LENGTH} 个字符")
        elif trimmed:
            label_rows.setdefault(trimmed, []).append(index)

    # 同一布局内编号不重复：涉及重复编号的每一行都报编号字段错误
    for indexes in label_rows.values():
        if len(indexes) > 1:
            for index in indexes:
                add_error(index, "label", "编号与其他开窗重复")

    return [{"index": i, "fields": fields_by_index[i]} for i in sorted(fields_by_index)], grid_step
