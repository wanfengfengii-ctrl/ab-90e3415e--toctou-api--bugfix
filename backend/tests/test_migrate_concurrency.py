"""并发启动迁移的回归测试。

复现缺陷：旧库（windows 缺 label、layouts 缺 grid_step）上两个 API 实例
同时启动，都先通过"列不存在"检查，随后并发发出不带 IF NOT EXISTS/迁移锁
的 ALTER TABLE —— check-then-act 的 TOCTOU 竞争，失败方收到 PostgreSQL
DuplicateColumn（42701），而 lifespan 不捕获该异常，实例退出。

修复后的契约：多个实例并发升级同一旧库时全部启动成功，每个新增列只出现
一次，历史行的 grid_step 回填为 1。

PostgreSQL 用例需要真实数据库，通过环境变量提供，否则跳过：

    TEST_DATABASE_URL=postgresql+psycopg2://matboard:matboard@db:5432/matboard \
        pytest tests/test_migrate_concurrency.py

用例在库内创建随机命名的隔离 schema 并在用例结束时 DROP CASCADE，
不触碰其他 schema 与宿主环境。

对 compose 栈跑这一组（测试目录未打进镜像，用挂载注入）：

    docker compose run --rm \
        -v "$PWD/backend:/app" -w /app \
        -e TEST_DATABASE_URL=postgresql+psycopg2://matboard:matboard@db:5432/matboard \
        api sh -c "pip install -q -r requirements-dev.txt && \
                   pytest tests/test_migrate_concurrency.py"
"""
import os
import sys
import threading
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from sqlalchemy import create_engine, inspect, text  # noqa: E402
from sqlalchemy.engine.reflection import Inspector  # noqa: E402

from app.migrate import run_migrations  # noqa: E402

# 升级前的旧版 windows 表（没有 label 列）
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

# 升级前的旧版 layouts 表（没有 grid_step 列）
OLD_LAYOUTS_DDL = """
CREATE TABLE {schema}.layouts (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    verdict VARCHAR(16) NOT NULL,
    result JSONB NOT NULL
)
"""


def _postgres_url():
    url = os.getenv("TEST_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not url or not url.startswith("postgresql"):
        return None
    try:
        probe = create_engine(url)
        with probe.connect() as conn:
            conn.execute(text("SELECT 1"))
        probe.dispose()
    except Exception:
        return None
    return url


pg_url = _postgres_url()
requires_postgres = pytest.mark.skipif(
    pg_url is None,
    reason="未提供可连通的 TEST_DATABASE_URL（postgresql），跳过并发迁移回归",
)


@pytest.fixture()
def old_schema():
    """在隔离 schema 中造旧版 windows/layouts 表并各写一条历史数据。"""
    schema = f"test_migrate_race_{uuid.uuid4().hex}"
    admin = create_engine(pg_url, future=True)
    with admin.begin() as conn:
        conn.execute(text(f"CREATE SCHEMA {schema}"))
        conn.execute(text(OLD_WINDOWS_DDL.format(schema=schema)))
        conn.execute(text(OLD_LAYOUTS_DDL.format(schema=schema)))
        conn.execute(
            text(
                f"INSERT INTO {schema}.layouts (verdict, result) VALUES"
                " ('可裁切', '{\"defect_conflicts\": []}'::jsonb)"
            )
        )
        conn.execute(
            text(
                f"INSERT INTO {schema}.windows "
                "(layout_id, position, x, y, w, h) VALUES (1, 0, 300, 300, 100, 80)"
            )
        )
    try:
        yield schema
    finally:
        with admin.begin() as conn:
            conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        admin.dispose()


def _column_count(admin, schema, table, column):
    with admin.connect() as conn:
        return conn.execute(
            text(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_schema = :s AND table_name = :t AND column_name = :c"
            ),
            {"s": schema, "t": table, "c": column},
        ).scalar()


def _run_concurrent_migrations(n, schema):
    """让 n 个"API 实例"（各自独立 engine/连接池）同时跑真实 run_migrations。

    通过反射钩子设同步点：每个线程的第一次 get_columns('windows')
    （即锁外那次"列不存在"检查）到齐后一起放行——旧代码就此同时发出
    ALTER，新代码则在咨询锁处排队、锁内复查后由一方完成 DDL。
    """
    engines = [
        create_engine(
            pg_url,
            connect_args={"options": f"-c search_path={schema},public"},
            future=True,
        )
        for _ in range(n)
    ]
    barrier = threading.Barrier(n, timeout=30)
    arrived = set()
    arrive_lock = threading.Lock()
    original_get_columns = Inspector.get_columns

    def synced_get_columns(self, table_name, schema=None, **kw):
        result = original_get_columns(self, table_name, schema=schema, **kw)
        if table_name == "windows":
            tid = threading.current_thread().ident
            with arrive_lock:
                first_visit = tid not in arrived
                arrived.add(tid)
            if first_visit:
                barrier.wait()  # 所有实例完成"列不存在"检查后同时继续
        return result

    errors = []

    def worker(i):
        try:
            run_migrations(engines[i])
        except Exception as exc:  # 对应 lifespan 未捕获即实例退出的失败
            errors.append((i, repr(exc)))

    Inspector.get_columns = synced_get_columns
    try:
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        Inspector.get_columns = original_get_columns
        for e in engines:
            e.dispose()
    return errors


@requires_postgres
@pytest.mark.parametrize("instances", [2, 3])
def test_concurrent_migrations_all_start_and_column_added_once(old_schema, instances):
    """多个实例并发升级同一旧库：全部成功；新增列各只出现一次。"""
    errors = _run_concurrent_migrations(instances, old_schema)

    assert errors == [], f"竞争失败的实例（旧版会抛 DuplicateColumn）: {errors}"

    admin = create_engine(pg_url, future=True)
    try:
        assert _column_count(admin, old_schema, "windows", "label") == 1
        assert _column_count(admin, old_schema, "layouts", "grid_step") == 1
        with admin.connect() as conn:
            # 旧记录随 ADD COLUMN ... DEFAULT 1 回填，且不重复加列
            assert conn.execute(
                text(f"SELECT grid_step FROM {old_schema}.layouts")
            ).scalar() == 1
            assert conn.execute(
                text(f"SELECT label FROM {old_schema}.windows")
            ).scalar() is None
            assert conn.execute(
                text(f"SELECT COUNT(*) FROM {old_schema}.windows")
            ).scalar() == 1
    finally:
        admin.dispose()


@requires_postgres
def test_third_instance_starting_after_upgrade_is_noop(old_schema):
    """并发升级完成后，后来的实例再启动：迁移为空操作且不报错。"""
    assert _run_concurrent_migrations(2, old_schema) == []

    latecomer = create_engine(
        pg_url,
        connect_args={"options": f"-c search_path={old_schema},public"},
        future=True,
    )
    try:
        run_migrations(latecomer)  # 不应抛错，也不应重复加列
        run_migrations(latecomer)
        admin = create_engine(pg_url, future=True)
        try:
            assert _column_count(admin, old_schema, "windows", "label") == 1
            assert _column_count(admin, old_schema, "layouts", "grid_step") == 1
        finally:
            admin.dispose()
    finally:
        latecomer.dispose()


def test_concurrent_sqlite_migrations_tolerate_lost_race(tmp_path):
    """无咨询锁方言（SQLite）：抢跑方加列后，失败方按幂等吞掉重复列错误。

    同样在两个连接各自完成"列不存在"检查后同步放行，强制 ALTER 撞车。
    """
    db_path = tmp_path / "old-race.db"
    bootstrap = create_engine(f"sqlite+pysqlite:///{db_path}", future=True)
    with bootstrap.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE windows ("
                "id INTEGER PRIMARY KEY, layout_id INTEGER NOT NULL, "
                "position INTEGER NOT NULL, x INTEGER NOT NULL, "
                "y INTEGER NOT NULL, w INTEGER NOT NULL, h INTEGER NOT NULL)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE layouts ("
                "id INTEGER PRIMARY KEY, created_at DATETIME NOT NULL, "
                "verdict VARCHAR(16) NOT NULL, result JSON NOT NULL)"
            )
        )
    bootstrap.dispose()

    engines = [
        create_engine(
            f"sqlite+pysqlite:///{db_path}",
            connect_args={"check_same_thread": False, "timeout": 15},
            future=True,
        )
        for _ in range(2)
    ]
    barrier = threading.Barrier(2, timeout=30)
    arrived = set()
    arrive_lock = threading.Lock()
    original_get_columns = Inspector.get_columns

    def synced_get_columns(self, table_name, schema=None, **kw):
        result = original_get_columns(self, table_name, schema=schema, **kw)
        if table_name == "windows":
            tid = threading.current_thread().ident
            with arrive_lock:
                first_visit = tid not in arrived
                arrived.add(tid)
            if first_visit:
                barrier.wait()
        return result

    errors = []

    def worker(i):
        try:
            run_migrations(engines[i])
        except Exception as exc:
            errors.append(repr(exc))

    Inspector.get_columns = synced_get_columns
    try:
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        Inspector.get_columns = original_get_columns
        for e in engines:
            e.dispose()

    assert errors == [], errors
    check = create_engine(f"sqlite+pysqlite:///{db_path}", future=True)
    try:
        columns = {c["name"] for c in inspect(check).get_columns("windows")}
        assert columns >= {"id", "label"}
        assert list(columns).count("label") == 1
        assert "grid_step" in {
            c["name"] for c in inspect(check).get_columns("layouts")
        }
    finally:
        check.dispose()
