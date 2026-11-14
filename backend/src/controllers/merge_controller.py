"""口径合并控制器：预览迁移计划 / 执行合并 / 合并归档查询。"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from src.services import audit_service, merge_service


def preview(db: Session, payload: dict) -> dict:
    return merge_service.build_plan(db, payload)


def execute(db: Session, payload: dict, principal: dict, request: Request) -> dict:
    merge, created = merge_service.execute_merge(
        db, payload, actor_email=principal["email"], note=payload.get("note")
    )
    if created:
        audit_service.record(
            db,
            actor_email=principal["email"],
            actor_role=principal["role"],
            action="metric.merge",
            entity="metric_merge",
            entity_id=merge.id,
            detail={
                "merge_key": merge.merge_key,
                "survivor": merge.survivor_code,
                "sources": merge.summary.get("source_codes", []),
                "reports_migrated": merge.summary.get("reports_migrated_count", 0),
                "references_migrated": merge.summary.get("references_migrated_count", 0),
                "impact_score": (merge.summary.get("impact") or {}).get("score", 0),
            },
            ip=request.client.host if request.client else None,
        )
        db.commit()
    result = merge_service.serialize(merge, with_archive=True)
    result["created"] = created
    result["duplicate"] = not created
    return result


def list_merges(db: Session, *, limit: int = 50, offset: int = 0) -> dict:
    return merge_service.list_merges(db, limit=limit, offset=offset)


def get_merge(db: Session, merge_id: int) -> dict:
    merge = merge_service.get_merge(db, merge_id)
    return merge_service.serialize(merge, with_archive=True)
