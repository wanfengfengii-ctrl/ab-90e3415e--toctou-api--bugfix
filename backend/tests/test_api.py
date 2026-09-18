"""API 集成测试：逐字段错误、整次不落库、合法保存与刷新恢复。"""
import pytest


def _put(client, windows, *, grid_step=...):
    body = {"windows": windows}
    if grid_step is not ...:
        body["grid_step"] = grid_step
    return client.put("/api/layout", json=body)


def test_health(client):
    assert client.get("/api/health").json() == {"status": "ok"}


def test_defects_endpoint_exposes_builtin_readonly_zones(client):
    data = client.get("/api/defects").json()
    assert data["defects"] == [
        {"index": 0, "x": 200, "y": 150, "w": 80, "h": 40},
        {"index": 1, "x": 620, "y": 420, "w": 60, "h": 90},
    ]
    assert data["sheet"] == {"width": 1000, "height": 700, "margin": 12}


def test_empty_layout_before_any_submission(client):
    data = client.get("/api/layout").json()
    assert data == {
        "verdict": None,
        "grid_step": 1,
        "windows": [],
        "result": None,
        "created_at": None,
    }


def test_valid_clean_submission_persists_and_is_cuttable(client):
    r = _put(client, [{"x": 300, "y": 300, "w": 100, "h": 80}])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verdict"] == "可裁切"
    assert body["result"]["conflicting_window_ids"] == []
    assert len(body["windows"]) == 1
    assert body["windows"][0]["x"] == 300


def test_refresh_restores_same_layout(client):
    _put(client, [
        {"x": 300, "y": 300, "w": 100, "h": 80},
        {"x": 500, "y": 100, "w": 40, "h": 40},
    ])
    data = client.get("/api/layout").json()
    assert data["verdict"] == "可裁切"
    assert [(w["x"], w["y"], w["w"], w["h"]) for w in data["windows"]] == [
        (300, 300, 100, 80),
        (500, 100, 40, 40),
    ]
    # 提交次序保留
    assert [w["position"] for w in data["windows"]] == [0, 1]


def test_latest_submission_wins(client):
    _put(client, [{"x": 300, "y": 300, "w": 10, "h": 10}])
    _put(client, [{"x": 500, "y": 500, "w": 20, "h": 20}])
    data = client.get("/api/layout").json()
    assert len(data["windows"]) == 1
    assert data["windows"][0]["x"] == 500


def test_defect_conflict_is_not_cuttable_but_persisted(client):
    # 与瑕疵区正面积相交：方案仍保存，裁决为不可裁切
    r = _put(client, [{"x": 250, "y": 170, "w": 60, "h": 40}])
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"] == "不可裁切"
    win_id = body["windows"][0]["id"]
    assert body["result"]["defect_conflicts"] == [
        {"window_id": win_id, "defect_index": 0}
    ]
    assert body["result"]["conflicting_window_ids"] == [win_id]

    again = client.get("/api/layout").json()
    assert again["verdict"] == "不可裁切"
    assert again["result"]["defect_conflicts"][0]["window_id"] == again["windows"][0]["id"]


def test_overlapping_windows_both_highlighted(client):
    r = _put(client, [
        {"x": 300, "y": 300, "w": 100, "h": 100},
        {"x": 350, "y": 350, "w": 100, "h": 100},
    ])
    body = r.json()
    assert body["verdict"] == "不可裁切"
    ids = [w["id"] for w in body["windows"]]
    assert body["result"]["window_conflicts"] == [
        {"window_a": ids[0], "window_b": ids[1]}
    ]
    assert body["result"]["conflicting_window_ids"] == sorted(ids)


def test_edge_touching_windows_cuttable(client):
    r = _put(client, [
        {"x": 300, "y": 300, "w": 50, "h": 50},
        {"x": 350, "y": 300, "w": 50, "h": 50},
    ])
    assert r.status_code == 200
    assert r.json()["verdict"] == "可裁切"


def test_boundary_values_accepted(client):
    # 贴满安全区左右/下边界的底部长条：[12,988) × [600,688)，不触瑕疵区
    r = _put(client, [{"x": 12, "y": 600, "w": 976, "h": 88}])
    assert r.status_code == 200, r.text
    assert r.json()["verdict"] == "可裁切"


# ---------- 非法提交：逐字段错误且整次不落库 ----------

def test_field_level_errors_and_no_persistence(client):
    r = _put(client, [
        {"x": 0, "y": 0, "w": 0, "h": 0},  # 四个字段全非法
    ])
    assert r.status_code == 422
    errs = r.json()["field_errors"]
    assert errs[0]["index"] == 0
    assert set(errs[0]["fields"].keys()) == {"x", "y", "w", "h"}

    # 整次不落库
    assert client.get("/api/layout").json()["windows"] == []


def test_error_reports_correct_row_index(client):
    r = _put(client, [
        {"x": 300, "y": 300, "w": 10, "h": 10},  # 合法
        {"x": 300, "y": 300, "w": -5, "h": 10},  # w 非法
        {"x": 300, "y": 900, "w": 10, "h": 10},  # y+h 越界
    ])
    assert r.status_code == 422
    rows = r.json()["field_errors"]
    assert [row["index"] for row in rows] == [1, 2]
    assert set(rows[0]["fields"]) == {"w"}
    assert set(rows[1]["fields"]) == {"y"}


def test_any_bad_row_rolls_back_whole_batch(client):
    r = _put(client, [
        {"x": 300, "y": 300, "w": 10, "h": 10},
        {"x": "300", "y": 300, "w": 10, "h": 10},
    ])
    assert r.status_code == 422
    assert client.get("/api/layout").json()["windows"] == []


def test_non_integer_json_types_rejected(client):
    for bad in ("300", 3.5, True, None):
        r = _put(client, [{"x": bad, "y": 300, "w": 10, "h": 10}])
        assert r.status_code == 422, bad
        assert "x" in r.json()["field_errors"][0]["fields"]


def test_float_that_is_integral_value_still_rejected(client):
    # 300.0 不是 JSON 整数
    r = _put(client, [{"x": 300.0, "y": 300, "w": 10.0, "h": 10}])
    assert r.status_code == 422
    assert set(r.json()["field_errors"][0]["fields"]) == {"x", "w"}


def test_missing_field_reported(client):
    r = client.put("/api/layout", json={"windows": [{"x": 300, "y": 300, "w": 10}]})
    assert r.status_code == 422
    assert r.json()["field_errors"][0]["fields"] == {"h": "缺少该字段"}


def test_malformed_body(client):
    r = client.put("/api/layout", content="not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 422


def test_windows_must_be_array(client):
    r = client.put("/api/layout", json={"windows": {"x": 1}})
    assert r.status_code == 422


def test_empty_batch_is_valid_cuttable(client):
    r = _put(client, [])
    assert r.status_code == 200
    assert r.json()["verdict"] == "可裁切"


# ---------- 可选工件编号 ----------

def test_labels_saved_and_restored_after_refresh(client):
    r = _put(client, [
        {"x": 300, "y": 300, "w": 100, "h": 80, "label": "ZW-2026-001"},
        {"x": 500, "y": 100, "w": 40, "h": 40, "label": "ZW-2026-002"},
    ])
    assert r.status_code == 200, r.text
    assert [w["label"] for w in r.json()["windows"]] == ["ZW-2026-001", "ZW-2026-002"]

    restored = client.get("/api/layout").json()
    assert [w["label"] for w in restored["windows"]] == ["ZW-2026-001", "ZW-2026-002"]
    assert [w["position"] for w in restored["windows"]] == [0, 1]


def test_label_trimmed_before_persistence(client):
    r = _put(client, [{"x": 300, "y": 300, "w": 10, "h": 10, "label": "  A-1  "}])
    assert r.status_code == 200
    assert r.json()["windows"][0]["label"] == "A-1"
    assert client.get("/api/layout").json()["windows"][0]["label"] == "A-1"


def test_label_max_length_boundary(client):
    ok = _put(client, [{"x": 300, "y": 300, "w": 10, "h": 10, "label": "编" * 24}])
    assert ok.status_code == 200, ok.text
    assert ok.json()["windows"][0]["label"] == "编" * 24

    too_long = _put(client, [{"x": 300, "y": 300, "w": 10, "h": 10, "label": "编" * 25}])
    assert too_long.status_code == 422
    assert "label" in too_long.json()["field_errors"][0]["fields"]


def test_blank_and_missing_labels_stored_as_null(client):
    r = _put(client, [
        {"x": 300, "y": 300, "w": 10, "h": 10},                      # 旧客户端：未携带编号
        {"x": 500, "y": 300, "w": 10, "h": 10, "label": None},       # 显式 null
        {"x": 700, "y": 300, "w": 10, "h": 10, "label": "   "},      # 全空格 = 未填写
    ])
    assert r.status_code == 200, r.text
    assert [w["label"] for w in r.json()["windows"]] == [None, None, None]
    restored = client.get("/api/layout").json()
    assert [w["label"] for w in restored["windows"]] == [None, None, None]


def test_blank_labels_do_not_count_as_duplicates(client):
    r = _put(client, [
        {"x": 300, "y": 300, "w": 10, "h": 10, "label": ""},
        {"x": 500, "y": 300, "w": 10, "h": 10, "label": "  "},
    ])
    assert r.status_code == 200, r.text


def test_duplicate_labels_rejected_per_row_and_nothing_persisted(client):
    _put(client, [{"x": 12, "y": 12, "w": 10, "h": 10, "label": "KEEP"}])
    before = client.get("/api/layout").json()

    r = _put(client, [
        {"x": 300, "y": 300, "w": 10, "h": 10, "label": "DUP"},
        {"x": 500, "y": 300, "w": 10, "h": 10},                # 合法行
        {"x": 700, "y": 300, "w": 10, "h": 10, "label": "DUP"},
    ])
    assert r.status_code == 422
    rows = r.json()["field_errors"]
    # 只有涉及重复的两行报编号字段错误
    assert [row["index"] for row in rows] == [0, 2]
    assert all(set(row["fields"]) == {"label"} for row in rows)

    # 整批不落库：最新记录保持原样
    assert client.get("/api/layout").json() == before


def test_duplicate_after_trimming_is_rejected(client):
    r = _put(client, [
        {"x": 300, "y": 300, "w": 10, "h": 10, "label": "A-1"},
        {"x": 500, "y": 300, "w": 10, "h": 10, "label": "  A-1  "},
    ])
    assert r.status_code == 422
    assert [row["index"] for row in r.json()["field_errors"]] == [0, 1]


def test_label_too_long_rejected_and_nothing_persisted(client):
    before = client.get("/api/layout").json()
    r = _put(client, [{"x": 300, "y": 300, "w": 10, "h": 10, "label": "X" * 25}])
    assert r.status_code == 422
    errs = r.json()["field_errors"]
    assert errs[0]["index"] == 0 and set(errs[0]["fields"]) == {"label"}
    assert client.get("/api/layout").json() == before


def test_label_must_be_string(client):
    for bad in (5, 3.5, True, {"a": 1}, ["A"]):
        r = _put(client, [{"x": 300, "y": 300, "w": 10, "h": 10, "label": bad}])
        assert r.status_code == 422, bad
        assert "label" in r.json()["field_errors"][0]["fields"]


def test_label_error_coexists_with_coordinate_errors(client):
    r = _put(client, [{"x": 0, "y": 300, "w": 10, "h": 10, "label": "X" * 25}])
    assert r.status_code == 422
    fields = r.json()["field_errors"][0]["fields"]
    assert set(fields) == {"x", "label"}


def test_overlapping_labeled_windows_keep_labels_in_saved_verdict(client):
    r = _put(client, [
        {"x": 300, "y": 300, "w": 100, "h": 100, "label": "WIN-A"},
        {"x": 350, "y": 350, "w": 100, "h": 100, "label": "WIN-B"},
    ])
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"] == "不可裁切"
    ids_by_label = {w["label"]: w["id"] for w in body["windows"]}
    assert body["result"]["window_conflicts"] == [
        {"window_a": ids_by_label["WIN-A"], "window_b": ids_by_label["WIN-B"]}
    ]

    restored = client.get("/api/layout").json()
    assert [w["label"] for w in restored["windows"]] == ["WIN-A", "WIN-B"]
    assert restored["result"] == body["result"]


# ---------- 定位步长 grid_step（1/5/10 毫米） ----------

def test_grid_step_defaults_to_1_for_legacy_client(client):
    # 旧客户端不提交 grid_step：按 1 毫米处理，旧格式请求照常成功
    r = _put(client, [{"x": 13, "y": 14, "w": 7, "h": 9}])
    assert r.status_code == 200, r.text
    assert r.json()["grid_step"] == 1


def test_empty_layout_reports_default_grid_step(client):
    assert client.get("/api/layout").json()["grid_step"] == 1


@pytest.mark.parametrize("step", [1, 5, 10])
def test_grid_step_accepted_persisted_and_restored(client, step):
    # x/y 以 12 为基准对齐，w/h 为步长整数倍
    win = {"x": 12, "y": 12, "w": step, "h": step}
    r = _put(client, [win], grid_step=step)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["grid_step"] == step

    restored = client.get("/api/layout").json()
    assert restored["grid_step"] == step
    assert restored["windows"][0]["x"] == 12
    assert restored["verdict"] == body["verdict"]
    assert restored["result"] == body["result"]


def test_grid_step_5_alignment_examples(client):
    # 12 + 5k 刻度：12,17,22,...；尺寸 5 的倍数
    r = _put(client, [{"x": 17, "y": 22, "w": 10, "h": 15}], grid_step=5)
    assert r.status_code == 200, r.text

    # 右/下边界附近：982+5=987 ≤ 988，是 5 毫米下容得下整窗的最后刻度起点；
    # 下一个刻度 987 再加 5 就越过 988，前端吸附时会回退到 982。
    r = _put(client, [{"x": 982, "y": 682, "w": 5, "h": 5}], grid_step=5)
    assert r.status_code == 200, r.text


def test_off_grid_coordinates_return_field_errors_per_row(client):
    r = _put(
        client,
        [
            {"x": 300, "y": 301, "w": 11, "h": 13},  # 5 毫米下四字段全部偏离
            {"x": 12, "y": 12, "w": 5, "h": 5},      # 合法
        ],
        grid_step=5,
    )
    assert r.status_code == 422
    rows = r.json()["field_errors"]
    assert [row["index"] for row in rows] == [0]
    assert set(rows[0]["fields"]) == {"x", "y", "w", "h"}


def test_off_grid_position_vs_size_fields_reported_separately(client):
    # 位置对齐但尺寸不是 5 的倍数 → 只报 w/h
    r = _put(client, [{"x": 12, "y": 12, "w": 7, "h": 8}], grid_step=5)
    assert r.status_code == 422
    assert set(r.json()["field_errors"][0]["fields"]) == {"w", "h"}

    # 尺寸对齐但位置偏离 → 只报 x/y
    r = _put(client, [{"x": 13, "y": 14, "w": 5, "h": 5}], grid_step=5)
    assert r.status_code == 422
    assert set(r.json()["field_errors"][0]["fields"]) == {"x", "y"}


def test_off_grid_submission_creates_no_new_layout(client):
    _put(client, [{"x": 12, "y": 12, "w": 5, "h": 5, "label": "KEEP"}], grid_step=5)
    before = client.get("/api/layout").json()

    r = _put(
        client,
        [
            {"x": 12, "y": 12, "w": 5, "h": 5},      # 合法行
            {"x": 300, "y": 300, "w": 10, "h": 10},  # 偏离 5 毫米刻度
        ],
        grid_step=5,
    )
    assert r.status_code == 422
    # 整批不落库：最新记录（坐标、步长、结论）保持不变
    assert client.get("/api/layout").json() == before


@pytest.mark.parametrize("bad", [2, 0, -5, 5.0, "5", True, [5]])
def test_invalid_grid_step_rejected_as_top_level_error(client, bad):
    r = client.put("/api/layout", json={"windows": [], "grid_step": bad})
    assert r.status_code == 422, bad
    assert r.json()["field_errors"] == []
    assert "grid_step" in r.json()["detail"]
    assert client.get("/api/layout").json()["windows"] == []


def test_explicit_null_grid_step_defaults_to_1(client):
    r = client.put("/api/layout", json={"windows": [], "grid_step": None})
    assert r.status_code == 200, r.text
    assert r.json()["grid_step"] == 1


def test_grid_step_1_accepts_every_integer_coordinate(client):
    r = _put(client, [{"x": 987, "y": 600, "w": 1, "h": 88}], grid_step=1)
    assert r.status_code == 200, r.text


def test_switching_step_changes_what_counts_as_off_grid(client):
    # 同一组坐标在 1 毫米合法、在 5 毫米被按字段打回
    win = [{"x": 13, "y": 16, "w": 7, "h": 3}]
    assert _put(client, win, grid_step=1).status_code == 200
    r = _put(client, win, grid_step=5)
    assert r.status_code == 422
    assert set(r.json()["field_errors"][0]["fields"]) == {"x", "y", "w", "h"}
    r = _put(client, win, grid_step=10)
    assert r.status_code == 422


def test_grid_step_10_boundary(client):
    # 10 毫米刻度以 12 为基准，尺寸为 10 的倍数。
    # w=10 时最后容得下整窗的位置刻度为 972（972+10=982 ≤ 988，下一刻度 982 越界）。
    r = _put(client, [{"x": 972, "y": 672, "w": 10, "h": 10}], grid_step=10)
    assert r.status_code == 200, r.text
    # x=983 位置偏离、w=5/h=6 尺寸偏离 10 毫米刻度
    r = _put(client, [{"x": 983, "y": 682, "w": 5, "h": 6}], grid_step=10)
    assert r.status_code == 422
    assert set(r.json()["field_errors"][0]["fields"]) == {"x", "w", "h"}
