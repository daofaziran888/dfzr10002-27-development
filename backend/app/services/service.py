"""数据服务发布业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.services import cache_graph
from app.store import store

MODULE = "service"
REQUIRED_FIELDS = ["服务编号", "服务名称", "输出数据集"]
STATUS_ORDER = ["草稿", "待审批", "已发布", "已停用"]
ACTION_RULES = {"提交审批": "待审批", "发布服务": "已发布", "停用服务": "已停用"}
NEGATIVE_ACTIONS = ["停用服务"]


class ServiceService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        # 数据服务是缓存规则的下游引用方：待刷新标记由缓存规则当前状态统一回算，
        # 避免数据服务自己改状态后与缓存规则失去一致。
        cache_graph.recompute_reference_flags()
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("服务编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        cache_graph.recompute_reference_flags()
        entry = store.find(MODULE, entry_id)
        if entry is not None:
            entry["关联缓存规则"] = [
                rule["规则编号"]
                for rule in store.rows("cache")
                if str(rule.get("缓存目标") or "") == str(entry.get("输出数据集") or "")
                or any(
                    str(item.get("编号") or "") == str(entry.get("服务编号") or "")
                    for item in (rule.get("下游引用") or [])
                    if isinstance(item, dict)
                )
            ]
        return entry

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"数据服务 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据服务发布可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        # 数据服务只是缓存规则的下游引用方：待刷新标记始终按缓存规则当前状态回算，
        # pending 与待刷新标记保持同一口径，不允许数据服务自己改写。
        cache_graph.recompute_reference_flags()
        entry["pending"] = bool(entry.get("待刷新")) or target == STATUS_ORDER[0]
        return entry, f"数据服务已{action}"
