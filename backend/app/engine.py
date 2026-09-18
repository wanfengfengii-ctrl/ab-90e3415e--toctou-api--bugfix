"""裁决引擎：开窗合法性、瑕疵区冲突、开窗之间冲突。

坐标约定
--------
纸张固定 1000×700 毫米，左上角为原点，x 轴向右、y 轴向下。
所有矩形均为半开矩形 [x, x+w) × [y, y+h)：
只有正面积相交才算冲突，边线或角点相接允许。

瑕疵区内置、只读：
  [200,280) × [150,190)
  [620,680) × [420,510)
可用内区（压边安全区向内）：
  12 ≤ x，x+w ≤ 988，12 ≤ y，y+h ≤ 688。
"""
from __future__ import annotations

from typing import Iterable, List, Sequence

# --- 纸张与安全区常量（毫米） ---
SHEET_WIDTH = 1000
SHEET_HEIGHT = 700
MARGIN = 12
INNER_RIGHT = SHEET_WIDTH - MARGIN   # 988
INNER_BOTTOM = SHEET_HEIGHT - MARGIN  # 688

# 内置只读瑕疵区（半开矩形）
DEFECTS: tuple[tuple[int, int, int, int], ...] = (
    (200, 150, 80, 40),   # [200,280) × [150,190)
    (620, 420, 60, 90),   # [620,680) × [420,510)
)

VERDICT_CUTTABLE = "可裁切"
VERDICT_REJECTED = "不可裁切"

# 可选定位步长（毫米）；技师可在 1 / 5 / 10 毫米刻度间切换。
# 旧客户端与旧记录一律按 DEFAULT_GRID_STEP（1 毫米）处理。
GRID_STEPS: tuple[int, ...] = (1, 5, 10)
DEFAULT_GRID_STEP = 1


def normalize_grid_step(raw) -> int | None:
    """把提交体中的 grid_step 规范化为合法步长。

    缺省（None）按 1 毫米处理；必须是 JSON 整数（True/False 不算）
    且取值 1/5/10。非法值返回 None，由上层报顶层 422。
    """
    if raw is None:
        return DEFAULT_GRID_STEP
    if isinstance(raw, int) and not isinstance(raw, bool) and raw in GRID_STEPS:
        return raw
    return None


def grid_field_errors(x, y, w, h, step: int) -> dict[str, str]:
    """逐字段检查开窗是否对齐指定步长刻度。

    吸附以安全内区左上角 (MARGIN, MARGIN) 为坐标基准：
    位置 x/y 必须满足 (坐标 - 12) 为步长整数倍；尺寸 w/h 本身
    必须为步长整数倍（这样开窗两条边都落在刻度线上）。
    仅在坐标本身为合法整数时调用——非整数等错误由 field_errors 报出。
    """
    errors: dict[str, str] = {}
    if (x - MARGIN) % step != 0:
        errors["x"] = f"x 必须对齐 {step} 毫米刻度（以安全内区左上角为基准）"
    if (y - MARGIN) % step != 0:
        errors["y"] = f"y 必须对齐 {step} 毫米刻度（以安全内区左上角为基准）"
    if w % step != 0:
        errors["w"] = f"宽必须为 {step} 毫米的整数倍"
    if h % step != 0:
        errors["h"] = f"高必须为 {step} 毫米的整数倍"
    return errors


def rects_positive_overlap(a, b) -> bool:
    """两个半开矩形是否有正面积相交。

    边/角相接（任一维上的相交长度为 0）不算冲突。
    矩形统一表示为 (x, y, w, h)。
    """
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    overlap_w = min(ax + aw, bx + bw) - max(ax, bx)
    overlap_h = min(ay + ah, by + bh) - max(ay, by)
    return overlap_w > 0 and overlap_h > 0


def field_errors(x, y, w, h) -> dict[str, str]:
    """逐字段校验。返回 {字段: 错误信息}，合法时为空 dict。

    非法的可能是：不是整数（None/非 int）、宽高小于 1、
    坐标为负、越过压边安全区。
    """
    errors: dict[str, str] = {}

    def is_int(v) -> bool:
        # bool 是 int 的子类，但 True/False 显然不是合法坐标
        return isinstance(v, int) and not isinstance(v, bool)

    if not is_int(x):
        errors["x"] = "x 必须为整数"
    elif x < MARGIN:
        errors["x"] = f"x 必须 ≥ {MARGIN}（压边安全区）"

    if not is_int(y):
        errors["y"] = "y 必须为整数"
    elif y < MARGIN:
        errors["y"] = f"y 必须 ≥ {MARGIN}（压边安全区）"

    if not is_int(w):
        errors["w"] = "宽必须为整数"
    elif w < 1:
        errors["w"] = "宽至少为 1"

    if not is_int(h):
        errors["h"] = "高必须为整数"
    elif h < 1:
        errors["h"] = "高至少为 1"

    # 右/下边界同时涉及两个字段；起点与尺寸本身合法时才检查，
    # 避免在一个字段上堆叠多条错误，同时保证一次提交报出全部非法字段。
    if is_int(x) and is_int(w) and w >= 1 and x >= MARGIN and x + w > INNER_RIGHT:
        errors["x"] = f"x+宽 必须 ≤ {INNER_RIGHT}（压边安全区）"
    if is_int(y) and is_int(h) and h >= 1 and y >= MARGIN and y + h > INNER_BOTTOM:
        errors["y"] = f"y+高 必须 ≤ {INNER_BOTTOM}（压边安全区）"

    return errors


def find_defect_conflicts(window) -> list[int]:
    """返回与该开窗正面积相交的瑕疵区索引（0 起）。"""
    return [i for i, d in enumerate(DEFECTS) if rects_positive_overlap(window, d)]


def find_pair_conflicts(windows: Sequence[tuple]) -> list[tuple[int, int]]:
    """返回开窗之间正面积相交的索引对 (i, j)，i < j。"""
    pairs: list[tuple[int, int]] = []
    for i in range(len(windows)):
        for j in range(i + 1, len(windows)):
            if rects_positive_overlap(windows[i], windows[j]):
                pairs.append((i, j))
    return pairs


def adjudicate(windows: Iterable[dict]) -> dict:
    """对一批开窗做完整裁决。

    输入：[{"id":..., "x":int, "y":int, "w":int, "h":int}, ...]
    输出完整裁决结果（可直接落库/返回前端）：
      verdict: 可裁切 | 不可裁切
      defect_conflicts: [{window_id, defect_index}]
      window_conflicts: [{window_a, window_b}]
      conflicting_window_ids: 涉及任何冲突的开窗 id（高亮用）
    """
    indexed: list[tuple[int, tuple]] = [
        (win["id"], (win["x"], win["y"], win["w"], win["h"]))
        for win in windows
    ]
    rects = [r for _, r in indexed]

    defect_conflicts = []
    conflicting: set[int] = set()
    for win_id, rect in indexed:
        for di in find_defect_conflicts(rect):
            defect_conflicts.append({"window_id": win_id, "defect_index": di})
            conflicting.add(win_id)

    window_conflicts = []
    for i, j in find_pair_conflicts(rects):
        a, b = indexed[i][0], indexed[j][0]
        window_conflicts.append({"window_a": a, "window_b": b})
        conflicting.add(a)
        conflicting.add(b)

    verdict = VERDICT_CUTTABLE if not (defect_conflicts or window_conflicts) else VERDICT_REJECTED
    return {
        "verdict": verdict,
        "defect_conflicts": defect_conflicts,
        "window_conflicts": window_conflicts,
        "conflicting_window_ids": sorted(conflicting),
    }
