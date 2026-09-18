from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import engine as eng
from .db import Base, engine as db_engine, get_db
from .migrate import run_migrations
from .models import Layout, Window
from .validation import normalize_label, validate_payload


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 容器启动时一次性建表；幂等
    Base.metadata.create_all(bind=db_engine)
    # 旧库补充后加的可空列（如 windows.label）；幂等
    run_migrations(db_engine)
    yield


app = FastAPI(title="档案装裱排版校验台", version="1.0.0", lifespan=lifespan)

# 开发/联调时前端来自不同源（vite dev server），放开同源限制
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/defects")
def defects() -> dict:
    """内置只读瑕疵区，供前端绘制。引擎中才是裁决事实来源。"""
    return {
        "defects": [
            {"index": i, "x": x, "y": y, "w": w, "h": h}
            for i, (x, y, w, h) in enumerate(eng.DEFECTS)
        ],
        "sheet": {"width": eng.SHEET_WIDTH, "height": eng.SHEET_HEIGHT, "margin": eng.MARGIN},
    }


def _serialize(layout: Layout) -> dict:
    windows = [
        {
            "id": w.id,
            "x": w.x,
            "y": w.y,
            "w": w.w,
            "h": w.h,
            "position": w.position,
            "label": w.label,
        }
        for w in layout.windows
    ]
    result = dict(layout.result)
    return {
        "verdict": layout.verdict,
        # 旧记录理论上已由迁移回填为 1；这里再次兜底，
        # 保证任何读取路径（含外部直写的旧库）都拿到合法步长。
        "grid_step": layout.grid_step or eng.DEFAULT_GRID_STEP,
        "windows": windows,
        "result": result,
        "created_at": layout.created_at.isoformat() if layout.created_at else None,
    }


@app.get("/api/layout")
def get_latest_layout(db: Session = Depends(get_db)) -> JSONResponse:
    """恢复最近一次合法提交的布局；没有提交过时返回空布局。

    旧记录没有 grid_step 列时由迁移补为 1；为稳妥起见这里再次兜底，
    保证读取旧数据时前端总能拿到合法步长。
    """
    layout = db.scalar(select(Layout).order_by(Layout.id.desc()).limit(1))
    if layout is None:
        return JSONResponse(
            {"verdict": None, "grid_step": eng.DEFAULT_GRID_STEP,
             "windows": [], "result": None, "created_at": None}
        )
    return JSONResponse(_serialize(layout))


@app.put("/api/layout")
async def submit_layout(request: Request, db: Session = Depends(get_db)):
    """整批提交：任一字段非法 → 逐字段错误，整次不落库；
    合法 → 保存全部开窗与完整裁决，并返回与 GET 相同的结构。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=422,
            content={"detail": "请求体不是合法 JSON", "field_errors": []},
        )

    try:
        row_errors, grid_step = validate_payload(body)
    except ValueError as exc:
        return JSONResponse(
            status_code=422,
            content={"detail": str(exc), "field_errors": []},
        )

    if row_errors:
        return JSONResponse(
            status_code=422,
            content={"detail": "存在非法开窗，本次提交未保存", "field_errors": row_errors},
        )

    items = body["windows"]
    # 以提交次序为临时 id 进行裁决（落库后由数据库生成稳定 id）
    staged = [
        {"id": i, "x": it["x"], "y": it["y"], "w": it["w"], "h": it["h"]}
        for i, it in enumerate(items)
    ]
    verdict_data = eng.adjudicate(staged)

    # 临时 id（=数组下标）映射到真实 id：先算好开窗行，裁决结果中替换 id
    # 为保证“刷新恢复同一布局”，这里直接用数据库 id 重新生成裁决结果。
    # 所选定位步长随布局一起保存，刷新后恢复同一刻度。
    layout = Layout(
        verdict=verdict_data["verdict"], result={}, grid_step=grid_step, windows=[]
    )
    for position, it in enumerate(items):
        layout.windows.append(
            Window(
                position=position,
                x=it["x"],
                y=it["y"],
                w=it["w"],
                h=it["h"],
                label=normalize_label(it.get("label")),
            )
        )
    db.add(layout)
    db.flush()  # 拿到 layout.id 与各 window.id

    persisted = [
        {"id": w.id, "x": w.x, "y": w.y, "w": w.w, "h": w.h} for w in layout.windows
    ]
    layout.result = eng.adjudicate(persisted)
    layout.verdict = layout.result["verdict"]

    db.commit()
    db.refresh(layout)
    return _serialize(layout)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
