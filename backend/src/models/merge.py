"""同名口径合并归档：一次合并里「被合并指标」的口径历史 / 版本 / 依赖来源完整留档。

一条记录对应一个 (保留指标, 被合并指标) 自然键 `merge_key`，重试同一合并时直接复用，
不会再次生成版本、依赖边或审计记录。指标行本身不删除：`metrics.status = merged`，
历史公式、依赖边与报表引用关系都保存在 `archived` JSON 里。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class MetricMerge(Base):
    __tablename__ = "metric_merges"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    merge_key: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    survivor_metric_id: Mapped[int | None] = mapped_column(
        ForeignKey("metrics.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_metric_id: Mapped[int | None] = mapped_column(
        ForeignKey("metrics.id", ondelete="SET NULL"), nullable=True, index=True
    )
    survivor_code: Mapped[str] = mapped_column(String(48), index=True)
    source_code: Mapped[str] = mapped_column(String(48), index=True)
    survivor_name: Mapped[str] = mapped_column(String(80), default="")
    source_name: Mapped[str] = mapped_column(String(80), default="")
    status: Mapped[str] = mapped_column(String(16), default="completed", index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    archived: Mapped[dict] = mapped_column(JSON, default=dict)
    requested_by: Mapped[str] = mapped_column(String(160), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
