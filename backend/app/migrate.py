"""轻量 schema 迁移：create_all 之外的并发安全幂等补丁。

项目用 ``Base.metadata.create_all`` 建表，它对已存在的旧表不会补列。
这里在启动时对旧库做最小 ALTER，把后加的列补齐；新库由
create_all 直接建出完整结构，本函数察觉列已存在即为空操作。

多个 API 实例可能同时启动并升级同一个旧库。早期实现先
``inspect`` 检查“列不存在”再单独开事务 ALTER，是典型的
check-then-act（TOCTOU）：两个实例都会通过检查，随后 ALTER
互相竞争，败者收到 ``DuplicateColumn``，而 lifespan 不捕获该
异常，对应实例直接退出。这里用事务级咨询锁把
“检查 -> ALTER -> 提交”整体串行化，并在锁内重新检查列，
先拿到锁的实例提交后，后来者看到新列即为空操作。
"""
from __future__ import annotations

from sqlalchemy import inspect, text

# 迁移咨询锁键（pg_advisory_xact_lock 的 bigint 参数，取值为 ASCII "MATBOARD"）。
# 咨询锁按数据库隔离，随事务提交/回滚自动释放，进程崩溃也不会残留。
_MIGRATION_LOCK_KEY = 0x4D4154424F415244


def run_migrations(db_engine) -> None:
    """为既有库补充后加的列（并发安全、幂等）：

    - windows.label（可空工件编号）
    - layouts.grid_step（定位步长，旧记录按 1 毫米处理）
    """
    # 检查与 ALTER 必须在同一个事务、同一把锁内完成，
    # 否则“列不存在”的结论在 ALTER 下发前可能已被别的实例改写。
    with db_engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            # 事务级咨询锁：把并发启动的多个实例在此串行化，
            # 事务提交（或回滚）时自动释放，不会跨进程残留。
            conn.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": _MIGRATION_LOCK_KEY}
            )

        # 锁内重新检视：后拿到锁的实例必须看到先到者刚提交的列。
        inspector = inspect(conn)
        tables = set(inspector.get_table_names())

        if "windows" in tables:
            columns = {col["name"] for col in inspector.get_columns("windows")}
            if "label" not in columns:
                conn.execute(text("ALTER TABLE windows ADD COLUMN label VARCHAR(24)"))

        if "layouts" in tables:
            columns = {col["name"] for col in inspector.get_columns("layouts")}
            if "grid_step" not in columns:
                # 旧记录缺少步长值，一律按 1 毫米处理（DEFAULT 1 同时补齐历史行）
                conn.execute(
                    text("ALTER TABLE layouts ADD COLUMN grid_step INTEGER NOT NULL DEFAULT 1")
                )
