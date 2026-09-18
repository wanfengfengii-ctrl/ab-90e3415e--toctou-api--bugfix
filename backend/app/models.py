from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from .db import Base


class Layout(Base):
    """一次合法提交对应的完整方案与裁决结果。

    每次合法提交新增一行，GET 永远返回最新一行；
    瑕疵区由裁决引擎内置、只读，不入库。
    """

    __tablename__ = "layouts"

    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # 唯一裁决结论："可裁切" | "不可裁切"
    verdict = Column(String(16), nullable=False)
    # 完整裁决：defect_conflicts / window_conflicts / conflicting_window_ids
    result = Column(JSON, nullable=False)
    # 本次提交使用的定位步长（1/5/10 毫米）；旧记录没有该列，
    # 由迁移补列并置 1（DEFAULT_GRID_STEP），与旧客户端行为一致。
    grid_step = Column(Integer, nullable=False, default=1, server_default="1")

    windows = relationship(
        "Window",
        back_populates="layout",
        cascade="all, delete-orphan",
        order_by="Window.position",
    )


class Window(Base):
    __tablename__ = "windows"

    id = Column(Integer, primary_key=True)
    layout_id = Column(
        Integer, ForeignKey("layouts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 该开窗在本次提交中的次序，用于刷新后还原同一布局
    position = Column(Integer, nullable=False)
    x = Column(Integer, nullable=False)
    y = Column(Integer, nullable=False)
    w = Column(Integer, nullable=False)
    h = Column(Integer, nullable=False)
    # 可选工件编号（藏品登记号）：去首尾空格后 ≤24 字符，同一布局内唯一；
    # 可空——旧记录与旧客户端的请求一律按空值处理
    label = Column(String(24), nullable=True)

    layout = relationship("Layout", back_populates="windows")
