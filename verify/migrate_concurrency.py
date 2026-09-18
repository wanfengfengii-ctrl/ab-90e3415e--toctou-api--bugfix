#!/usr/bin/env python3
"""并发启动迁移回归（Docker Compose 一次性验收）。

复现旧库并发升级的 TOCTOU：在当前数据库中创建并最终删除一个隔离
schema（migratetest_*），其中是缺少 windows.label、layouts.grid_step
的旧版表；两个独立引擎/连接的线程同时调用真实的
``app.migrate.run_migrations``，并在首条 ALTER 下发前对齐。修复前
两个实例都通过“列不存在”的检查，随后一方 ALTER 成功、另一方收到
DuplicateColumn 退出；修复后两方都成功，且每个新增列只出现一次。
另有 5 实例同一起跑线的宽并发检查。

只触碰临时隔离 schema，不接触业务表；全部断言通过时退出码为 0。

运行（与 verify 服务同样的 Compose 验收方式）：

    docker compose run --rm verify-migration
"""
import os
import sys
import threading
import time
import traceback
import uuid

sys.path.insert(0, "/app")  # backend 镜像把应用复制在 /app

from sqlalchemy import create_engine, event, inspect, text  # noqa: E402

from app.migrate import run_migrations  # noqa: E402

DATABASE_URL = os.environ["DATABASE_URL"]

OLD_LAYOUTS_DDL = """
CREATE TABLE {schema}.layouts (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    verdict VARCHAR(16) NOT NULL,
    result JSON NOT NULL
)
"""
OLD_WINDOWS_DDL = """
CREATE TABLE {schema}.windows (
    id SERIAL PRIMARY KEY,
    layout_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    x INTEGER NOT NULL,
    y INTEGER NOT NULL,
    w INTEGER NOT NULL,
    h INTEGER NOT NULL
)
"""


def assert_true(cond, msg):
    if not cond:
        raise AssertionError(msg)


def make_old_schema(admin, schema):
    with admin.begin() as conn:
        conn.execute(text(f"CREATE SCHEMA {schema}"))
        conn.execute(text(OLD_LAYOUTS_DDL.format(schema=schema)))
        conn.execute(text(OLD_WINDOWS_DDL.format(schema=schema)))
        conn.execute(
            text(
                f"INSERT INTO {schema}.layouts (verdict, result)"
                " VALUES ('可裁切', '{\"defect_conflicts\": []}')"
            )
        )
        conn.execute(
            text(
                f"INSERT INTO {schema}.windows (layout_id, position, x, y, w, h)"
                " VALUES (1, 0, 300, 300, 100, 80)"
            )
        )


def worker_engine(schema):
    return create_engine(
        DATABASE_URL,
        connect_args={"options": f"-c search_path={schema}"},
    )


def run_two_synced_before_first_alter(schema):
    """严格复现路径：两实例在首条 ALTER 下发前对齐。"""
    gate = threading.Event()
    parked = {"n": 0}
    state_lock = threading.Lock()
    outcomes = []

    def worker(name):
        engine = worker_engine(schema)
        blocked = {"done": False}

        @event.listens_for(engine, "before_cursor_execute")
        def _park(conn, cursor, statement, params, context, many):
            normalized = " ".join(statement.split()).upper()
            if not blocked["done"] and normalized.startswith("ALTER TABLE WINDOWS"):
                blocked["done"] = True
                with state_lock:
                    parked["n"] += 1
                gate.wait(timeout=30)

        try:
            run_migrations(engine)
            outcomes.append((name, "ok", None))
        except Exception as exc:  # noqa: BLE001
            outcomes.append((name, "error", f"{type(exc).__name__}: {exc}"))
        finally:
            engine.dispose()

    threads = [threading.Thread(target=worker, args=(n,)) for n in ("A", "B")]
    for t in threads:
        t.start()

    # 修复前：两方都停在首条 ALTER 前；修复后：只有一方到达（另一方
    # 在咨询锁上等待并在锁内重新检查列）。等第一个到达后留 1 秒宽限，
    # 再统一放行。
    deadline = time.monotonic() + 5
    while parked["n"] == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(1.0)
    gate.set()
    for t in threads:
        t.join()
    return outcomes


def run_burst(schema, n_threads):
    """宽并发：n 个实例同一起跑线升级同一旧库。"""
    start = threading.Barrier(n_threads)
    outcomes = []

    def worker(idx):
        engine = worker_engine(schema)
        start.wait()
        try:
            run_migrations(engine)
            outcomes.append((idx, "ok", None))
        except Exception as exc:  # noqa: BLE001
            outcomes.append((idx, "error", f"{type(exc).__name__}: {exc}"))
        finally:
            engine.dispose()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return outcomes


def check_schema_final(admin, schema):
    """迁移后结构与历史数据断言。"""
    insp = inspect(admin)
    win_cols = [c["name"] for c in insp.get_columns("windows", schema=schema)]
    lay_cols = [c["name"] for c in insp.get_columns("layouts", schema=schema)]
    assert_true(win_cols.count("label") == 1, f"windows.label 应恰好出现一次: {win_cols}")
    assert_true(
        lay_cols.count("grid_step") == 1,
        f"layouts.grid_step 应恰好出现一次: {lay_cols}",
    )
    with admin.connect() as conn:
        label, step = conn.execute(
            text(
                f"SELECT (SELECT label FROM {schema}.windows),"
                f" (SELECT grid_step FROM {schema}.layouts)"
            )
        ).one()
        rows = conn.execute(text(f"SELECT COUNT(*) FROM {schema}.windows")).scalar()
    assert_true(label is None, f"历史开窗 label 应为 NULL，实际 {label!r}")
    assert_true(step == 1, f"历史布局 grid_step 应回填 1，实际 {step}")
    assert_true(rows == 1, f"历史开窗行数应保留为 1，实际 {rows}")


def main():
    admin = create_engine(DATABASE_URL)
    schema_two = f"migratetest_{uuid.uuid4().hex[:20]}"
    schema_burst = f"migratetest_{uuid.uuid4().hex[:20]}"
    print(f"[setup] 在隔离 schema {schema_two} 中创建旧版 windows/layouts")
    make_old_schema(admin, schema_two)
    print(f"[setup] 在隔离 schema {schema_burst} 中创建旧版 windows/layouts")
    make_old_schema(admin, schema_burst)
    try:
        print("[check 1/2] 两实例在首条 ALTER 前同步，并发升级同一旧库")
        outcomes = run_two_synced_before_first_alter(schema_two)
        for name, status, msg in outcomes:
            print(f"  实例 {name}: {status}" + (f" -> {msg}" if msg else ""))
        errors = [f"{n}: {m}" for n, s, m in outcomes if s == "error"]
        assert_true(not errors, f"存在竞争失败的实例（修复前为 DuplicateColumn）：{errors}")
        check_schema_final(admin, schema_two)

        print("[check 2/2] 另一个旧库上 5 个实例同一起跑线并发升级")
        outcomes = run_burst(schema_burst, 5)
        for idx, status, msg in outcomes:
            print(f"  实例 #{idx}: {status}" + (f" -> {msg}" if msg else ""))
        errors = [f"#{i}: {m}" for i, s, m in outcomes if s == "error"]
        assert_true(not errors, f"并发升级存在失败实例：{errors}")
        check_schema_final(admin, schema_burst)
    finally:
        with admin.begin() as conn:
            conn.execute(text(f"DROP SCHEMA IF EXISTS {schema_two} CASCADE"))
            conn.execute(text(f"DROP SCHEMA IF EXISTS {schema_burst} CASCADE"))
        print(f"[teardown] 已删除隔离 schema {schema_two} / {schema_burst}")
        admin.dispose()

    print("PASS: 并发启动迁移幂等，多实例同时升级全部成功，新增列各一次")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(1)
