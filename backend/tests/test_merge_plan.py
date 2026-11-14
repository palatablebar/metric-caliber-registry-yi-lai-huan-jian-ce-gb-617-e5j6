"""口径合并规划单测：正常迁移 / 旧路径补全 / 自引用与环阻止 / 影响面去重 / 确定性。"""
from __future__ import annotations

from src.services import merge_plan


def _metric(code, name, deps=None, status="active", fields=None):
    return {
        "code": code,
        "name": name,
        "domain": "trade",
        "tier": "normal",
        "owner": "测试",
        "status": status,
        "version": 1,
        "signature": f"sig-{code}",
        "parse_status": "ok",
        "aggs": ["sum"],
        "fields": fields or ["dwd.order.amount"],
        "filters": [],
        "dims": ["dt"],
        "deps": deps or [],
        "expression": f"{code} = sum(@x)" if deps else f"{code} = sum(dwd.order.amount)",
    }


def _report(code, is_core, metric_codes):
    return {
        "code": code,
        "name": code,
        "channel": "BI",
        "is_core": is_core,
        "weight": 3 if is_core else 1,
        "metric_codes": metric_codes,
    }


METRICS = [
    _metric("gmv", "GMV"),
    _metric("gmv_net", "GMV", deps=[]),
    _metric("net_revenue", "净营收", deps=["gmv_net"]),
    _metric("channel_gmv", "渠道GMV", deps=["gmv", "gmv_net"]),
    _metric("member_gmv", "会员GMV", deps=["gmv"]),
]

REPORTS = [
    _report("r01", True, ["gmv", "gmv_net", "net_revenue"]),
    _report("r02", True, ["gmv_net"]),
    _report("r03", False, ["channel_gmv"]),
]


def test_references_rewritten_and_deduplicated():
    plan = merge_plan.plan_merge(
        METRICS,
        {"gmv": [], "gmv_net": [], "net_revenue": ["gmv_net"], "channel_gmv": ["gmv", "gmv_net"], "member_gmv": ["gmv"]},
        REPORTS,
        survivor_code="gmv",
        source_codes=["gmv_net"],
    )
    assert plan["ok"] is True
    after = plan["reference_graph_after"]
    assert after["net_revenue"] == ["gmv"]
    # 同时引用 gmv 与 gmv_net 的指标只保留一条边，避免重复计算
    assert after["channel_gmv"] == ["gmv"]
    deduped = next(item for item in plan["references_migrated"] if item["code"] == "channel_gmv")
    assert deduped["deduped"] == ["gmv_net"]
    assert "gmv_net" not in after


def test_reports_migrated_and_impact_not_double_counted():
    plan = merge_plan.plan_merge(
        METRICS,
        {},
        REPORTS,
        survivor_code="gmv",
        source_codes=["gmv_net"],
    )
    impact = plan["impact"]
    # 合并前 gmv 只有 r01（3 分），合并后并集 r01/r02 = 3+3（r03 只引用 channel_gmv）
    assert impact["score_before"] == 3
    assert impact["score"] == 6
    # r01 同时引用两个口径，只算一次
    assert impact["report_count"] == 2
    assert impact["duplicate_report_codes"] == ["r01"]
    migrated = {item["code"]: item for item in plan["reports_migrated"]}
    assert migrated["r01"]["after"] == ["gmv", "net_revenue"]
    assert migrated["r01"]["deduped"] == ["gmv"]


def test_old_dependency_path_is_seen_even_if_not_in_current_dsl():
    """依赖边表里残留的旧引用（当前 DSL 已不再引用它）也必须参与环检测。"""
    references = {
        "gmv": [],
        "gmv_net": [],
        "cycle_a": ["gmv_net"],
        "cycle_b": ["cycle_a"],
        "gmv": [],
    }
    metrics = [
        _metric("gmv", "GMV"),
        _metric("gmv_net", "GMV"),
        _metric("cycle_a", "A"),
        _metric("cycle_b", "B"),
    ]
    # 合并 gmv_net -> gmv：cycle_b -> cycle_a -> gmv 仍是一条 DAG，不阻止
    plan = merge_plan.plan_merge(
        metrics, references, [], survivor_code="gmv", source_codes=["gmv_net"]
    )
    assert plan["ok"] is True
    assert plan["reference_graph_after"]["cycle_a"] == ["gmv"]


def test_self_reference_blocks_whole_merge():
    metrics = [
        _metric("gmv", "GMV", deps=["gmv_net"]),  # 保留口径引用旧口径 -> 合并后自引用
        _metric("gmv_net", "GMV"),
    ]
    plan = merge_plan.plan_merge(
        metrics,
        {"gmv": ["gmv_net"], "gmv_net": []},
        REPORTS,
        survivor_code="gmv",
        source_codes=["gmv_net"],
    )
    assert plan["ok"] is False
    blockers = plan["blockers"]
    assert len(blockers) == 1
    assert blockers[0]["kind"] == "self_reference"
    assert blockers[0]["path"] == ["gmv", "gmv"]
    assert blockers[0]["rewired_edges"][0]["original_to"] == "gmv_net"
    assert "引用自身" in blockers[0]["message"]


def test_rewrite_creates_cycle_blocks_with_conflict_path():
    metrics = [
        _metric("gmv", "GMV"),
        _metric("gmv_net", "GMV"),
        _metric("a", "A", deps=["gmv_net"]),
        _metric("b", "B", deps=["a"]),
        _metric("gmv", "GMV"),  # 重复定义不影响 by_code
    ]
    references = {
        "gmv": ["b"],  # 保留口径引用 b
        "gmv_net": [],
        "a": ["gmv_net"],  # a -> gmv_net 重写为 a -> gmv
        "b": ["a"],
    }
    plan = merge_plan.plan_merge(
        metrics, references, [], survivor_code="gmv", source_codes=["gmv_net"]
    )
    assert plan["ok"] is False
    paths = [" → ".join(item["path"]) for item in plan["blockers"]]
    assert paths == ["a → gmv → b → a"]
    edge = plan["blockers"][0]["rewired_edges"][0]
    assert edge == {"from": "a", "original_to": "gmv_net", "to": "gmv"}


def test_preexisting_cycle_not_caused_by_merge_is_reported_but_not_blocking():
    metrics = [_metric("gmv", "GMV"), _metric("gmv_net", "GMV")]
    references = {"gmv": [], "gmv_net": []}
    metrics += [_metric("x", "X", deps=["y"]), _metric("y", "Y", deps=["x"])]
    references.update({"x": ["y"], "y": ["x"]})
    plan = merge_plan.plan_merge(
        metrics, references, [], survivor_code="gmv", source_codes=["gmv_net"]
    )
    assert plan["ok"] is True
    assert plan["new_cycles"] == []
    assert [item["path"] for item in plan["existing_cycles"]] == [["x", "y", "x"]]


def test_invalid_and_deterministic():
    plan = merge_plan.plan_merge(
        METRICS, {}, REPORTS, survivor_code="missing", source_codes=["gmv"]
    )
    assert plan["ok"] is False and plan["kind"] == "invalid"
    first = merge_plan.plan_merge(METRICS, {}, REPORTS, survivor_code="gmv", source_codes=["gmv_net"])
    second = merge_plan.plan_merge(METRICS, {}, REPORTS, survivor_code="gmv", source_codes=["gmv_net"])
    assert first == second
