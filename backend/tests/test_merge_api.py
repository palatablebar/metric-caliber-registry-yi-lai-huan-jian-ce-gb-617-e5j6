"""合并 API 集成测试：走真实 FastAPI 路由（TestClient + SQLite），覆盖 403 / 422 / 409 / 幂等。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from src.config.database import get_db
from src.config.app import create_app
from src.models import Base
from src.models.audit import AuditLog
from src.models.formula import Formula
from src.models.merge import MetricMerge
from src.models.metric import Metric
from src.models.report import Report
from src.models.user import User
from src.services import dependency_service, dsl_parser, formula_service
from src.utils.security import create_access_token, hash_password


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr("src.middlewares.rate_limit.settings.redis_url", "redis://127.0.0.1:6399/0")
    monkeypatch.setattr("src.services.overview_service.worker_alive", lambda: False)
    engine = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app = create_app()
    app.dependency_overrides[get_db] = override_db
    db = TestingSession()
    for email, role in (
        ("admin@metricreg.io", "admin"),
        ("analyst@metricreg.io", "analyst"),
        ("viewer@metricreg.io", "viewer"),
    ):
        db.add(User(email=email, password_hash=hash_password("Demo@2026"), display_name=email, role=role))
    for code, name, dsl in (
        ("gmv", "GMV", "gmv = sum(dwd.order.amount) where order_status = 'paid' group by dt"),
        (
            "gmv_net",
            "GMV",
            "gmv_net = sum(dwd.order.amount) - sum(dwd.refund.amount) where order_status = 'paid' group by dt",
        ),
        ("net_revenue", "净营收", "net_revenue = sum(@gmv_net) group by dt"),
    ):
        metric = Metric(code=code, name=name, domain="trade", tier="normal")
        db.add(metric)
        db.flush()
        formula_service.save_version(db, metric, dsl_text=dsl, author_email="admin@metricreg.io")
    db.add_all(
        [
            Report(code="r01", name="经营日报", is_core=True, weight=3, metric_codes=["gmv", "gmv_net", "net_revenue"]),
            Report(code="r02", name="月会GMV看板", is_core=True, weight=3, metric_codes=["gmv_net"]),
        ]
    )
    db.flush()
    metrics = db.scalars(select(Metric)).all()
    latest = formula_service.latest_map(db)
    dependency_service.rebuild(
        db,
        metrics,
        {
            metric.code: dsl_parser.parse_dsl(
                latest[metric.id].dsl_text if metric.id in latest else "", metric.code
            )
            for metric in metrics
        },
    )
    db.commit()
    db.close()
    # 不用 with：跳过 lifespan（建表 / seed 钩子），测试库由 fixture 自己建
    test_client = TestClient(app)
    try:
        yield test_client
    finally:
        test_client.close()


def _auth(email, role):
    token, _ = create_access_token(email, role)
    return {"Authorization": f"Bearer {token}"}


PAYLOAD = {"survivor_code": "gmv", "source_codes": ["gmv_net"]}


def test_viewer_cannot_merge_and_preview_is_forbidden(client):
    assert client.post("/api/merges/preview", json=PAYLOAD, headers=_auth("viewer@metricreg.io", "viewer")).status_code == 403
    assert client.post("/api/merges", json=PAYLOAD, headers=_auth("viewer@metricreg.io", "viewer")).status_code == 403


def test_preview_then_execute_then_retry_is_idempotent(client):
    preview = client.post("/api/merges/preview", json=PAYLOAD, headers=_auth("analyst@metricreg.io", "analyst"))
    assert preview.status_code == 200
    body = preview.json()
    assert body["ok"] is True
    assert body["merge_key"] == "merge:gmv:gmv_net"
    assert body["reports_migrated_count"] == 2
    assert body["impact"]["score"] == 6

    first = client.post("/api/merges", json=PAYLOAD, headers=_auth("analyst@metricreg.io", "analyst"))
    assert first.status_code == 201
    assert first.json()["created"] is True
    assert first.json()["summary"]["reports_migrated"] == ["r01", "r02"]

    retry = client.post("/api/merges", json=PAYLOAD, headers=_auth("analyst@metricreg.io", "analyst"))
    assert retry.status_code == 201
    assert retry.headers["X-Idempotent-Replay"] == "1"
    assert retry.json()["duplicate"] is True
    assert retry.json()["created"] is False
    assert retry.json()["id"] == first.json()["id"]
    assert retry.json()["summary"]["impact"]["score"] == first.json()["summary"]["impact"]["score"]

    db_session = next(client.app.dependency_overrides[get_db]())
    audits = db_session.scalars(select(AuditLog)).all()
    assert len([row for row in audits if row.action == "metric.merge"]) == 1
    assert db_session.scalar(select(func.count()).select_from(Formula)) == 4
    db_session.close()

    listing = client.get("/api/merges", headers=_auth("viewer@metricreg.io", "viewer")).json()
    assert listing["total"] == 1
    detail = client.get(f"/api/merges/{first.json()['id']}", headers=_auth("viewer@metricreg.io", "viewer")).json()
    assert detail["archived"]["sources"][0]["metric"]["code"] == "gmv_net"


def test_merge_rejects_unknown_and_mismatched_names(client):
    bad = client.post(
        "/api/merges", json={"survivor_code": "nope", "source_codes": ["gmv_net"]}, headers=_auth("admin@metricreg.io", "admin")
    )
    assert bad.status_code == 404
    empty = client.post(
        "/api/merges", json={"survivor_code": "gmv", "source_codes": []}, headers=_auth("admin@metricreg.io", "admin")
    )
    assert empty.status_code == 422


def test_merge_blocked_returns_conflict_paths(client):
    db_session = next(client.app.dependency_overrides[get_db]())
    metric = db_session.scalar(select(Metric).where(Metric.code == "gmv"))
    formula_service.save_version(
        db_session, metric, dsl_text="gmv = sum(@gmv_net) group by dt", author_email="admin@metricreg.io"
    )
    db_session.commit()
    blocked = client.post("/api/merges", json=PAYLOAD, headers=_auth("analyst@metricreg.io", "analyst"))
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "MERGE_BLOCKED"
    assert blocked.json()["detail"]["conflict_paths"] == ["gmv → gmv"]
    # 未做写入
    db_session.expire_all()
    assert db_session.scalar(select(func.count()).select_from(MetricMerge)) == 0
    statuses = {m.code: m.status for m in db_session.scalars(select(Metric)).all()}
    assert statuses == {"gmv": "active", "gmv_net": "active", "net_revenue": "active"}
    assert db_session.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "metric.merge")) == 0
    db_session.close()


def test_merged_source_disappears_from_conflict_page_and_metric_list(client):
    client.post("/api/merges", json=PAYLOAD, headers=_auth("admin@metricreg.io", "admin"))
    conflicts = client.get("/api/conflicts", headers=_auth("viewer@metricreg.io", "viewer")).json()
    assert all("GMV" != item["name"] for item in conflicts["items"])
    metrics = client.get("/api/metrics", headers=_auth("viewer@metricreg.io", "viewer")).json()
    assert all(item["code"] != "gmv_net" for item in metrics["items"])
    merged = client.get(
        "/api/metrics", params={"status": "merged"}, headers=_auth("viewer@metricreg.io", "viewer")
    ).json()
    assert [item["code"] for item in merged["items"]] == ["gmv_net"]
    assert merged["items"][0]["merged_into"] == "gmv"


def test_exports_show_merged_status_and_target(client):
    client.post("/api/merges", json=PAYLOAD, headers=_auth("admin@metricreg.io", "admin"))
    response = client.get("/api/exports/metrics.csv", headers=_auth("admin@metricreg.io", "admin"))
    assert response.status_code == 200
    text = response.content.decode("utf-8-sig")
    header = text.splitlines()[0]
    assert "合并到" in header
    row = next(line for line in text.splitlines() if line.startswith("gmv_net,"))
    assert "已合并" in row and "gmv" in row


def test_read_endpoints_after_merge(client):
    client.post("/api/merges", json=PAYLOAD, headers=_auth("admin@metricreg.io", "admin"))
    headers = _auth("admin@metricreg.io", "admin")
    overview = client.get("/api/overview", headers=headers)
    assert overview.status_code == 200
    body = overview.json()
    assert body["metrics_total"] == 3 and body["metrics_effective"] == 2 and body["merged_metrics"] == 1
    assert body["merge_count"] == 1
    graph = client.get("/api/graph", headers=headers).json()
    assert all(node["code"] != "gmv_net" for node in graph["nodes"])
    assert graph["stats"]["nodes"] == 2
    assert client.get("/api/conflicts", headers=headers).json()["total"] == 0
    assert client.get("/api/reports", headers=headers).status_code == 200
    assert client.get("/api/merges", headers=headers).json()["total"] == 1
    assert client.get("/api/exports/conflicts.csv", headers=headers).status_code == 200
    audits = client.get("/api/audit", headers=headers).json()
    assert any(item["action"] == "metric.merge" for item in audits["items"])
    metrics = client.get("/api/metrics", params={"status": "merged"}, headers=headers).json()
    detail = client.get(f"/api/metrics/{metrics['items'][0]['id']}", headers=headers).json()
    assert detail["metric"]["merged_into"] == "gmv"
    assert detail["history"][0]["version"] == 1


def test_csv_exports_after_merge(client):
    client.post("/api/merges", json=PAYLOAD, headers=_auth("admin@metricreg.io", "admin"))
    headers = _auth("admin@metricreg.io", "admin")
    conflicts_csv = client.get("/api/exports/conflicts.csv", headers=headers).content.decode("utf-8-sig")
    assert "GMV" not in conflicts_csv or "口径成员" in conflicts_csv.splitlines()[0]
    changes_csv = client.get("/api/exports/change-requests.csv", headers=headers).content.decode("utf-8-sig")
    runs_csv = client.get("/api/exports/runs.csv", headers=headers).content.decode("utf-8-sig")
    assert "变更单号" in changes_csv.splitlines()[0]
    assert "运行号" in runs_csv.splitlines()[0]
    metrics_csv = client.get("/api/exports/metrics.csv", headers=headers).content.decode("utf-8-sig")
    assert metrics_csv.count("\n") >= 3
