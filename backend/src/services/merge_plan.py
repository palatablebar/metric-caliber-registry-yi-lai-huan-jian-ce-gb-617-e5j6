"""口径合并规划（算法 B 扩展，纯函数、确定性）。

业务选保留口径后，把同名指标的旧口径并入保留指标：

- 引用迁移：其他指标 / 报表里指向旧指标的引用改指保留指标；同时引用两者的去重，避免重复计数；
- 旧路径补全：调用方传入的 `references` 是「当前解析出的 DSL 引用 ∪ 依赖边表」的并集，
  即使上次校验后有人改了口径、依赖边还停留在旧状态，合并规划也不会漏掉旧路径；
- 自引用 / 新环检测：把旧指标节点折叠进保留指标后重新求 SCC。只要环上有一条边是「旧引用重写而来」，
  这个环就是本次合并产生的，整次合并必须保留原样并输出冲突路径；
- 影响面：保留指标与被合并指标的下游报表按 code 去重后按权重求和，
  保留指标原有的报表只算一次，不重复计分。
"""
from __future__ import annotations

from src.constants.enums import REPORT_WEIGHT_CORE, REPORT_WEIGHT_NORMAL
from src.services import graph_analysis


def _report_index(reports: list[dict]) -> dict[str, list[dict]]:
    index: dict[str, list[dict]] = {}
    for report in sorted(reports, key=lambda item: item["code"]):
        weight = report.get("weight") or (REPORT_WEIGHT_CORE if report.get("is_core") else REPORT_WEIGHT_NORMAL)
        entry = dict(report, weight=int(weight), is_core=bool(report.get("is_core")))
        for metric_code in report.get("metric_codes") or []:
            index.setdefault(metric_code, []).append(entry)
    return index


def _rewrite(deps: list[str], sources: set[str], survivor: str) -> tuple[list[str], list[dict], list[str]]:
    """把 deps 里的旧指标引用改指 survivor，并返回被去重的重复引用。"""
    rewritten: list[str] = []
    rewired: list[dict] = []
    duplicates: list[str] = []
    seen: set[str] = set()
    for dep in deps:
        target = survivor if dep in sources else dep
        if target in seen:
            duplicates.append(dep)
            continue
        seen.add(target)
        rewritten.append(target)
        if target != dep:
            rewired.append({"from": dep, "to": target})
    return sorted(rewritten), rewired, duplicates


def active_cycle_paths(dep_map: dict[str, list[str]]) -> list[dict]:
    """对折叠后的引用图求 SCC，返回真实环路径（等长取字典序）。"""
    cycles: list[dict] = []
    for component in graph_analysis.tarjan_scc(dep_map):
        is_cycle = len(component) > 1 or any(code in dep_map.get(code, []) for code in component)
        if not is_cycle:
            continue
        members = set(component)
        path = graph_analysis.shortest_cycle(min(component), dep_map, members)
        cycles.append({"path": path, "length": len(path) - 1, "members": sorted(members)})
    cycles.sort(key=lambda item: (item["length"], item["path"]))
    return cycles


def _blocked(reason: str, *, survivor_code: str, sources: list[str], codes: list[str]) -> dict:
    return {
        "ok": False,
        "kind": "invalid",
        "survivor_code": survivor_code,
        "source_codes": sources,
        "metrics": [],
        "reference_graph_after": {},
        "references_migrated": [],
        "reports_migrated": [],
        "reports_migrated_count": 0,
        "impact": {},
        "blockers": [
            {"kind": "invalid", "path": codes, "length": 0, "rewired_edges": [], "message": reason}
        ],
        "new_cycles": [],
        "existing_cycles": [],
    }


def plan_merge(
    metrics: list[dict],
    references: dict[str, list[str]],
    reports: list[dict],
    *,
    survivor_code: str,
    source_codes: list[str],
) -> dict:
    """计算一次同名口径合并的迁移计划；`ok=False` 时不得落库。"""
    by_code = {metric["code"]: metric for metric in metrics}
    sources = sorted({code for code in source_codes if code != survivor_code})
    known = set(by_code)
    unknown = [code for code in [survivor_code] + sources if code not in known]
    if unknown:
        return _blocked(f"指标不存在：{' / '.join(unknown)}", survivor_code=survivor_code, sources=sources, codes=unknown)
    if not sources:
        return _blocked("至少需要一个被合并指标", survivor_code=survivor_code, sources=sources, codes=[survivor_code])

    source_set = set(sources)
    active_codes = [code for code in sorted(by_code) if code not in source_set]
    active_set = set(active_codes)
    after: dict[str, list[str]] = {}
    rewired_map: dict[tuple[str, str], list[str]] = {}
    references_migrated: list[dict] = []
    for code in active_codes:
        refs = sorted({ref for ref in references.get(code, []) if ref in known})
        mapped, rewired, duplicates = _rewrite(refs, source_set, survivor_code)
        mapped = sorted(ref for ref in mapped if ref in active_set)
        after[code] = mapped
        for edge in rewired:
            rewired_map.setdefault((code, edge["to"]), []).append(edge["from"])
        if mapped != refs:
            references_migrated.append(
                {
                    "code": code,
                    "before": refs,
                    "after": mapped,
                    "rewired": rewired,
                    "deduped": duplicates,
                }
            )

    blockers: list[dict] = []
    new_cycles: list[dict] = []
    existing_cycles: list[dict] = []
    for cycle in active_cycle_paths(after):
        path = cycle["path"]
        edges = []
        for left, right in zip(path, path[1:]):
            for original in rewired_map.get((left, right), []):
                edges.append({"from": left, "original_to": original, "to": right})
        is_self = len(path) == 2 and path[0] == path[1]
        entry = {
            "kind": "self_reference" if is_self else "cycle",
            "path": path,
            "length": cycle["length"],
            "members": cycle["members"],
            "rewired_edges": edges,
        }
        edge_text = "、".join(f"{edge['from']} → {edge['original_to']}" for edge in edges)
        if is_self:
            entry["message"] = (
                f"合并后 {path[0]} 会引用自身（{edge_text} 被重写为 {path[0]} → {path[0]}）"
                if edges
                else f"依赖图原本就有自引用：{path[0]} → {path[0]}"
            )
        else:
            entry["message"] = "合并后出现依赖环：" + " → ".join(path)
            if edges:
                entry["message"] += f"（其中 {edge_text} 被重写为指向保留指标）"
        if edges:
            new_cycles.append(entry)
            blockers.append(entry)
        else:
            existing_cycles.append(entry)

    reports_index = _report_index(reports)
    union_codes = [survivor_code] + sources
    union_reports = graph_analysis.downstream_reports(union_codes, reports_index)
    before_reports = graph_analysis.downstream_reports([survivor_code], reports_index)
    impact = graph_analysis.impact_payload(union_reports)
    impact_before = graph_analysis.impact_payload(before_reports)
    before_codes = {report["code"] for report in before_reports}
    # 同时引用保留口径与旧口径的报表：如果按指标逐个累加会被算两次，这里显式去重
    duplicate_codes = sorted(before_codes & {report["code"] for report in union_reports})
    impact.update(
        {
            "reports_before": impact_before["reports"],
            "report_count_before": impact_before["report_count"],
            "score_before": impact_before["score"],
            "duplicate_report_codes": duplicate_codes,
            "duplicate_report_count": len(duplicate_codes),
        }
    )

    reports_migrated: list[dict] = []
    for report in union_reports:
        metric_codes = list(report.get("metric_codes") or [])
        if not source_set.intersection(metric_codes):
            continue
        mapped = [survivor_code if code in source_set else code for code in metric_codes]
        kept: list[str] = []
        for code in mapped:
            if code not in kept:
                kept.append(code)
        reports_migrated.append(
            {
                "code": report["code"],
                "name": report["name"],
                "channel": report.get("channel"),
                "is_core": bool(report.get("is_core")),
                "weight": int(report["weight"]),
                "before": metric_codes,
                "after": kept,
                "deduped": sorted({code for code in mapped if mapped.count(code) > 1}),
            }
        )

    metrics_payload = []
    for code in [survivor_code] + sources:
        before = sorted({ref for ref in references.get(code, []) if ref in known})
        metrics_payload.append(
            {
                "code": code,
                "name": by_code[code].get("name"),
                "status": by_code[code].get("status"),
                "version": by_code[code].get("version"),
                "expression": by_code[code].get("expression"),
                "deps_before": before,
                "deps_after": after.get(code) if code == survivor_code else None,
            }
        )

    return {
        "ok": not blockers,
        "kind": "merge",
        "survivor_code": survivor_code,
        "survivor_name": by_code[survivor_code].get("name"),
        "source_codes": sources,
        "source_names": [by_code[code].get("name") for code in sources],
        "metrics": metrics_payload,
        "reference_graph_after": after,
        "references_migrated": references_migrated,
        "reports_migrated": reports_migrated,
        "reports_migrated_count": len(reports_migrated),
        "impact": impact,
        "blockers": blockers,
        "new_cycles": new_cycles,
        "existing_cycles": existing_cycles,
    }
