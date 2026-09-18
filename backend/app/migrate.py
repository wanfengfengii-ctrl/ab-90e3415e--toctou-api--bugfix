"""轻量 schema 迁移：create_all 之外的幂等补丁。

项目用 ``Base.metadata.create_all`` 建表，它对已存在的旧表不会补列。
这里在启动时对旧库做最小 ALTER，把后加的列补齐；新库由
create_all 直接建出完整结构，本函数察觉列已存在即为空操作。
"""
from __future__ import annotations

from sqlalchemy import inspect, text


def run_migrations(db_engine) -> None:
    """为既有库补充后加的列（均幂等）：

    - windows.label（可空工件编号）
    - layouts.grid_step（定位步长，旧记录按 1 毫米处理）
    """
    inspector = inspect(db_engine)
    tables = set(inspector.get_table_names())

    if "windows" in tables:
        columns = {col["name"] for col in inspector.get_columns("windows")}
        if "label" not in columns:
            with db_engine.begin() as conn:
                conn.execute(text("ALTER TABLE windows ADD COLUMN label VARCHAR(24)"))

    if "layouts" in tables:
        columns = {col["name"] for col in inspector.get_columns("layouts")}
        if "grid_step" not in columns:
            with db_engine.begin() as conn:
                # 旧记录缺少步长值，一律按 1 毫米处理（DEFAULT 1 同时补齐历史行）
                conn.execute(
                    text("ALTER TABLE layouts ADD COLUMN grid_step INTEGER NOT NULL DEFAULT 1")
                )
