"""轻量迁移：为既有（旧结构）数据库补充可空 label 列。"""
import os
import sys

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, inspect, text  # noqa: E402

from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.migrate import run_migrations  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

# 旧结构的 windows 表（没有 label 列）
OLD_SCHEMA = """
CREATE TABLE windows (
    id INTEGER PRIMARY KEY,
    layout_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    x INTEGER NOT NULL,
    y INTEGER NOT NULL,
    w INTEGER NOT NULL,
    h INTEGER NOT NULL
)
"""

# 旧结构的 layouts 表（没有 grid_step 列）
OLD_LAYOUTS_SCHEMA = """
CREATE TABLE layouts (
    id INTEGER PRIMARY KEY,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    verdict VARCHAR(16) NOT NULL,
    result JSON NOT NULL
)
"""


def _make_old_db(path):
    """造一个旧结构的库，并写入一条历史记录。"""
    engine = create_engine(f"sqlite+pysqlite:///{path}")
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS windows"))
        conn.execute(text(OLD_SCHEMA))
        conn.execute(
            text(
                "INSERT INTO windows (id, layout_id, position, x, y, w, h)"
                " VALUES (1, 1, 0, 300, 300, 100, 80)"
            )
        )
    return engine


def _make_old_layouts_db(path):
    """造一个连 layouts.grid_step 也没有的旧库，写入一条历史布局。"""
    engine = create_engine(f"sqlite+pysqlite:///{path}")
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS windows"))
        conn.execute(text("DROP TABLE IF EXISTS layouts"))
        conn.execute(text(OLD_SCHEMA))
        conn.execute(text(OLD_LAYOUTS_SCHEMA))
        conn.execute(
            text(
                "INSERT INTO layouts (id, verdict, result) VALUES"
                " (1, '可裁切', '{\"defect_conflicts\": []}')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO windows (id, layout_id, position, x, y, w, h)"
                " VALUES (1, 1, 0, 300, 300, 100, 80)"
            )
        )
    return engine


def test_migration_adds_nullable_label_column(tmp_path):
    engine = _make_old_db(tmp_path / "old.db")

    # 模拟服务启动：create_all 不会动已存在的旧表，迁移负责补列
    Base.metadata.create_all(bind=engine)
    run_migrations(engine)

    columns = {c["name"]: c for c in inspect(engine).get_columns("windows")}
    assert "label" in columns
    assert columns["label"]["nullable"] is True

    # 旧记录读取时编号为空值
    with engine.connect() as conn:
        row = conn.execute(text("SELECT id, label FROM windows WHERE id = 1")).one()
    assert row == (1, None)


def test_migration_is_idempotent_and_preserves_rows(tmp_path):
    engine = _make_old_db(tmp_path / "old.db")
    Base.metadata.create_all(bind=engine)
    run_migrations(engine)
    run_migrations(engine)  # 第二次启动不应报错

    with engine.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM windows")).scalar()
    assert count == 1


def test_migration_noop_on_fresh_schema(tmp_path):
    # 新库由 create_all 直接建出完整结构（含 label），迁移为空操作
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path}/fresh.db")
    Base.metadata.create_all(bind=engine)
    run_migrations(engine)
    assert "label" in {c["name"] for c in inspect(engine).get_columns("windows")}


def test_migration_adds_grid_step_column_defaulting_to_1(tmp_path):
    engine = _make_old_layouts_db(tmp_path / "old-layouts.db")

    Base.metadata.create_all(bind=engine)  # 不动已存在的旧 layouts 表
    run_migrations(engine)

    columns = {c["name"]: c for c in inspect(engine).get_columns("layouts")}
    assert "grid_step" in columns
    assert columns["grid_step"]["nullable"] is False

    # 旧记录缺少该值：迁移补列并回填 1（DEFAULT 1）
    with engine.connect() as conn:
        row = conn.execute(text("SELECT id, grid_step FROM layouts WHERE id = 1")).one()
    assert row == (1, 1)


def test_migration_grid_step_idempotent_and_preserves_rows(tmp_path):
    engine = _make_old_layouts_db(tmp_path / "old-layouts.db")
    Base.metadata.create_all(bind=engine)
    run_migrations(engine)
    run_migrations(engine)  # 第二次启动不应报错

    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM layouts")).scalar() == 1
        assert conn.execute(text("SELECT grid_step FROM layouts")).scalar() == 1


def test_old_layout_loads_through_api_with_default_step(tmp_path):
    """旧记录经启动迁移后，通过真实 GET 路由加载，步长兜底为 1。

    这是“旧记录缺少该值时按 1 毫米处理 + 历史数据正常加载”的兼容证据。
    """
    engine = _make_old_layouts_db(tmp_path / "old-api.db")
    TestingSessionLocal = sessionmaker(bind=engine, future=True)
    # 模拟容器启动：create_all 不补旧表列，迁移补列（grid_step 回填 1）
    Base.metadata.create_all(bind=engine)
    run_migrations(engine)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as client:
            data = client.get("/api/layout").json()
    finally:
        app.dependency_overrides.clear()

    assert data["grid_step"] == 1
    assert data["verdict"] == "可裁切"
    assert [(w["x"], w["y"], w["w"], w["h"]) for w in data["windows"]] == [
        (300, 300, 100, 80)
    ]
    assert data["windows"][0]["label"] is None
