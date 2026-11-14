"""口径合并集成测试：SQLite 内存库上跑完整的归档 / 迁移 / 幂等 / 阻止流程。"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from src.models import Base
from src.models.audit import AuditLog
from src.models.dependency import Dependency
from src.models.formula import Formula
from src.models.merge import MetricMerge
from src.models.metric import Metric
from src.models.report import Report
from src.services import audit_service, formula_service, merge_service, dependency_service, dsl_parser
from src.utils.errors import AppError


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()


def _metric(db, code, name, dsl):
    metric = Metric(code=code, name=name, domain="trade", tier="normal")
    db.add(metric)
    db.flush()
    formula_service.save_version(db, metric, dsl_text=dsl, author_email="seed@metricreg.io")
    return metric


def _seed(db):
    _metric(db, "gmv", "GMV", "gmv = sum(dwd.order.amount) where order_status = 'paid' group by dt")
    _metric(
        db,
        "gmv_net",
        "GMV",
        "gmv_net = sum(dwd.order.amount) - sum(dwd.refund.amount) where order_status = 'paid' group by dt",
    )
    _metric(db, "net_revenue", "净营收", "net_revenue = sum(@gmv_net) group by dt")
    _metric(db, "channel_gmv", "渠道GMV", "channel_gmv = sum(@gmv) - sum(@gmv_net) group by channel")
    db.add_all(
        [
            Report(code="r01", name="经营日报", is_core=True, weight=3, metric_codes=["gmv", "gmv_net", "net_revenue"]),
            Report(code="r02", name="月会GMV看板", is_core=True, weight=3, metric_codes=["gmv_net"]),
            Report(code="r03", name="渠道日报", is_core=False, weight=1, metric_codes=["channel_gmv"]),
        ]
    )
    db.flush()


def _rebuild(db):
    metrics = db.scalars(select(Metric).order_by(Metric.code)).all()
    latest = formula_service.latest_map(db)
    parsed = {
        metric.code: dsl_parser.parse_dsl(latest[metric.id].dsl_text if metric.id in latest else "", metric.code)
        for metric in metrics
    }
    dependency_service.rebuild(db, metrics, parsed)


def test_merge_migrates_reports_references_and_archives(db):
    _seed(db)
    _rebuild(db)
    source_formulas = db.scalar(
        select(func.count()).select_from(Formula).where(Formula.metric_id == 3)
    )
    merge, created = merge_service.execute_merge(
        db, {"survivor_code": "gmv", "source_codes": ["gmv_net"]}, actor_email="analyst@metricreg.io"
    )
    assert created is True

    # 被合并口径保留历史行，只标状态
    gmv_net = db.scalar(select(Metric).where(Metric.code == "gmv_net"))
    assert gmv_net.status == "merged"
    assert gmv_net.enabled is False
    assert db.scalar(
        select(func.count()).select_from(Formula).where(Formula.metric_id == gmv_net.id)
    ) == source_formulas

    # 归档完整：每条版本、依赖边、报表与变更引用
    archived = merge.archived
    source = archived["sources"][0]
    assert source["metric"]["code"] == "gmv_net"
    assert [item["version"] for item in source["formulas"]] == [1]
    assert source["formula_versions"] == [1]
    assert any(edge["kind"] == "report" for edge in source["dependencies"])
    assert {report["code"] for report in source["reports"]} == {"r01", "r02"}
    assert archived["roots"]["reports"]
    assert archived["roots"]["dependency_edges"]

    # 报表引用迁移 + 去重
    reports = {report.code: report for report in db.scalars(select(Report)).all()}
    assert reports["r01"].metric_codes == ["gmv", "net_revenue"]
    assert reports["r02"].metric_codes == ["gmv"]
    assert merge.summary["reports_migrated_count"] == 2

    # 其他指标的口径迁移成新版本，DSL 改指保留口径
    formula = formula_service.latest_for(db, db.scalar(select(Metric).where(Metric.code == "net_revenue")))
    assert formula.version == 2
    assert "@gmv" in formula.dsl_text
    assert "@gmv_net" not in formula.dsl_text
    before = formula_service.latest_for(db, db.scalar(select(Metric).where(Metric.code == "channel_gmv")))
    assert before.version == 2
    assert before.dsl_text.count("@gmv") == 2  # 两次旧引用改写后去重为一条依赖边
    assert merge.summary["references_migrated_count"] == 2

    # 依赖边按合并后的图重建：没有指向已合并指标的边
    edges = db.scalars(select(Dependency).where(Dependency.kind == "metric")).all()
    assert all(edge.to_ref != "gmv_net" for edge in edges)
    assert len([edge for edge in edges if edge.from_metric_id == before.metric_id]) == 1

    # 影响面不重复计分（r01 同时引用两个口径只算一次）
    impact = merge.summary["impact"]
    assert impact["score_before"] == 3
    assert impact["score"] == 6
    assert impact["report_count"] == 2
    assert impact["duplicate_report_codes"] == ["r01"]


def test_retry_does_not_duplicate_versions_audit_or_records(db):
    _seed(db)
    _rebuild(db)
    first, created = merge_service.execute_merge(
        db, {"survivor_code": "gmv", "source_codes": ["gmv_net"]}, actor_email="analyst@metricreg.io"
    )
    audit_service.record(db, "analyst@metricreg.io", "analyst", "metric.merge", "metric_merge", first.id)
    db.flush()
    formula_count = db.scalar(select(func.count()).select_from(Formula))
    audit_count = db.scalar(select(func.count()).select_from(AuditLog))
    report_snapshot = sorted(
        (report.code, tuple(report.metric_codes)) for report in db.scalars(select(Report)).all()
    )

    second, created_again = merge_service.execute_merge(
        db, {"survivor_code": "gmv", "source_codes": ["gmv_net"]}, actor_email="analyst@metricreg.io"
    )
    assert created_again is False
    assert second.id == first.id
    assert db.scalar(select(func.count()).select_from(MetricMerge)) == 1
    assert db.scalar(select(func.count()).select_from(Formula)) == formula_count
    assert db.scalar(select(func.count()).select_from(AuditLog)) == audit_count
    assert sorted(
        (report.code, tuple(report.metric_codes)) for report in db.scalars(select(Report)).all()
    ) == report_snapshot
    assert second.summary["impact"]["score"] == first.summary["impact"]["score"]


def test_blocked_merge_leaves_everything_untouched(db):
    _seed(db)
    _rebuild(db)
    # 让保留口径引用被合并口径：合并后 gmv 会引用自身
    metric = db.scalar(select(Metric).where(Metric.code == "gmv"))
    formula_service.save_version(
        db, metric, dsl_text="gmv = sum(@gmv_net) group by dt", author_email="admin@metricreg.io"
    )
    db.flush()
    snapshot = {
        "formulas": db.scalar(select(func.count()).select_from(Formula)),
        "reports": sorted((r.code, tuple(r.metric_codes)) for r in db.scalars(select(Report)).all()),
        "statuses": sorted((m.code, m.status) for m in db.scalars(select(Metric)).all()),
        "merges": db.scalar(select(func.count()).select_from(MetricMerge)),
        "audits": db.scalar(select(func.count()).select_from(AuditLog)),
    }
    with pytest.raises(AppError) as excinfo:
        merge_service.execute_merge(
            db, {"survivor_code": "gmv", "source_codes": ["gmv_net"]}, actor_email="analyst@metricreg.io"
        )
    assert excinfo.value.code == "MERGE_BLOCKED"
    assert excinfo.value.detail["conflict_paths"] == ["gmv → gmv"]
    assert snapshot == {
        "formulas": db.scalar(select(func.count()).select_from(Formula)),
        "reports": sorted((r.code, tuple(r.metric_codes)) for r in db.scalars(select(Report)).all()),
        "statuses": sorted((m.code, m.status) for m in db.scalars(select(Metric)).all()),
        "merges": db.scalar(select(func.count()).select_from(MetricMerge)),
        "audits": db.scalar(select(func.count()).select_from(AuditLog)),
    }


def test_old_dependency_edge_caught_even_when_dsl_changed(db):
    _seed(db)
    _rebuild(db)
    # 改口径但尚未跑校验：依赖边表仍保留 a -> gmv_net 的旧路径
    _metric(db, "cycle_a", "循环指标A", "cycle_a = sum(@gmv) group by dt")
    _metric(db, "cycle_b", "循环指标B", "cycle_b = sum(@cycle_a) group by dt")
    db.flush()
    _rebuild(db)
    cycle_a = db.scalar(select(Metric).where(Metric.code == "cycle_a"))
    cycle_b = db.scalar(select(Metric).where(Metric.code == "cycle_b"))
    # 直接造旧依赖边：cycle_b -> cycle_a -> gmv_net，而当前 DSL 已经改指 gmv
    gmv_net = db.scalar(select(Metric).where(Metric.code == "gmv_net"))
    db.add(
        Dependency(
            from_metric_id=cycle_a.id,
            kind="metric",
            to_ref="gmv_net",
            to_metric_id=gmv_net.id,
        )
    )
    db.flush()
    plan = merge_service.build_plan(db, {"survivor_code": "gmv", "source_codes": ["gmv_net"]})
    assert plan["ok"] is True
    assert plan["reference_graph_after"]["cycle_a"] == ["gmv"]


def test_folded_duplicate_reference_records_warning(db):
    _seed(db)
    _rebuild(db)
    metric = db.scalar(select(Metric).where(Metric.code == "channel_gmv"))
    formula_service.save_version(
        db,
        metric,
        dsl_text="channel_gmv = sum(@gmv) - sum(@gmv_net) group by channel",
        author_email="analyst@metricreg.io",
    )
    db.flush()
    merge, _ = merge_service.execute_merge(
        db, {"survivor_code": "gmv", "source_codes": ["gmv_net"]}, actor_email="analyst@metricreg.io"
    )
    warnings = merge.summary["reference_warnings"]
    assert warnings and warnings[0]["code"] == "channel_gmv"
    assert "2 处引用" in warnings[0]["warnings"][0]
    latest = formula_service.latest_for(db, metric)
    assert latest.dsl_text.count("@gmv") == 2
