from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.constants.roles import MERGE_ROLES
from src.controllers import merge_controller
from src.middlewares.rbac import READ_ROLES, require_roles
from src.schemas.merge import MergeIn, MergePreviewIn
from src.utils.pagination import page_params

router = APIRouter(prefix="/merges", tags=["merges"])


@router.get("")
def list_merges(
    limit: int = Query(50),
    offset: int = Query(0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    page = page_params(limit, offset)
    return merge_controller.list_merges(db, limit=page.limit, offset=page.offset)


@router.get("/{merge_id}")
def get_merge(
    merge_id: int, db: Session = Depends(get_db), principal: dict = Depends(require_roles(*READ_ROLES))
) -> dict:
    return merge_controller.get_merge(db, merge_id)


@router.post("/preview")
def preview_merge(
    payload: MergePreviewIn,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*MERGE_ROLES)),
) -> dict:
    return merge_controller.preview(db, payload.model_dump())


@router.post("", status_code=201)
def execute_merge(
    payload: MergeIn,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*MERGE_ROLES)),
) -> dict:
    result = merge_controller.execute(db, payload.model_dump(), principal, request)
    if result.get("duplicate"):
        # 幂等重放：审计中间件看到这个响应头就不再写 http.mutation，避免重复审计
        response.headers["X-Idempotent-Replay"] = "1"
    return result
