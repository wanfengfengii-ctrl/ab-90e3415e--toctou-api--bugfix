#!/usr/bin/env python3
"""一次性端到端验收脚本（仅用标准库）。

对运行中的真实容器栈打 HTTP 请求：
  - 经 api 直连验证裁决、逐字段错误、整次不落库、刷新恢复；
  - 经 web（nginx 反代）验证前端页面与 /api 代理接线。

任一断言失败即以非零码退出。环境变量：
  API_URL（默认 http://api:8000）
  WEB_URL（默认 http://web）
"""
import json
import os
import sys
import urllib.error
import urllib.request

API_URL = os.getenv("API_URL", "http://api:8000").rstrip("/")
WEB_URL = os.getenv("WEB_URL", "http://web").rstrip("/")

PASS = 0
FAILURES = []


def check(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAILURES.append(name)
        print(f"  FAIL  {name}  {detail}")


def request(method, url, body=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def section(title):
    print(f"\n== {title} ==")


def main():
    section("1. 健康检查与内置只读瑕疵区")
    status, body = request("GET", f"{API_URL}/api/health")
    check("GET /api/health → 200 ok", status == 200 and body == {"status": "ok"}, body)

    status, body = request("GET", f"{API_URL}/api/defects")
    check(
        "瑕疵区固定为两个半开矩形、纸张 1000×700、边距 12",
        status == 200
        and body["defects"]
        == [
            {"index": 0, "x": 200, "y": 150, "w": 80, "h": 40},
            {"index": 1, "x": 620, "y": 420, "w": 60, "h": 90},
        ]
        and body["sheet"] == {"width": 1000, "height": 700, "margin": 12},
        body,
    )

    section("2. 非法提交：逐字段错误且整次不落库")
    status, before = request("GET", f"{API_URL}/api/layout")
    before_count = len(before["windows"])

    status, body = request(
        "PUT",
        f"{API_URL}/api/layout",
        {"windows": [{"x": 0, "y": 0, "w": 0, "h": 0}]},
    )
    check(
        "四字段全非法 → 422 且逐字段返回 x/y/w/h",
        status == 422
        and body["field_errors"][0]["index"] == 0
        and set(body["field_errors"][0]["fields"]) == {"x", "y", "w", "h"},
        body,
    )

    status, body = request(
        "PUT",
        f"{API_URL}/api/layout",
        {"windows": [{"x": 300.5, "y": "50", "w": 10, "h": True}]},
    )
    check("非整数 JSON 值（300.5/\"50\"/true）→ 422", status == 422, body)

    status, body = request(
        "PUT",
        f"{API_URL}/api/layout",
        {
            "windows": [
                {"x": 300, "y": 300, "w": 10, "h": 10},
                {"x": 300, "y": 900, "w": 10, "h": 10},
            ]
        },
    )
    check(
        "一批中任一开窗非法 → 422 且带正确行号",
        status == 422 and [r["index"] for r in body["field_errors"]] == [1],
        body,
    )

    status, after_bad = request("GET", f"{API_URL}/api/layout")
    check(
        "非法提交后开窗数量不变（整次不落库）",
        len(after_bad["windows"]) == before_count,
        f"{len(after_bad['windows'])} != {before_count}",
    )

    section("3. 合法干净方案 → 可裁切并落库")
    clean = [{"x": 300, "y": 300, "w": 100, "h": 80}, {"x": 500, "y": 100, "w": 40, "h": 40}]
    status, body = request("PUT", f"{API_URL}/api/layout", {"windows": clean})
    check("提交成功", status == 200, body)
    check("唯一结论为 可裁切", body.get("verdict") == "可裁切", body)
    check(
        "返回的开窗坐标与提交一致且含数据库 id/position",
        [(w["x"], w["y"], w["w"], w["h"]) for w in body["windows"]]
        == [(300, 300, 100, 80), (500, 100, 40, 40)]
        and all(isinstance(w["id"], int) for w in body["windows"]),
        body,
    )

    section("4. 刷新恢复同一布局")
    status, restored = request("GET", f"{API_URL}/api/layout")
    check("GET 恢复全部开窗与次序", status == 200 and len(restored["windows"]) == 2, restored)
    check(
        "恢复的坐标一致",
        [(w["x"], w["y"], w["w"], w["h"]) for w in restored["windows"]]
        == [(300, 300, 100, 80), (500, 100, 40, 40)],
        restored,
    )
    check("恢复的裁决仍为 可裁切", restored["verdict"] == "可裁切", restored)

    section("5. 瑕疵区冲突（半开矩形）")
    status, body = request(
        "PUT",
        f"{API_URL}/api/layout",
        {"windows": [{"x": 250, "y": 170, "w": 60, "h": 40}]},
    )
    win_id = body["windows"][0]["id"]
    check(
        "正面积侵入瑕疵区 → 不可裁切，明细指出瑕疵 0",
        body["verdict"] == "不可裁切"
        and body["result"]["defect_conflicts"]
        == [{"window_id": win_id, "defect_index": 0}]
        and body["result"]["conflicting_window_ids"] == [win_id],
        body,
    )
    check("冲突方案同样落库（刷新仍为不可裁切）",
          request("GET", f"{API_URL}/api/layout")[1]["verdict"] == "不可裁切")

    # 边线/角点相接允许
    for rect, label in [
        ({"x": 280, "y": 150, "w": 10, "h": 40}, "右边线相接"),
        ({"x": 200, "y": 190, "w": 80, "h": 10}, "下边线相接"),
        ({"x": 280, "y": 190, "w": 5, "h": 5}, "角点相接"),
    ]:
        status, body = request("PUT", f"{API_URL}/api/layout", {"windows": [rect]})
        check(f"{label} → 可裁切", body["verdict"] == "可裁切", body)

    section("6. 开窗之间冲突（双方高亮）")
    status, body = request(
        "PUT",
        f"{API_URL}/api/layout",
        {
            "windows": [
                {"x": 300, "y": 300, "w": 100, "h": 100},
                {"x": 350, "y": 350, "w": 100, "h": 100},
            ]
        },
    )
    ids = [w["id"] for w in body["windows"]]
    check(
        "重叠 → 不可裁切，双方进入冲突列表",
        body["verdict"] == "不可裁切"
        and body["result"]["window_conflicts"]
        == [{"window_a": ids[0], "window_b": ids[1]}]
        and body["result"]["conflicting_window_ids"] == sorted(ids),
        body,
    )
    status, body = request(
        "PUT",
        f"{API_URL}/api/layout",
        {
            "windows": [
                {"x": 300, "y": 300, "w": 50, "h": 50},
                {"x": 350, "y": 300, "w": 50, "h": 50},
            ]
        },
    )
    check("开窗边线相接 → 可裁切", body["verdict"] == "可裁切", body)

    section("7. 安全区边界")
    status, body = request(
        "PUT",
        f"{API_URL}/api/layout",
        {"windows": [{"x": 12, "y": 600, "w": 976, "h": 88}]},
    )
    check("贴边 12 / x+w=988 / y+h=688 → 可裁切", body["verdict"] == "可裁切", body)
    status, body = request(
        "PUT",
        f"{API_URL}/api/layout",
        {"windows": [{"x": 12, "y": 12, "w": 977, "h": 10}]},
    )
    check("x+w=989 越界 → 422", status == 422, body)

    section("8. 工件编号（可选，布局内唯一）")
    # 两个合法编号保存后刷新原样恢复
    labeled = [
        {"x": 300, "y": 300, "w": 100, "h": 80, "label": "ZW-2026-001"},
        {"x": 500, "y": 100, "w": 40, "h": 40, "label": "ZW-2026-002"},
    ]
    status, body = request("PUT", f"{API_URL}/api/layout", {"windows": labeled})
    check("带编号提交成功", status == 200, body)
    check(
        "响应带回两个编号",
        [w.get("label") for w in body["windows"]] == ["ZW-2026-001", "ZW-2026-002"],
        body,
    )
    status, restored = request("GET", f"{API_URL}/api/layout")
    check(
        "刷新后两个编号原样恢复",
        [w.get("label") for w in restored["windows"]] == ["ZW-2026-001", "ZW-2026-002"],
        restored,
    )

    # 首尾空格被去除后落库
    status, body = request(
        "PUT",
        f"{API_URL}/api/layout",
        {"windows": [{"x": 300, "y": 300, "w": 10, "h": 10, "label": "  TRIM-1  "}]},
    )
    check(
        "编号去首尾空格后保存",
        status == 200 and body["windows"][0]["label"] == "TRIM-1",
        body,
    )

    # 空编号按空值处理（未携带 / null / 全空格）
    status, body = request(
        "PUT",
        f"{API_URL}/api/layout",
        {
            "windows": [
                {"x": 300, "y": 300, "w": 10, "h": 10},
                {"x": 500, "y": 300, "w": 10, "h": 10, "label": None},
                {"x": 700, "y": 300, "w": 10, "h": 10, "label": "   "},
            ]
        },
    )
    check(
        "未携带/null/全空格编号 → 存为空值（页面将显示顺序号）",
        status == 200 and [w["label"] for w in body["windows"]] == [None, None, None],
        body,
    )

    # 重复编号 → 逐行 422 且最新记录不变
    status, before = request("GET", f"{API_URL}/api/layout")
    status, body = request(
        "PUT",
        f"{API_URL}/api/layout",
        {
            "windows": [
                {"x": 300, "y": 300, "w": 10, "h": 10, "label": "DUP"},
                {"x": 500, "y": 300, "w": 10, "h": 10},
                {"x": 700, "y": 300, "w": 10, "h": 10, "label": "DUP"},
            ]
        },
    )
    check(
        "重复编号 → 422 且涉及的两行都报编号字段错误",
        status == 422
        and [r["index"] for r in body["field_errors"]] == [0, 2]
        and all("label" in r["fields"] for r in body["field_errors"]),
        body,
    )
    status, after = request("GET", f"{API_URL}/api/layout")
    check("重复编号提交整批不落库（最新记录不变）", after == before, after)

    # 超长编号 → 422 且整批不落库
    status, body = request(
        "PUT",
        f"{API_URL}/api/layout",
        {"windows": [{"x": 300, "y": 300, "w": 10, "h": 10, "label": "X" * 25}]},
    )
    check(
        "编号超过 24 字符 → 422 且报编号字段",
        status == 422 and "label" in body["field_errors"][0]["fields"],
        body,
    )
    status, after = request("GET", f"{API_URL}/api/layout")
    check("超长编号提交整批不落库", after == before, after)

    # 带编号的重叠开窗：冲突双方即两编号开窗，刷新后编号仍在
    status, body = request(
        "PUT",
        f"{API_URL}/api/layout",
        {
            "windows": [
                {"x": 300, "y": 300, "w": 100, "h": 100, "label": "WIN-A"},
                {"x": 350, "y": 350, "w": 100, "h": 100, "label": "WIN-B"},
            ]
        },
    )
    ids_by_label = {w["label"]: w["id"] for w in body["windows"]}
    check(
        "带编号重叠开窗 → 不可裁切，冲突双方即两编号开窗",
        body["verdict"] == "不可裁切"
        and body["result"]["window_conflicts"]
        == [{"window_a": ids_by_label["WIN-A"], "window_b": ids_by_label["WIN-B"]}]
        and body["result"]["conflicting_window_ids"]
        == sorted(ids_by_label.values()),
        body,
    )
    status, restored = request("GET", f"{API_URL}/api/layout")
    check(
        "刷新后重叠开窗的编号与裁决一并恢复",
        [w["label"] for w in restored["windows"]] == ["WIN-A", "WIN-B"]
        and restored["verdict"] == "不可裁切",
        restored,
    )

    section("9. 定位步长（1/5/10 毫米）")
    # 旧客户端：不提交 grid_step，旧格式请求照常成功，并按 1 毫米处理
    status, body = request(
        "PUT",
        f"{API_URL}/api/layout",
        {"windows": [{"x": 13, "y": 14, "w": 7, "h": 9}]},
    )
    check(
        "旧格式（无 grid_step）请求成功保存，响应按 1 毫米处理",
        status == 200 and body["grid_step"] == 1,
        body,
    )

    # 5 毫米对齐方案（以安全内区左上角 12 为基准）：保存并恢复选择项与坐标
    aligned = [{"x": 217, "y": 302, "w": 45, "h": 35}]
    status, body = request("PUT", f"{API_URL}/api/layout", {"grid_step": 5, "windows": aligned})
    check("5 毫米步长 + 刻度对齐开窗 → 200，步长随布局保存", status == 200
          and body["grid_step"] == 5 and body["verdict"] == "可裁切", body)
    saved5 = body
    status, restored = request("GET", f"{API_URL}/api/layout")
    check(
        "刷新后恢复 5 毫米选择项、同一坐标、结论与冲突高亮结果",
        status == 200
        and restored["grid_step"] == 5
        and [(w["x"], w["y"], w["w"], w["h"]) for w in restored["windows"]] == [(217, 302, 45, 35)]
        and restored["verdict"] == saved5["verdict"]
        and restored["result"] == saved5["result"],
        restored,
    )

    # 偏离刻度：按行返回字段错误，整次不落库
    status, before = request("GET", f"{API_URL}/api/layout")
    status, body = request(
        "PUT",
        f"{API_URL}/api/layout",
        {
            "grid_step": 5,
            "windows": [
                {"x": 300, "y": 301, "w": 11, "h": 13},  # 四字段全偏离
                {"x": 12, "y": 12, "w": 5, "h": 5},       # 合法行
            ],
        },
    )
    check(
        "偏离 5 毫米刻度 → 422，按行报 x/y/w/h 字段错误",
        status == 422
        and [r["index"] for r in body["field_errors"]] == [0]
        and set(body["field_errors"][0]["fields"]) == {"x", "y", "w", "h"},
        body,
    )
    status, after = request("GET", f"{API_URL}/api/layout")
    check("偏离刻度提交整批不落库（最新记录未改变）", after == before, after)

    # 非法步长：顶层 422 且不落库
    for bad in (3, 0, "5", 5.0):
        status, body = request(
            "PUT", f"{API_URL}/api/layout", {"grid_step": bad, "windows": []}
        )
        check(f"非法步长 {bad!r} → 422", status == 422 and "grid_step" in body["detail"], body)
    status, after = request("GET", f"{API_URL}/api/layout")
    check("非法步长提交不产生新布局", after == before, after)

    # 同一组坐标：1 毫米合法、5 毫米被打回；10 毫米边界刻度
    status, body = request(
        "PUT", f"{API_URL}/api/layout", {"grid_step": 1, "windows": [{"x": 13, "y": 16, "w": 7, "h": 3}]}
    )
    check("同组坐标在 1 毫米下合法", status == 200, body)
    status, body = request(
        "PUT", f"{API_URL}/api/layout", {"grid_step": 5, "windows": [{"x": 13, "y": 16, "w": 7, "h": 3}]}
    )
    check("同组坐标在 5 毫米下 422", status == 422, body)
    status, body = request(
        "PUT",
        f"{API_URL}/api/layout",
        {"grid_step": 10, "windows": [{"x": 972, "y": 672, "w": 10, "h": 10}]},
    )
    check("10 毫米下容得下整窗的最后刻度（972/672）→ 可裁切",
          status == 200 and body["verdict"] == "可裁切", body)

    # 空布局 GET 在任何时候都带合法步长（旧记录缺值也按 1）
    check("GET 始终返回合法 grid_step", restored["grid_step"] in (1, 5, 10), restored)

    section("10. 经 web（nginx）反代的端到端接线")
    try:
        with urllib.request.urlopen(f"{WEB_URL}/", timeout=10) as resp:
            html = resp.read().decode()
        check(
            "GET web 根路径返回前端页面",
            resp.status == 200 and "档案装裱排版校验台" in html,
            resp.status,
        )
    except Exception as exc:  # noqa: BLE001
        check("GET web 根路径返回前端页面", False, repr(exc))

    status, body = request("GET", f"{WEB_URL}/api/defects")
    check("web → /api 代理可用（瑕疵区经代理返回）", status == 200 and len(body["defects"]) == 2, body)
    status, body = request(
        "PUT",
        f"{WEB_URL}/api/layout",
        {"windows": [{"x": 700, "y": 100, "w": 30, "h": 30}]},
    )
    check("经 web 代理提交方案 → 可裁切", status == 200 and body["verdict"] == "可裁切", body)

    print(f"\n{'=' * 50}")
    if FAILURES:
        print(f"验收失败：{len(FAILURES)} 项失败，{PASS} 项通过")
        for name in FAILURES:
            print(f"  - {name}")
        sys.exit(1)
    print(f"验收通过：{PASS} 项全部通过")


if __name__ == "__main__":
    main()
