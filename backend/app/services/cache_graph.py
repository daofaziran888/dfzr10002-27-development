"""查询缓存依赖图与失效级联。

缓存规则（cache）的上游依赖可以指向数据集、查询模板或另一条缓存规则；下游引用
指向数据服务。数据集刷新时按依赖层级向下传播，先标记缓存规则待刷新，再同步标记
其它业务面的查询模板与数据服务清单。

依赖保存与冲突优先级（本模块内固定规则）：
1. 依赖去重后按「类型、编号」校验目标必须存在；
2. 缓存规则不能依赖自身，也不能形成环；保存即拒绝，发布前再校验一次；
3. 跨团队依赖只允许指向共享缓存规则：共享规则优先于团队规则，团队规则又优先于
   其它团队规则；被拒绝时返回优先级说明；
4. 同一缓存目标上只允许一条已启用规则：共享规则 > 本团队规则，同优先级按规则
   id 小者优先（先发布者保留），新规则必须等旧规则停用后才能启用。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Iterable

from app.store import store

ENTITY_TYPES: dict[str, tuple[str, str]] = {
    "数据集": ("dataset", "数据集编号"),
    "查询模板": ("query", "模板编号"),
    "缓存规则": ("cache", "规则编号"),
    "数据服务": ("service", "服务编号"),
}
UPSTREAM_TYPES = ("数据集", "查询模板", "缓存规则")
DOWNSTREAM_TYPES = ("数据服务",)
SHARED_TEAM = "全平台共享"


# ---------------------------------------------------------------- 基础工具

def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def rules() -> list[dict[str, Any]]:
    return store.rows("cache")


def find_entity(kind: str, code: str) -> dict[str, Any] | None:
    mapping = ENTITY_TYPES.get(kind)
    if mapping is None:
        return None
    module, field = mapping
    for row in store.rows(module):
        if str(row.get(field) or "") == str(code):
            return row
    return None


def rule_by_code(code: str) -> dict[str, Any] | None:
    for rule in rules():
        if str(rule.get("规则编号") or "") == str(code):
            return rule
    return None


def dependency_key(item: dict[str, Any]) -> tuple[str, str]:
    return str(item.get("类型") or "").strip(), str(item.get("编号") or "").strip()


def rule_dependencies(rule: dict[str, Any], kind: str | None = None) -> list[dict[str, Any]]:
    items = [item for item in (rule.get("上游依赖") or []) if isinstance(item, dict)]
    if kind is not None:
        items = [item for item in items if item.get("类型") == kind]
    return items


def rule_downstream(rule: dict[str, Any], kind: str | None = None) -> list[dict[str, Any]]:
    """下游引用清单；已发布规则同时带上发布时固化的下游关系，便于追溯。"""
    items = [item for item in (rule.get("下游引用") or []) if isinstance(item, dict)]
    if kind is not None:
        items = [item for item in items if item.get("类型") == kind]
    return items


def downstream_services(rule: dict[str, Any]) -> list[str]:
    """缓存规则的下游数据服务：显式下游引用 + 发布快照里的下游引用 + 输出数据集等于缓存目标的服务。"""
    codes = {
        str(item.get("编号") or "").strip()
        for item in rule_downstream(rule, "数据服务")
        if str(item.get("编号") or "").strip()
    }
    codes.update(
        str(item.get("编号") or "").strip()
        for item in (rule.get("发布依赖快照") or [])
        if isinstance(item, dict) and item.get("类型") == "数据服务"
        and str(item.get("编号") or "").strip()
    )
    target = str(rule.get("缓存目标") or "").strip()
    for service in store.rows("service"):
        if target and str(service.get("输出数据集") or "").strip() == target:
            codes.add(str(service.get("服务编号") or "").strip())
    codes.discard("")
    return sorted(codes)


def downstream_templates(rule: dict[str, Any]) -> list[str]:
    """规则失效时受影响的查询模板：上游里声明的查询模板依赖。"""
    return sorted(
        str(item.get("编号") or "").strip()
        for item in rule_dependencies(rule, "查询模板")
        if str(item.get("编号") or "").strip()
    )


def declared_upstream(rule: dict[str, Any]) -> list[dict[str, Any]]:
    """规则的上下游关系：当前草稿关系 + 发布时固化的关系，用于追溯已发布规则。"""
    items = rule_dependencies(rule)
    seen = {dependency_key(item) for item in items}
    for item in rule.get("发布依赖快照") or []:
        if not isinstance(item, dict):
            continue
        key = dependency_key(item)
        if key[0] in UPSTREAM_TYPES and key not in seen:
            seen.add(key)
            items.append({"类型": key[0], "编号": key[1]})
    return items


def normalize_dependencies(
    kind: str,
    entries: Iterable[dict[str, Any]],
    *,
    allowed_types: tuple[str, ...],
) -> tuple[list[dict[str, str]], list[str]]:
    """校验并去重依赖条目；返回规范化列表与可读的错误说明。"""
    normalized: list[dict[str, str]] = []
    errors: list[str] = []
    seen: set[tuple[str, str]] = set()
    for item in entries:
        if not isinstance(item, dict):
            errors.append(f"{kind}存在无法识别的条目，应为「类型 + 编号」")
            continue
        item_type, code = dependency_key(item)
        if item_type not in allowed_types:
            errors.append(f"{kind}类型「{item_type or '未填写'}」不在允许范围：{'、'.join(allowed_types)}")
            continue
        if not code:
            errors.append(f"{kind}缺少编号")
            continue
        if (item_type, code) in seen:
            continue
        if find_entity(item_type, code) is None:
            errors.append(f"{kind}指向的{item_type}「{code}」不存在或已归档")
            continue
        seen.add((item_type, code))
        normalized.append({"类型": item_type, "编号": code})
    return normalized, errors


# ---------------------------------------------------------------- 环与图

def detect_rule_cycles() -> list[list[str]]:
    """返回缓存规则依赖图的环（每条是规则编号列表），无环时返回空列表。"""
    codes = [str(rule.get("规则编号") or "") for rule in rules()]
    graph: dict[str, list[str]] = defaultdict(list)
    for rule in rules():
        code = str(rule.get("规则编号") or "")
        for item in declared_upstream(rule):
            if item.get("类型") != "缓存规则":
                continue
            target = str(item.get("编号") or "").strip()
            if target in codes:
                graph[code].append(target)

    color: dict[str, int] = {code: 0 for code in codes}
    stack: list[str] = []
    cycles: list[list[str]] = []
    found: set[tuple[str, ...]] = set()

    def dfs(node: str) -> None:
        color[node] = 1
        stack.append(node)
        for nxt in graph.get(node, []):
            if color.get(nxt, 0) == 1:
                start = stack.index(nxt)
                cycle = stack[start:] + [nxt]
                key = tuple(sorted(set(cycle)))
                if key not in found:
                    found.add(key)
                    cycles.append(cycle)
            elif color.get(nxt, 0) == 0:
                dfs(nxt)
        stack.pop()
        color[node] = 2

    for code in codes:
        if color[code] == 0:
            dfs(code)
    return cycles


def would_create_cycle(rule: dict[str, Any], candidates: list[dict[str, str]]) -> list[str] | None:
    """把候选依赖装进临时副本后检查环，返回命中环的规则编号列表。"""
    code = str(rule.get("规则编号") or "")
    original = rule.get("上游依赖")
    rule["上游依赖"] = candidates
    try:
        for cycle in detect_rule_cycles():
            if code and code in cycle:
                return cycle
    finally:
        rule["上游依赖"] = original[:] if isinstance(original, list) else original
    return None


# ---------------------------------------------------------------- 状态一致性

def normalize_rule_pending(rule: dict[str, Any]) -> None:
    """待刷新标记必须与缓存规则状态一致：只有已启用规则可以位于待刷新。"""
    enabled = rule.get("status") == "已启用"
    if not enabled:
        rule["待刷新"] = False
        rule["刷新来源"] = ""
    else:
        rule["待刷新"] = bool(rule.get("待刷新"))


def recompute_reference_flags() -> None:
    """按当前缓存规则的待刷新状态，重算查询模板与数据服务清单的待刷新标记。"""
    pending_templates: dict[str, str] = {}
    pending_services: dict[str, str] = {}
    for rule in rules():
        normalize_rule_pending(rule)
        if not rule.get("待刷新"):
            continue
        source = str(rule.get("刷新来源") or "")
        for code in downstream_templates(rule):
            pending_templates.setdefault(code, source)
        for code in downstream_services(rule):
            pending_services.setdefault(code, source)

    for template in store.rows("query"):
        code = str(template.get("模板编号") or "")
        template["待刷新"] = code in pending_templates
        template["刷新来源"] = pending_templates.get(code, "")

    for service in store.rows("service"):
        code = str(service.get("服务编号") or "")
        service["待刷新"] = code in pending_services
        service["刷新来源"] = pending_services.get(code, "")


# ---------------------------------------------------------------- 失效传播

def _matches_source(rule: dict[str, Any], source_kind: str, source_code: str) -> bool:
    """规则是否直接依赖来源节点。"""
    return any(
        dependency_key(item) == (source_kind, source_code)
        for item in declared_upstream(rule)
    )


def affected_rules(source_kind: str, source_code: str) -> list[dict[str, Any]]:
    """按依赖层级返回受影响的缓存规则，层级顺序即失效顺序。

    从来源节点出发逐层向下：先直接依赖来源的规则，再依赖这些规则的规则。
    """
    frontier: set[tuple[str, str]] = {(source_kind, source_code)}
    if source_kind == "缓存规则":
        rule = rule_by_code(source_code)
        if rule is not None:
            frontier.add(("数据集", str(rule.get("缓存目标") or "")))
    ordered: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    while frontier:
        layer: list[dict[str, Any]] = []
        for rule in sorted(rules(), key=lambda item: int(item.get("id", 0))):
            if int(rule.get("id", 0)) in seen_ids:
                continue
            if any(_matches_source(rule, kind, code) for kind, code in frontier):
                layer.append(rule)
        if not layer:
            break
        ordered.extend(layer)
        seen_ids.update(int(rule.get("id", 0)) for rule in layer)
        frontier = {("缓存规则", str(rule.get("规则编号") or "")) for rule in layer}
    return ordered


def invalidate(
    source_kind: str,
    source_code: str,
    *,
    operator: str = "值班管理员",
) -> dict[str, Any]:
    """标记失效：返回按依赖顺序排列的受影响节点，并同步其它业务面的待刷新清单。"""
    if source_kind not in ("数据集", "查询模板", "缓存规则"):
        return {"来源类型": source_kind, "来源编号": source_code, "依赖顺序": [], "待刷新缓存规则": [], "run": None}
    affected = [
        rule for rule in affected_rules(source_kind, source_code)
        if not (source_kind == "缓存规则" and str(rule.get("规则编号") or "") == source_code)
    ]
    steps: list[dict[str, Any]] = []
    for rule in affected:
        normalize_rule_pending(rule)
        enabled = rule.get("status") == "已启用"
        if enabled:
            rule["待刷新"] = True
            rule["刷新来源"] = source_code
        steps.append({
            "类型": "缓存规则",
            "编号": rule["规则编号"],
            "规则编号": rule["规则编号"],
            "缓存目标": rule.get("缓存目标", ""),
            "归属团队": rule.get("归属团队", ""),
            "当前快照版本": rule.get("当前快照版本", ""),
            "状态": "待刷新" if enabled else "跳过",
            "说明": "已标记待刷新，等待预加载回填" if enabled else f"缓存规则状态为{rule.get('status')}，不参与待刷新",
        })
        for code in downstream_templates(rule):
            if source_kind == "查询模板" and code == source_code:
                continue
            steps.append({
                "类型": "查询模板",
                "编号": code,
                "关联缓存规则": rule["规则编号"],
                "状态": "待刷新" if enabled else "跳过",
                "说明": "缓存规则的查询模板依赖随失效同步标记",
            })
        for code in downstream_services(rule):
            steps.append({
                "类型": "数据服务",
                "编号": code,
                "关联缓存规则": rule["规则编号"],
                "状态": "待刷新" if enabled else "跳过",
                "说明": "数据服务输出依赖该缓存，随失效同步标记",
            })

    recompute_reference_flags()

    run_id = max((int(item.get("id", 0)) for item in store.rows("cache_run")), default=0) + 1
    run_steps = [
        {
            "顺序": index + 1,
            "id": rule.get("id"),
            "规则编号": rule["规则编号"],
            "缓存目标": rule.get("缓存目标", ""),
            "归属团队": rule.get("归属团队", ""),
            "状态": "待回填" if rule.get("status") == "已启用" else "跳过",
            "失败原因": "",
            "快照版本": "",
            "耗时": 0,
        }
        for index, rule in enumerate(affected)
    ]
    run = {
        "id": run_id,
        "来源类型": source_kind,
        "来源编号": source_code,
        "触发人": operator,
        "触发时间": now_text(),
        "状态": "待回填" if any(step["状态"] == "待回填" for step in run_steps) else "无需回填",
        "步骤": run_steps,
        "失败起点": -1,
        "最近更新": now_text(),
    }
    store.rows("cache_run").append(run)

    return {
        "来源类型": source_kind,
        "来源编号": source_code,
        "依赖顺序": steps,
        "待刷新缓存规则": [rule["规则编号"] for rule in affected if rule.get("待刷新")],
        "run": run,
    }
