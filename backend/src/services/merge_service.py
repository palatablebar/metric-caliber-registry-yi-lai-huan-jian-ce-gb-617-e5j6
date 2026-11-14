"""同名口径合并服务：归档旧口径 → 迁移报表与引用 → 落合并记录（幂等）。

合并失败（自引用 / 新依赖环 / 参数不合法）时不做任何写入：`metrics.status`、
公式版本、报表引用、依赖边与审计记录全部保持原样，只返回冲突路径。

成功合并以 `merge_key = merge:<保留指标>:<排序后的被合并指标>` 为自然键：
重复提交同一合并直接返回已归档记录，不新增公式版本、不新增审计、不重复影响分。
"""
from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.constants.enums import DependencyKind, MetricStatus
from src.models.change_request import ChangeRequest
from src.models.dependency import Dependency
from src.models.formula import Formula
from src.models.merge import MetricMerge
from src.models.metric import Metric, MetricAlias
from src.models.report import Report
from src.services import dependency_service, dsl_parser, formula_service, graph_service, report_service
from src.services import merge_plan
from src.utils.errors import AppError, not_found
from src.utils.timeutil import iso_utc


def merge_key_of(survivor_code: str, source_codes: list[str]) -> str:
    return f"merge:{survivor_code}:{','.join(sorted(set(source_codes)))}"


def serialize(merge: MetricMerge, *, with_archive: bool = False) -> dict:
    payload = {
        "id": merge.id,
        "merge_key": merge.merge_key,
        "survivor_metric_id": merge.survivor_metric_id,
        "source_metric_id": merge.source_metric_id,
        "survivor_code": merge.survivor_code,
        "source_code": merge.source_code,
        "survivor_name": merge.survivor_name,
        "source_name": merge.source_name,
        "status": merge.status,
        "note": merge.note,
        "summary": merge.summary or {},
        "requested_by": merge.requested_by,
        "created_at": iso_utc(merge.created_at),
    }
    if with_archive:
        payload["archived"] = merge.archived or {}
    return payload


def list_merges(db: Session, *, limit: int = 50, offset: int = 0) -> dict:
    rows = db.scalars(
        select(MetricMerge).order_by(MetricMerge.id.desc()).limit(limit).offset(offset)
    ).all()
    total = db.scalar(select(func.count()).select_from(MetricMerge)) or 0
    return {
        "items": [serialize(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def get_merge(db: Session, merge_id: int) -> MetricMerge:
    merge = db.get(MetricMerge, merge_id)
    if merge is None:
        raise not_found("metric_merge", merge_id)
    return merge


# ---------------------------------------------------------------------------
# 计划
# ---------------------------------------------------------------------------


def _merge_redirects(db: Session) -> dict[str, str]:
    """已合并口径 -> 保留口径（多次合并时按记录顺序传递）。"""
    redirect: dict[str, str] = {}
    for merge in db.scalars(select(MetricMerge).order_by(MetricMerge.id)).all():
        for code in (merge.source_code or "").split(" / "):
            code = code.strip()
            if code:
                redirect[code] = merge.survivor_code
    return redirect


def _resolve(code: str, redirect: dict[str, str]) -> str:
    seen: set[str] = set()
    while code in redirect and code not in seen:
        seen.add(code)
        code = redirect[code]
    return code


def _reference_union(db: Session, metrics: list[Metric], latest: dict[int, Formula]) -> dict[str, list[str]]:
    """当前口径解析出的引用 ∪ 依赖边表：旧路径（上次校验后改过口径）也不会漏。

    指向已合并口径的旧引用按合并记录重定向到保留口径，避免折叠后的图漏边。
    """
    known = {metric.code for metric in metrics}
    redirect = _merge_redirects(db)
    references: dict[str, list[str]] = {metric.code: [] for metric in metrics}
    for metric in metrics:
        formula = latest.get(metric.id)
        deps: list[str] = []
        if formula is not None:
            deps.extend((formula.parsed or {}).get("deps") or [])
            if not deps and formula.dsl_text:
                parsed = dsl_parser.parse_dsl(formula.dsl_text, metric.code)
                deps.extend(parsed.get("deps") or [])
        references[metric.code].extend(ref for ref in deps if ref in known)
    for edge in db.scalars(select(Dependency).where(Dependency.kind == DependencyKind.METRIC.value)).all():
        code = next((item.code for item in metrics if item.id == edge.from_metric_id), None)
        if code is None:
            continue
        target = next((item.code for item in metrics if item.id == edge.to_metric_id), None) or edge.to_ref
        if target in known:
            references[code].append(target)
    resolved: dict[str, list[str]] = {}
    for code, deps in references.items():
        resolved[code] = sorted({_resolve(dep, redirect) for dep in deps if _resolve(dep, redirect) in known})
    return resolved


def _normalize(payload: dict) -> tuple[str, list[str]]:
    survivor_code = (payload.get("survivor_code") or "").strip()
    raw_sources = payload.get("source_codes") or []
    if not survivor_code:
        raise AppError("VALIDATION", 422, {"field": "survivor_code"})
    if not isinstance(raw_sources, list) or not raw_sources:
        raise AppError("VALIDATION", 422, {"field": "source_codes"})
    sources = sorted({str(code).strip() for code in raw_sources if str(code).strip()})
    if not sources:
        raise AppError("VALIDATION", 422, {"field": "source_codes"})
    if survivor_code in sources:
        raise AppError("VALIDATION", 422, {"field": "source_codes", "message": "保留口径不能同时作为被合并指标"})
    return survivor_code, sources


def _validate_payload(payload: dict, metrics_by_code: dict[str, Metric]) -> tuple[str, list[str]]:
    survivor_code, sources = _normalize(payload)
    for code in [survivor_code] + sources:
        metric = metrics_by_code.get(code)
        if metric is None:
            raise AppError("NOT_FOUND", 404, {"entity": "metric", "code": code})
        if metric.status == MetricStatus.MERGED.value:
            raise AppError("CONFLICT", 409, {"entity": "metric", "code": code, "status": metric.status})
    survivor = metrics_by_code[survivor_code]
    mismatched = [
        code for code in sources
        if metrics_by_code[code].name != survivor.name
    ]
    if mismatched:
        raise AppError(
            "VALIDATION",
            422,
            {
                "field": "source_codes",
                "message": "被合并指标必须与保留指标同名",
                "survivor": {"code": survivor_code, "name": survivor.name},
                "mismatched": [
                    {"code": code, "name": metrics_by_code[code].name} for code in mismatched
                ],
            },
        )
    return survivor_code, sources


def build_plan(db: Session, payload: dict) -> dict:
    survivor_code, sources = _normalize(payload)
    key = merge_key_of(survivor_code, sources)
    existing = db.scalar(select(MetricMerge).where(MetricMerge.merge_key == key))
    if existing is not None:
        return {
            "ok": False,
            "kind": "already_merged",
            "merge_key": key,
            "already_merged": True,
            "existing_merge_id": existing.id,
            "survivor_code": survivor_code,
            "source_codes": sources,
            "blockers": [],
            "summary": existing.summary or {},
            "message": "这组同名口径已经合并过，重复提交不会再次迁移或生成记录。",
        }
    metrics = db.scalars(select(Metric).order_by(Metric.code)).all()
    metrics_by_code = {metric.code: metric for metric in metrics}
    _validate_payload(payload, metrics_by_code)
    latest = formula_service.latest_map(db)
    payloads, _, _ = graph_service.collect_inputs(db)
    references = _reference_union(db, metrics, latest)
    reports = report_service.report_payloads(db)
    plan = merge_plan.plan_merge(
        payloads,
        references,
        reports,
        survivor_code=survivor_code,
        source_codes=sources,
    )
    plan["merge_key"] = key
    plan["already_merged"] = False
    return plan


# ---------------------------------------------------------------------------
# 归档
# ---------------------------------------------------------------------------


def _iso(value: datetime | None) -> str | None:
    return iso_utc(value)


def _formula_snapshot(formula: Formula) -> dict:
    return {
        "id": formula.id,
        "version": formula.version,
        "dsl_text": formula.dsl_text,
        "parse_status": formula.parse_status,
        "parse_error": formula.parse_error,
        "parsed": formula.parsed or {},
        "signature": formula.signature,
        "author_email": formula.author_email,
        "note": formula.note,
        "seed_key": formula.seed_key,
        "created_at": _iso(formula.created_at),
    }


def _metric_snapshot(metric: Metric) -> dict:
    return {
        "id": metric.id,
        "code": metric.code,
        "name": metric.name,
        "domain": metric.domain,
        "tier": metric.tier,
        "unit": metric.unit,
        "owner": metric.owner,
        "description": metric.description,
        "status": metric.status,
        "current_version": metric.current_version,
        "enabled": metric.enabled,
        "created_by": metric.created_by,
        "seed_key": metric.seed_key,
        "created_at": _iso(metric.created_at),
        "updated_at": _iso(metric.updated_at),
    }


def _dependency_snapshot(edge: Dependency, codes: dict[int, str]) -> dict:
    return {
        "id": edge.id,
        "from_metric_code": codes.get(edge.from_metric_id),
        "kind": edge.kind,
        "to_ref": edge.to_ref,
        "to_metric_code": codes.get(edge.to_metric_id) if edge.to_metric_id else None,
        "weight": edge.weight,
        "note": edge.note,
        "created_at": _iso(edge.created_at),
    }


def _archive_source(db: Session, metric: Metric, codes: dict[int, str]) -> dict:
    formulas = db.scalars(
        select(Formula).where(Formula.metric_id == metric.id).order_by(Formula.version)
    ).all()
    dependencies = db.scalars(
        select(Dependency).where(
            (Dependency.from_metric_id == metric.id)
            | (Dependency.to_metric_id == metric.id)
            | ((Dependency.kind == DependencyKind.METRIC.value) & (Dependency.to_ref == metric.code))
        )
    ).all()
    aliases = db.scalars(select(MetricAlias).where(MetricAlias.metric_id == metric.id)).all()
    reports = []
    for report in db.scalars(select(Report).order_by(Report.code)).all():
        if metric.code in (report.metric_codes or []):
            reports.append(
                {
                    "code": report.code,
                    "name": report.name,
                    "is_core": bool(report.is_core),
                    "weight": int(report.weight or 1),
                    "metric_codes": list(report.metric_codes or []),
                }
            )
    changes = db.scalars(
        select(ChangeRequest).where(
            (ChangeRequest.metric_id == metric.id) | (ChangeRequest.target_ref == metric.code)
        ).order_by(ChangeRequest.id)
    ).all()
    return {
        "metric": _metric_snapshot(metric),
        "formulas": [_formula_snapshot(row) for row in formulas],
        "formula_versions": [row.version for row in formulas],
        "dependencies": [_dependency_snapshot(edge, codes) for edge in dependencies],
        "aliases": [row.alias for row in aliases],
        "reports": reports,
        "change_requests": [
            {
                "id": change.id,
                "cr_no": change.cr_no,
                "target_ref": change.target_ref,
                "change_kind": change.change_kind,
                "status": change.status,
                "impact_score": change.impact_score,
                "created_at": _iso(change.created_at),
            }
            for change in changes
        ],
    }


# ---------------------------------------------------------------------------
# 迁移
# ---------------------------------------------------------------------------


def _rewrite_dsl(dsl_text: str, source_codes: list[str], survivor_code: str) -> str:
    text = dsl_text
    for code in source_codes:
        pattern = re.compile(rf"@{re.escape(code)}(?![A-Za-z0-9_])")
        text = pattern.sub(f"@{survivor_code}", text)
    return text


def _migrate_reports(db: Session, sources: set[str], survivor_code: str) -> list[dict]:
    migrated: list[dict] = []
    for report in db.scalars(select(Report).order_by(Report.code)).all():
        codes = list(report.metric_codes or [])
        if not sources.intersection(codes):
            continue
        mapped = [survivor_code if code in sources else code for code in codes]
        after: list[str] = []
        for code in mapped:
            if code not in after:
                after.append(code)
        report.metric_codes = after
        migrated.append(
            {
                "code": report.code,
                "name": report.name,
                "before": codes,
                "after": after,
                "deduped": sorted({code for code in mapped if mapped.count(code) > 1}),
            }
        )
    return migrated


def _migrate_formulas(
    db: Session,
    metrics: list[Metric],
    latest: dict[int, Formula],
    sources: set[str],
    survivor_code: str,
    author_email: str,
) -> list[dict]:
    migrated: list[dict] = []
    for metric in metrics:
        if metric.code in sources or metric.status == MetricStatus.MERGED.value:
            continue
        formula = latest.get(metric.id)
        if formula is None:
            continue
        deps = set((formula.parsed or {}).get("deps") or [])
        if not deps and formula.dsl_text:
            deps = set(dsl_parser.parse_dsl(formula.dsl_text, metric.code).get("deps") or [])
        if not sources.intersection(deps):
            continue
        rewritten = _rewrite_dsl(formula.dsl_text, sorted(sources), survivor_code)
        warnings: list[str] = []
        refs_now = len(re.findall(rf"@{re.escape(survivor_code)}(?![A-Za-z0-9_])", rewritten))
        if refs_now > 1:
            warnings.append(
                f"公式里有 {refs_now} 处引用被折叠到 @{survivor_code}，原先是两份不同口径，需人工确认加减关系"
            )
        entry = {
            "code": metric.code,
            "formula_id": formula.id,
            "version_before": formula.version,
            "dsl_before": formula.dsl_text,
            "dsl_after": rewritten,
            "new_version": None,
            "warnings": warnings,
        }
        if rewritten != formula.dsl_text:
            new_formula = formula_service.save_version(
                db,
                metric,
                dsl_text=rewritten,
                author_email=author_email,
                note=f"口径合并：{' / '.join(sorted(sources))} → {survivor_code}",
            )
            entry["new_version"] = new_formula.version
        migrated.append(entry)
    return migrated


def execute_merge(db: Session, payload: dict, *, actor_email: str, note: str | None = None) -> tuple[MetricMerge, bool]:
    """执行合并。返回 (合并记录, 是否本次新建)；已执行过的合并直接返回原记录。"""
    survivor_code, sources = _normalize(payload)
    key = merge_key_of(survivor_code, sources)
    existing = db.scalar(select(MetricMerge).where(MetricMerge.merge_key == key))
    if existing is not None:
        return existing, False

    metrics = db.scalars(select(Metric).order_by(Metric.code)).all()
    metrics_by_code = {metric.code: metric for metric in metrics}
    _validate_payload(payload, metrics_by_code)
    plan = build_plan(db, payload)
    if not plan["ok"]:
        raise AppError(
            "MERGE_BLOCKED",
            409,
            {
                "merge_key": key,
                "survivor_code": survivor_code,
                "source_codes": sources,
                "blockers": plan["blockers"],
                "conflict_paths": [" → ".join(item["path"]) for item in plan["blockers"]],
                "message": "合并会产生自引用或依赖环，本次操作未做任何写入。",
            },
            message="合并会产生自引用或依赖环，本次操作已保留原样",
        )

    codes = {metric.id: metric.code for metric in metrics}
    latest = formula_service.latest_map(db)
    survivor = metrics_by_code[survivor_code]
    archived = {
        "survivor": _metric_snapshot(survivor),
        "roots": {
            "reports": report_service.report_payloads(db),
            "dependency_edges": [
                _dependency_snapshot(edge, codes)
                for edge in db.scalars(select(Dependency).order_by(Dependency.id)).all()
            ],
        },
        "sources": [
            _archive_source(db, metrics_by_code[code], codes) for code in sources
        ],
    }

    reports_migrated = _migrate_reports(db, set(sources), survivor_code)
    references_migrated = _migrate_formulas(
        db, metrics, latest, set(sources), survivor_code, actor_email
    )
    archived["report_migrations"] = reports_migrated
    archived["reference_migrations"] = references_migrated

    for code in sources:
        source = metrics_by_code[code]
        source.status = MetricStatus.MERGED.value
        source.enabled = False
        source.core_report_count = 0

    # 依赖边按合并后的口径重建（源指标的边会自然清理，不会重复）
    active_metrics = [metric for metric in db.scalars(select(Metric).order_by(Metric.code)).all()
                      if metric.status != MetricStatus.MERGED.value]
    latest_after = formula_service.latest_map(db)
    parsed_by_code: dict[str, dict] = {}
    for metric in active_metrics:
        formula = latest_after.get(metric.id)
        parsed_by_code[metric.code] = dsl_parser.parse_dsl(
            formula.dsl_text if formula else "", metric.code
        )
    dependency_service.rebuild(db, active_metrics, parsed_by_code)

    impact = plan["impact"]
    summary = {
        "merge_key": key,
        "survivor_code": survivor_code,
        "source_codes": sources,
        "source_names": plan["source_names"],
        "references_migrated_count": len(references_migrated),
        "reports_migrated_count": len(reports_migrated),
        "reports_migrated": [item["code"] for item in reports_migrated],
        "versions_created": [
            {"code": item["code"], "version": item["new_version"]}
            for item in references_migrated
            if item["new_version"] is not None
        ],
        "reference_warnings": [
            {"code": item["code"], "warnings": item["warnings"]}
            for item in references_migrated
            if item.get("warnings")
        ],
        "impact": impact,
        "existing_cycles": plan["existing_cycles"],
    }
    merge = MetricMerge(
        merge_key=key,
        survivor_metric_id=survivor.id,
        source_metric_id=metrics_by_code[sources[0]].id if sources else None,
        survivor_code=survivor_code,
        source_code=" / ".join(sources),
        survivor_name=survivor.name,
        source_name=" / ".join(metrics_by_code[code].name for code in sources),
        status="completed",
        note=note,
        summary=summary,
        archived=archived,
        requested_by=actor_email,
    )
    db.add(merge)
    db.flush()
    return merge, True
