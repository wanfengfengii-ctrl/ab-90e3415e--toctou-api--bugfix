"""并发启动迁移的回归测试（PostgreSQL 专用）。

复现 TOCTOU 竞争：旧库缺少 ``windows.label`` 与 ``layouts.grid_step``，
两个 API 实例（测试里用同进程内各自独立引擎/连接的两个线程模拟）
同时调用真实的 ``run_migrations``，并在首条 ALTER 真正下发前对齐。
修复前两方都通过“列不存在”的检查，随后一个 ALTER 成功、另一个收到
``DuplicateColumn``；真实 lifespan 不捕获该异常，竞争失败的实例退出。
修复后两方都应成功，且每个新增列只出现一次、历史行保留。

默认套件用 SQLite 内存库，无法模拟跨连接并发 DDL，本模块仅在设置了
``MIGRATION_TEST_DATABASE_URL`` 时运行，例如（compose 栈的 db 暴露在
宿主时）::

    MIGRATION_TEST_DATABASE_URL=\\
    'postgresql+psycopg2://matboard:matboard@localhost:5432/matboard' \\
        pytest tests/test_migrate_concurrency.py

测试只在目标库中创建并最终删除独立 schema（``migratetest_*``），
不触碰任何既有表。
"""
import os
import sys
import threading
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from sqlalchemy import create_engine, event, text  # noqa: E402

from app.migrate import run_migrations  # noqa: E402

TEST_DB_URL = os.environ.get("MIGRATION_TEST_DATABASE_URL")

try:
    import psycopg2  # noqa: F401

    _HAS_PSYCOPG2 = True
except ImportError:  # 纯 SQLite 环境（如 requirements-dev 最小安装）
    _HAS_PSYCOPG2 = False

pytestmark = pytest.mark.skipif(
    not TEST_DB_URL or not _HAS_PSYCOPG2,
    reason="需要 PostgreSQL：设置 MIGRATION_TEST_DATABASE_URL 并安装 psycopg2",
)

# 旧版 layouts（缺 grid_step）
_OLD_LAYOUTS_DDL = """
CREATE TABLE {schema}.layouts (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    verdict VARCHAR(16) NOT NULL,
    result JSON NOT NULL
)
"""
# 旧版 windows（缺 label）
_OLD_WINDOWS_DDL = """
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


@pytest.fixture()
def old_schema():
    """在目标库中准备一个隔离的旧结构 schema，测试结束后删除。"""
    admin = create_engine(TEST_DB_URL)
    schema = f"migratetest_{uuid.uuid4().hex[:20]}"
    with admin.begin() as conn:
        conn.execute(text(f"CREATE SCHEMA {schema}"))
        conn.execute(text(_OLD_LAYOUTS_DDL.format(schema=schema)))
        conn.execute(text(_OLD_WINDOWS_DDL.format(schema=schema)))
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
    try:
        yield schema
    finally:
        with admin.begin() as conn:
            conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        admin.dispose()


def _worker_engine(schema):
    """每个“API 实例”使用完全独立的引擎与连接池。"""
    return create_engine(
        TEST_DB_URL,
        connect_args={"options": f"-c search_path={schema}"},
    )


def _column_rows(admin, schema):
    with admin.connect() as conn:
        return conn.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns"
                " WHERE table_schema = :schema"
                " AND ((table_name = 'windows' AND column_name = 'label')"
                "   OR (table_name = 'layouts' AND column_name = 'grid_step'))"
            ),
            {"schema": schema},
        ).all()


def _run_concurrent(schema, n_threads, *, gate_at_first_alter):
    """让 n 个独立实例同时对同一旧库执行真实迁移。

    gate_at_first_alter=True 时严格按复现路径同步：每个实例在自己的
    首条 ALTER 下发前停在同一道闸门前，主线程确认两方都已通过列检查、
    停在 ALTER 前，再统一放行。修复前两方随后同时 ALTER，败者抛
    DuplicateColumn；修复后第二个实例在咨询锁上等待，根本不会下发
    ALTER，闸门由超时放行（仅此情形付出等待代价）。
    """
    start = threading.Barrier(n_threads)
    gate = threading.Event()
    parked = {"n": 0}
    state_lock = threading.Lock()
    outcomes = []

    def worker():
        engine = _worker_engine(schema)
        blocked = {"done": False}

        if gate_at_first_alter:

            @event.listens_for(engine, "before_cursor_execute")
            def _park_before_alter(conn, cursor, statement, params, context, many):
                normalized = " ".join(statement.split()).upper()
                if not blocked["done"] and normalized.startswith("ALTER TABLE WINDOWS"):
                    blocked["done"] = True
                    with state_lock:
                        parked["n"] += 1
                    gate.wait(timeout=30)

        start.wait()
        try:
            run_migrations(engine)
            outcomes.append(("ok", None))
        except Exception as exc:  # noqa: BLE001
            outcomes.append(("error", f"{type(exc).__name__}: {exc}"))
        finally:
            engine.dispose()

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()

    if gate_at_first_alter:
        # 修复前：两方毫秒级都停到 ALTER 前；修复后：只会有一方到达
        # （另一方在咨询锁上等待）。等第一个到达后留 1 秒宽限确认，
        # 再统一放行，避免修复版本承担固定 30 秒闸门超时。
        deadline = time.monotonic() + 5
        while parked["n"] == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(1.0)
        gate.set()

    for t in threads:
        t.join()
    return outcomes


def test_concurrent_migration_sync_before_first_alter(old_schema):
    """复现路径：两实例在首条 ALTER 前同步，旧库上同时升级。"""
    outcomes = _run_concurrent(old_schema, 2, gate_at_first_alter=True)

    errors = [msg for status, msg in outcomes if status == "error"]
    assert errors == [], f"存在竞争失败的实例（修复前为 DuplicateColumn）：{errors}"
    assert len(outcomes) == 2

    admin = create_engine(TEST_DB_URL)
    try:
        # 每个新增列只出现一次
        rows = _column_rows(admin, old_schema)
        assert sorted(rows) == [
            ("layouts", "grid_step"),
            ("windows", "label"),
        ]
        # 历史行保留：旧开窗 label 为空，旧布局步长回填 1
        with admin.connect() as conn:
            label, step = conn.execute(
                text(
                    f"SELECT (SELECT label FROM {old_schema}.windows),"
                    f" (SELECT grid_step FROM {old_schema}.layouts)"
                )
            ).one()
        assert label is None
        assert step == 1
        # 提交后咨询锁自动释放：再跑一次为空操作，不报错
        run_migrations(_worker_engine(old_schema))
    finally:
        admin.dispose()


def test_five_instances_migrate_same_old_db_concurrently(old_schema):
    """更宽的并发面：5 个实例同一起跑线升级同一旧库，全部成功。"""
    outcomes = _run_concurrent(old_schema, 5, gate_at_first_alter=False)

    errors = [msg for status, msg in outcomes if status == "error"]
    assert errors == [], f"并发升级存在失败实例：{errors}"
    assert len(outcomes) == 5

    admin = create_engine(TEST_DB_URL)
    try:
        rows = _column_rows(admin, old_schema)
        assert sorted(rows) == [
            ("layouts", "grid_step"),
            ("windows", "label"),
        ]
        with admin.connect() as conn:
            assert conn.execute(
                text(f"SELECT COUNT(*) FROM {old_schema}.windows")
            ).scalar() == 1
    finally:
        admin.dispose()
