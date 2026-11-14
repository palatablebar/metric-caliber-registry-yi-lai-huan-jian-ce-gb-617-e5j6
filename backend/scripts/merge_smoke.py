"""在内存 SQLite 库上验证完整合并流程的确定性输出（不依赖 PostgreSQL）。

用法：
    PYTHONPATH=/tmp/appdeps:backend python3 -m scripts.merge_smoke
或：
    docker compose exec -T backend python -m scripts.merge_smoke

输出两段逐字一致的 `MERGE_SMOKE` 行：第一次执行 / 重试同一合并。
不读写项目数据库，只在本进程内建表并 seed 一份最小数据。
"""
from __future__ import annotations

import json
import sys
from contextlib import contextmanager

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from src.models import Base
from src.models.audit import AuditLog
from src.models.formula import Formula
from src.models.merge import MetricMerge
from src.models.metric import Metric
from src.models.report import Report
from src.services import audit_service, dependency_service, dsl_parser, formula_service, merge_service


def _metric(db: Session, code: str, name: str, dsl: str) -> Metric:
    metric = Metric(code=code, name=name, domain="trade", tier="normal")
    db.add(metric)
    db.flush()
    formula_service.save_version(db, metric, dsl_text=dsl, author_email="admin@metricreg.io")
    return metric


@contextmanager
def _session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    db = Session(engine)
    try:
        yield db
    finally:
        db.close()


def _seed(db: Session) -> None:
    _metric(db, "gmv", "GMV", "gmv = sum(dwd.order.amount) where order_status = 'paid' group by dt")
    _metric(
        db,
        "gmv_net",
        "GMV",
        "gmv_net = sum(dwd.order.amount) - sum(dwd.refund.amount) where order_status = 'paid' group by dt",
    )
    _metric(db, "net_revenue", "净营收", "net_revenue = sum(@gmv_net) group by dt")
    db.add_all(
        [
            Report(code="r01", name="经营日报", is_core=True, weight=3, metric_codes=["gmv", "gmv_net", "net_revenue"]),
            Report(code="r02", name="月会GMV看板", is_core=True, weight=3, metric_codes=["gmv_net"]),
        ]
    )
    db.flush()
    metrics = db.scalars(select(Metric).order_by(Metric.code)).all()
    latest = formula_service.latest_map(db)
    parsed = {
        metric.code: dsl_parser.parse_dsl(latest[metric.id].dsl_text if metric.id in latest else "", metric.code)
        for metric in metrics
    }
    dependency_service.rebuild(db, metrics, parsed)


def _counts(db: Session) -> dict:
    return {
        "formulas": db.scalar(select(func.count()).select_from(Formula)),
        "merges": db.scalar(select(func.count()).select_from(MetricMerge)),
        "audit": db.scalar(select(func.count()).select_from(AuditLog)),
    }


def main() -> int:
    with _session() as db:
        _seed(db)
        payload = {"survivor_code": "gmv", "source_codes": ["gmv_net"]}
        first, created = merge_service.execute_merge(db, payload, actor_email="analyst@metricreg.io")
        if created:
            audit_service.record(db, "analyst@metricreg.io", "analyst", "metric.merge", "metric_merge", first.id)
        db.flush()
        print(
            "MERGE_SMOKE first "
            + json.dumps(
                {
                    "created": created,
                    "counts": _counts(db),
                    "summary": first.summary,
                    "reports": sorted(
                        (report.code, list(report.metric_codes)) for report in db.scalars(select(Report)).all()
                    ),
                    "archived_versions": [
                        item["formula_versions"] for item in first.archived["sources"]
                    ],
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            flush=True,
        )
        second, created_again = merge_service.execute_merge(db, payload, actor_email="analyst@metricreg.io")
        print(
            "MERGE_SMOKE retry "
            + json.dumps(
                {
                    "created": created_again,
                    "same_id": second.id == first.id,
                    "counts": _counts(db),
                    "summary": second.summary,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            flush=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
