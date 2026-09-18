"""轻量 schema 迁移：create_all 之外的并发幂等补丁。

项目用 ``Base.metadata.create_all`` 建表，它对已存在的旧表不会补列。
这里在启动时对旧库做最小 ALTER，把后加的列补齐；新库由
create_all 直接建出完整结构，本函数察觉列已存在即为空操作。

多个 API 实例可能针对同一个旧库同时启动（多副本、容器编排、滚动
升级）。朴素的"先查 information_schema、缺了再 ALTER"是
check-then-act，存在 TOCTOU 竞争：两个实例都查到列缺失后并发
ALTER，其中一个必然收到 DuplicateColumn（SQLSTATE 42701）；
lifespan 不捕获该异常，竞争失败的实例直接退出。

为让多个实例并发升级同一旧库时都能成功启动，PostgreSQL 路径在
**同一个事务**内串行化：

  1. ``pg_advisory_xact_lock`` 取事务级咨询锁，实例间互斥；
  2. 在锁内以最新快照重新检查列是否存在，锁内查得缺失才动手
     （锁外的检查只用于跳过已是最新的库、避免无谓取锁）；
  3. DDL 再带 ``ADD COLUMN IF NOT EXISTS`` 双保险。

其他方言（如测试用的 SQLite）逐列开短事务，并把并发抢跑导致的
duplicate column 错误按幂等吞掉。
"""
from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError

# (表名, 新增列, 列定义)；旧记录的兼容含义见各列在 models 中的注释
_MISSING_COLUMNS = (
    ("windows", "label", "VARCHAR(24)"),
    ("layouts", "grid_step", "INTEGER NOT NULL DEFAULT 1"),
)

# 迁移互斥咨询锁的应用级固定键（'MATB' 起头的 64 位常量）。
# 咨询锁仅在当前数据库内互斥，与任何业务表无关。
_SCHEMA_LOCK_KEY = 0x4D41544200000001


def _pending_columns(bind) -> list[tuple[str, str, str]]:
    """返回此刻确实缺失的待补列；快照只在调用瞬间成立。

    PostgreSQL 路径必须在持有迁移咨询锁时再调用一次，才能把
    "查到缺失"与"执行 ALTER"收进同一个临界区。
    """
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    pending: list[tuple[str, str, str]] = []
    for table, column, coltype in _MISSING_COLUMNS:
        if table not in tables:
            continue
        existing = {c["name"] for c in inspector.get_columns(table)}
        if column not in existing:
            pending.append((table, column, coltype))
    return pending


def _add_column_ddl(table: str, column: str, coltype: str, if_not_exists: bool) -> str:
    clause = "ADD COLUMN IF NOT EXISTS" if if_not_exists else "ADD COLUMN"
    return f"ALTER TABLE {table} {clause} {column} {coltype}"


def _is_duplicate_column(exc: DBAPIError) -> bool:
    """识别"列已存在"：PostgreSQL 的 42701 与 SQLite 的 duplicate column。"""
    orig = getattr(exc, "orig", None)
    if getattr(orig, "pgcode", None) == "42701":  # DuplicateColumn
        return True
    return "duplicate column" in str(orig).lower()


def run_migrations(db_engine) -> None:
    """为既有库补充后加的列（并发幂等）：

    - windows.label（可空工件编号）
    - layouts.grid_step（定位步长，旧记录按 1 毫米处理）
    """
    if db_engine.dialect.name == "postgresql":
        _run_migrations_postgresql(db_engine)
    else:
        _run_migrations_generic(db_engine)


def _run_migrations_postgresql(db_engine) -> None:
    # 锁外快检：已是最新结构时完全不开事务、不取锁
    if not _pending_columns(db_engine):
        return

    with db_engine.begin() as conn:
        # 事务级咨询锁：并发实例在此排队，事务提交后自动释放
        conn.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": _SCHEMA_LOCK_KEY},
        )
        # READ COMMITTED 下锁内语句拿最新快照：抢先实例已提交的列可见，
        # 因此本实例会自动跳过，不再发出注定失败的 ALTER。
        for table, column, coltype in _pending_columns(conn):
            conn.execute(
                text(_add_column_ddl(table, column, coltype, if_not_exists=True))
            )


def _run_migrations_generic(db_engine) -> None:
    """SQLite 等不支持咨询锁/ADD COLUMN IF NOT EXISTS 的方言。

    逐列开短事务；若另一个连接已抢先加列，重复列错误按幂等处理。
    """
    for table, column, coltype in _pending_columns(db_engine):
        try:
            with db_engine.begin() as conn:
                conn.execute(
                    text(_add_column_ddl(table, column, coltype, if_not_exists=False))
                )
        except DBAPIError as exc:
            if _is_duplicate_column(exc):
                continue
            raise
