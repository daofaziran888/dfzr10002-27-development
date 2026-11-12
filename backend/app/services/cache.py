"""查询缓存管理业务规则：状态流转、依赖保存、失效传播、预加载回填与权限校验。

主对象是缓存规则（cache），缓存目标、规则编号与模块状态必须一致；数据服务只是
缓存规则的下游引用方，它的待刷新标记由 cache_graph.recompute_reference_flags 统一
回算，保证与缓存规则的当前状态一致。

权限模型见 app.services.access：
- 越权团队只能查看规则，所有写操作返回 ok=False 并说明原因；
- 共享缓存规则除失效条件外始终只读，失效条件需要 cache:shared:manage；
- 修改失效条件需要 cache:invalidate:edit。
"""
from __future__ import annotations

from typing import Any

from app.services import cache_graph as graph
from app.services.access import SHARED_TEAM, Actor
from app.store import store

MODULE = "cache"
REQUIRED_FIELDS = ["规则编号", "缓存目标", "缓存时间"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用配置": "已启用", "停用配置": "已停用"}
NEGATIVE_ACTIONS = ["停用配置"]
EDITABLE_FIELDS = ["缓存目标", "缓存时间", "失效条件", "预加载设置", "内存上限", "淘汰策略", "是否启用", "备注"]


def is_shared(rule: dict[str, Any]) -> bool:
    return bool(rule.get("共享规则")) or str(rule.get("归属团队") or "") == SHARED_TEAM


class CacheService:
    # ------------------------------------------------------------ 列表与明细

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        graph.recompute_reference_flags()
        rows = store.rows(MODULE)
        if keyword:
            rows = [
                row for row in rows
                if keyword in str(row.get("规则编号", ""))
                or keyword in str(row.get("缓存目标", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        graph.recompute_reference_flags()
        entry = store.find(MODULE, entry_id)
        if entry is not None:
            self._attach_usage(entry)
        return entry

    def _attach_usage(self, entry: dict[str, Any]) -> dict[str, Any]:
        """明细里补充上下游与待刷新状态，字段只在读取时计算，不落库。"""
        entry["下游数据服务"] = graph.downstream_services(entry)
        entry["下游查询模板"] = graph.downstream_templates(entry)
        entry["依赖环"] = [
            cycle for cycle in graph.detect_rule_cycles()
            if entry["规则编号"] in cycle
        ]
        entry["命中记录数"] = sum(
            1 for hit in store.rows("cache_hit")
            if int(hit.get("rule_id", 0)) == int(entry.get("id", 0))
        )
        return entry

    def list_hits(
        self,
        *,
        rule_id: int | None = None,
        keyword: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """历史命中记录只读保留，规则失效或进入待刷新都不会清掉。"""
        rows = store.rows("cache_hit")
        if rule_id is not None:
            rows = [row for row in rows if int(row.get("rule_id", 0)) == rule_id]
        if keyword:
            rows = [
                row for row in rows
                if keyword in str(row.get("规则编号", ""))
                or keyword in str(row.get("查询模板", ""))
                or keyword in str(row.get("数据服务", ""))
            ]
        rows = sorted(rows, key=lambda row: (str(row.get("命中时间", "")), int(row.get("id", 0))), reverse=True)
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def list_snapshots(self, rule_id: int | None = None) -> list[dict[str, Any]]:
        rows = store.rows("cache_snapshot")
        if rule_id is not None:
            rows = [row for row in rows if int(row.get("rule_id", 0)) == rule_id]
        return rows

    # ------------------------------------------------------------ 权限

    def permission_error(self, rule: dict[str, Any], actor: Actor, *, field: str | None = None) -> str | None:
        """返回越权原因；None 表示可以改动。"""
        shared = is_shared(rule)
        owner = str(rule.get("归属团队") or "")
        if shared:
            if field is None:
                if actor.has("cache:shared:manage") and actor.has("cache:edit"):
                    return None
                return "共享缓存规则为只读，仅允许修改失效条件"
            if field != "失效条件":
                return f"共享缓存规则的{field}为只读字段，不能修改"
            if not actor.has("cache:shared:manage"):
                return "修改共享缓存规则的失效条件需要 cache:shared:manage 权限"
            return None
        if actor.team != owner:
            return f"缓存规则归属「{owner}」，当前团队「{actor.team}」只能查看，不能改动"
        if not actor.has("cache:edit"):
            return "当前操作人没有 cache:edit 权限，只能查看缓存规则"
        if field == "失效条件" and not actor.has("cache:invalidate:edit"):
            return "修改失效条件需要 cache:invalidate:edit 权限"
        return None

    def can_edit(self, rule: dict[str, Any] | None, actor: Actor) -> bool:
        if rule is None:
            return False
        return self.permission_error(rule, actor) is None

    # ------------------------------------------------------------ 登记与修改

    def create_entry(self, values: dict[str, Any], actor: Actor) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        target = str(values.get("缓存目标") or "").strip()
        if graph.find_entity("数据集", target) is None:
            return None, [f"缓存目标「{target}」必须对应已登记的分析数据集编号"]
        if graph.rule_by_code(str(values.get("规则编号") or "").strip()):
            return None, [f"规则编号「{values.get('规则编号')}」已存在"]
        if not actor.has("cache:edit"):
            return None, ["当前操作人没有 cache:edit 权限，不能登记缓存规则"]

        normalized, errors = graph.normalize_dependencies(
            "上游依赖",
            values.get("上游依赖") or [],
            allowed_types=graph.UPSTREAM_TYPES,
        )
        if errors:
            return None, errors
        downstream, downstream_errors = graph.normalize_dependencies(
            "下游引用",
            values.get("下游引用") or [],
            allowed_types=graph.DOWNSTREAM_TYPES,
        )
        if downstream_errors:
            return None, downstream_errors

        rows = store.rows(MODULE)
        entry: dict[str, Any] = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["共享规则"] = bool(values.get("共享规则"))
        entry["归属团队"] = SHARED_TEAM if entry["共享规则"] else str(values.get("归属团队") or actor.team)
        entry["共享范围"] = values.get("共享范围") or ("全平台只读" if entry["共享规则"] else "团队内")
        entry["预加载设置"] = values.get("预加载设置") or "关闭"
        entry["失效条件"] = values.get("失效条件") or "数据集版本变更"
        entry["内存上限"] = values.get("内存上限") or "256MB"
        entry["淘汰策略"] = values.get("淘汰策略") or "LRU"
        entry["是否启用"] = "否"
        entry["待刷新"] = False
        entry["刷新来源"] = ""
        entry["刷新失败原因"] = ""
        entry["当前快照版本"] = "v0"
        entry["依赖版本"] = 1
        entry["最近回填"] = ""
        entry["备注"] = values.get("备注") or ""

        if entry["归属团队"] != actor.team and not entry["共享规则"]:
            return None, [f"只能在「{actor.team}」名下登记缓存规则，跨团队请登记为共享规则"]
        if entry["共享规则"] and not actor.has("cache:shared:manage"):
            return None, ["登记共享缓存规则需要 cache:shared:manage 权限"]
        if not _is_positive_int(entry.get("缓存时间")):
            return None, ["缓存时间必须是大于 0 的整数秒"]

        cycle = graph.would_create_cycle(entry, normalized)
        if cycle:
            return None, [self._cycle_message(cycle)]
        for item in normalized:
            if item["类型"] == "缓存规则":
                dependency = graph.rule_by_code(item["编号"])
                if dependency is not None:
                    permission = self._cross_team_dependency_error(entry, dependency, actor)
                    if permission:
                        return None, [permission]
        entry["上游依赖"] = normalized
        entry["下游引用"] = downstream
        rows.append(entry)
        graph.recompute_reference_flags()
        return entry, []

    def update_entry(
        self,
        entry_id: int,
        values: dict[str, Any],
        actor: Actor,
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"缓存规则 {entry_id} 不存在或已归档"
        shared = is_shared(entry)

        changes = {field: values[field] for field in EDITABLE_FIELDS if field in values}
        if not changes:
            return entry, "没有需要保存的字段变更"
        for field in changes:
            permission = self.permission_error(entry, actor, field=field)
            if permission:
                return None, permission

        if "缓存目标" in changes:
            target = str(changes["缓存目标"] or "").strip()
            if graph.find_entity("数据集", target) is None:
                return None, f"缓存目标「{target}」必须对应已登记的分析数据集编号"
            if target != str(entry.get("缓存目标") or "") and entry.get("status") == "已启用":
                return None, "已启用缓存规则不能更换缓存目标，请先停用后重新发布"
            changes["缓存目标"] = target
        if "是否启用" in changes:
            if shared:
                # 共享规则的启用状态随状态机走，不允许直接改「是否启用」字段。
                changes.pop("是否启用")
            elif str(changes["是否启用"]).strip() not in ("是", "否"):
                return None, "是否启用只能填「是」或「否」"
        if "缓存时间" in changes and not str(changes["缓存时间"]).strip():
            return None, "缓存时间不能为空"
        if "缓存时间" in changes and not _is_positive_int(changes["缓存时间"]):
            return None, "缓存时间必须是大于 0 的整数秒"

        for field, value in changes.items():
            entry[field] = value
        entry["依赖版本"] = int(entry.get("依赖版本", 1)) + 1
        if "失效条件" in changes:
            entry["变更说明"] = f"{actor.name}（{actor.team}）更新失效条件：{changes['失效条件']}"
        graph.recompute_reference_flags()
        return entry, "缓存规则已保存"

    # ------------------------------------------------------------ 依赖图

    def save_dependencies(
        self,
        entry_id: int,
        *,
        upstream: list[dict[str, Any]] | None,
        downstream: list[dict[str, Any]] | None,
        actor: Actor,
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"缓存规则 {entry_id} 不存在或已归档"
        permission = self.permission_error(entry, actor)
        if permission:
            return None, permission

        if upstream is None:
            upstream = entry.get("上游依赖") or []
        if downstream is None:
            downstream = entry.get("下游引用") or []
        normalized, errors = graph.normalize_dependencies(
            "上游依赖", upstream, allowed_types=graph.UPSTREAM_TYPES
        )
        normalized_down, downstream_errors = graph.normalize_dependencies(
            "下游引用", downstream, allowed_types=graph.DOWNSTREAM_TYPES
        )
        if errors or downstream_errors:
            return None, "；".join(errors + downstream_errors)

        cycle = graph.would_create_cycle(entry, normalized)
        if cycle:
            return None, self._cycle_message(cycle)
        for item in normalized:
            if item["类型"] == "缓存规则":
                dependency = graph.rule_by_code(item["编号"])
                if dependency is not None:
                    error = self._cross_team_dependency_error(entry, dependency, actor)
                    if error:
                        return None, error

        entry["上游依赖"] = normalized
        entry["下游引用"] = normalized_down
        entry["依赖版本"] = int(entry.get("依赖版本", 1)) + 1
        graph.recompute_reference_flags()
        return entry, "缓存依赖关系已保存"

    def dependency_graph(self) -> dict[str, Any]:
        graph.recompute_reference_flags()
        nodes = [
            {
                "id": rule.get("id"),
                "规则编号": rule["规则编号"],
                "缓存目标": rule.get("缓存目标", ""),
                "归属团队": rule.get("归属团队", ""),
                "共享规则": bool(rule.get("共享规则")),
                "status": rule.get("status"),
                "待刷新": bool(rule.get("待刷新")),
                "上游依赖": graph.declared_upstream(rule),
                "下游引用": rule.get("下游引用") or [],
                "下游数据服务": graph.downstream_services(rule),
            }
            for rule in graph.rules()
        ]
        edges = [
            {
                "from": item["编号"],
                "to": rule["规则编号"],
                "类型": item["类型"],
            }
            for rule in graph.rules()
            for item in graph.declared_upstream(rule)
        ]
        return {
            "nodes": nodes,
            "edges": edges,
            "cycles": graph.detect_rule_cycles(),
        }

    # ------------------------------------------------------------ 动作与发布

    def run_action(
        self,
        entry_id: int,
        action: str,
        actor: Actor,
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"缓存规则 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于查询缓存管理可执行范围"
        permission = self.permission_error(entry, actor)
        if permission:
            return None, permission
        target = ACTION_RULES[action]

        if action == "启用配置":
            if entry.get("status") == "已启用":
                return entry, "缓存规则已处于已启用状态"
            if not str(entry.get("缓存目标") or "").strip():
                return None, "缓存目标为空，不能发布缓存规则"
            if graph.find_entity("数据集", str(entry.get("缓存目标"))) is None:
                return None, f"缓存目标「{entry.get('缓存目标')}」对应的数据集不存在，不能启用"
            cycle = graph.detect_rule_cycles()
            for loop in cycle:
                if entry["规则编号"] in loop:
                    return None, self._cycle_message(loop)
            conflict = self._target_conflict(entry, str(entry.get("缓存目标")), releasing=True)
            if conflict:
                return None, conflict
            entry["status"] = target
            entry["pending"] = True
            entry["abnormal"] = False
            entry["是否启用"] = "是"
            entry["发布依赖快照"] = [
                dict(item) for item in (entry.get("上游依赖") or []) + (entry.get("下游引用") or [])
                if isinstance(item, dict)
            ]
            entry["依赖版本"] = int(entry.get("依赖版本", 1)) + 1
            graph.recompute_reference_flags()
            return entry, "缓存规则已发布并记录上下游依赖"

        if action == "停用配置":
            if entry.get("status") == "已停用":
                return entry, "缓存规则已处于已停用状态"
            entry["status"] = target
            entry["pending"] = False
            entry["abnormal"] = True
            entry["是否启用"] = "否"
            graph.normalize_rule_pending(entry)
            graph.recompute_reference_flags()
            return entry, "缓存规则已停用，待刷新标记随之清除"

        if action == "提交校验":
            entry["status"] = target
            entry["pending"] = True
            entry["abnormal"] = False
            graph.normalize_rule_pending(entry)
            graph.recompute_reference_flags()
            return entry, "缓存规则已提交校验"

        return None, f"目标状态「{target}」不在允许的状态序列里"

    def publish_dependencies(self, entry_id: int, actor: Actor) -> tuple[dict[str, Any] | None, str]:
        """发布规则：确认依赖无环、目标有效后进入已启用，并记录上下游关系。"""
        return self.run_action(entry_id, "启用配置", actor)

    def _target_conflict(self, entry: dict[str, Any], target: str, *, releasing: bool = False) -> str | None:
        """同一缓存目标只允许一条已启用规则；共享规则 > 本团队规则 > 其它团队规则。"""
        if not target:
            return None
        others = [
            rule for rule in graph.rules()
            if int(rule.get("id", 0)) != int(entry.get("id", 0))
            and rule.get("status") == "已启用"
            and str(rule.get("缓存目标") or "") == target
        ]
        if not others:
            return None
        shared = is_shared(entry)
        for other in others:
            other_shared = is_shared(other)
            if shared and other_shared:
                return (
                    f"缓存目标「{target}」已被共享规则 {other['规则编号']} 占用；"
                    "共享规则之间按规则编号小者优先，请先停用旧规则"
                )
            if not shared and other_shared:
                return (
                    f"缓存目标「{target}」已被共享规则 {other['规则编号']} 占用；"
                    "共享规则优先于团队规则，请先停用共享规则"
                )
            if shared and not other_shared:
                if releasing:
                    return f"共享规则将接管缓存目标「{target}」，请先停用团队规则 {other['规则编号']}"
                return None
            if str(other.get("归属团队") or "") == str(entry.get("归属团队") or ""):
                first = min(int(other.get("id", 0)), int(entry.get("id", 0)))
                winner = other["规则编号"] if int(other.get("id", 0)) == first else entry["规则编号"]
                return (
                    f"缓存目标「{target}」已有本团队启用规则 {other['规则编号']}；"
                    f"同优先级按规则编号小者优先，保留 {winner}，请先停用旧规则"
                )
            if int(other.get("id", 0)) < int(entry.get("id", 0)):
                return (
                    f"缓存目标「{target}」已被其它团队规则 {other['规则编号']} 占用；"
                    "跨团队冲突时先发布者优先，请改用共享规则或等待旧规则停用"
                )
        return None

    def _cross_team_dependency_error(
        self, entry: dict[str, Any], dependency: dict[str, Any], actor: Actor
    ) -> str | None:
        if is_shared(entry) or is_shared(dependency):
            return None
        entry_team = str(entry.get("归属团队") or "")
        dependency_team = str(dependency.get("归属团队") or "")
        if entry_team and dependency_team and entry_team != dependency_team:
            return (
                f"缓存规则「{entry.get('规则编号')}」归属「{entry_team}」，不能依赖其它团队的"
                f"缓存规则「{dependency.get('规则编号')}」（{dependency_team}）；"
                "跨团队依赖只允许指向共享缓存规则"
            )
        return None

    @staticmethod
    def _cycle_message(cycle: list[str]) -> str:
        return "检测到循环依赖：" + " → ".join(cycle) + "，已拒绝保存"

    # ------------------------------------------------------------ 失效与回填

    def refresh_dataset(
        self,
        dataset_code: str,
        *,
        actor: Actor,
    ) -> tuple[dict[str, Any] | None, str]:
        """刷新分析数据集：按依赖顺序失效并把缓存规则置为待刷新。

        刷新只负责失效；新快照由预加载回填写入，回填中断后从最早失败节点重试。
        """
        if not actor.has("cache:refresh"):
            return None, "刷新数据集与预加载回填需要 cache:refresh 权限"
        dataset = graph.find_entity("数据集", dataset_code)
        if dataset is None:
            return None, f"数据集「{dataset_code}」不存在或已归档"

        previous = str(dataset.get("数据集版本") or "d0")
        dataset["数据集版本"] = _next_dataset_version(previous)
        dataset["刷新时间"] = graph.now_text()
        dataset["最近刷新"] = graph.now_text()
        dataset["pending"] = True

        invalidation = graph.invalidate("数据集", dataset_code, operator=actor.name)
        run = invalidation["run"]
        run["数据集版本"] = dataset["数据集版本"]
        run["上次数据集版本"] = previous

        graph.recompute_reference_flags()
        pending = len(invalidation["待刷新缓存规则"])
        suffix = "已进入待刷新队列，等待预加载回填" if pending else "没有需要回填的缓存规则"
        return {
            "数据集": dataset_code,
            "数据集版本": dataset["数据集版本"],
            "依赖顺序": invalidation["依赖顺序"],
            "run": run,
        }, f"数据集 {dataset_code} 已刷新，{pending} 条缓存规则{suffix}"

    def preload(
        self,
        run_id: int | None = None,
        *,
        actor: Actor,
        dataset_code: str | None = None,
        fail_at_rules: list[str] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """预加载回填：默认取最近一次刷新，回填中断后从最早失败节点重试。

        失败起点记录在刷新记录里；重试时从该节点重新开始，已成功的节点不会重复回填。
        """
        if not actor.has("cache:refresh"):
            return None, "预加载回填需要 cache:refresh 权限"
        run = self._find_run(run_id, dataset_code)
        if run is None:
            return None, "没有可回填的刷新记录，请先刷新数据集" if run_id is None else f"刷新记录 {run_id} 不存在"
        steps = run.get("步骤") or []
        retry_index = int(run.get("失败起点", -1))
        if retry_index < 0:
            snapshot_version = str(run.get("数据集版本") or "")
            retry_index = next(
                (
                    index for index, step in enumerate(steps)
                    if step.get("状态") in ("待回填", "失败")
                    and step.get("数据集版本", snapshot_version) == snapshot_version
                ),
                -1,
            )
        if retry_index < 0:
            return run, "没有待回填的节点，预加载无需执行"
        if run.get("状态") == "已完成" and retry_index < 0:
            return run, "刷新记录已回填完成"

        run["状态"] = "回填中"
        run["最近更新"] = graph.now_text()
        filled = self._backfill(
            run,
            str(run.get("来源编号") or ""),
            stop_rules=set(fail_at_rules or []),
            start_index=retry_index,
        )
        graph.recompute_reference_flags()
        if run.get("状态") == "已完成":
            message = f"预加载回填完成，共 {len(filled)} 个节点"
        else:
            message = f"预加载回填中断于 {run.get('最终失败节点') or '未知节点'}，可从失败节点重试"
        return run, message

    def list_runs(self, page: int = 1, size: int = 20) -> tuple[list[dict[str, Any]], int]:
        rows = sorted(store.rows("cache_run"), key=lambda row: int(row.get("id", 0)), reverse=True)
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    # ------------------------------------------------------------ 内部回填

    def _find_run(self, run_id: int | None, dataset_code: str | None) -> dict[str, Any] | None:
        rows = store.rows("cache_run")
        if run_id is not None:
            return next((row for row in rows if int(row.get("id", 0)) == run_id), None)
        candidates = [
            row for row in rows
            if dataset_code is None or str(row.get("来源编号") or "") == dataset_code
        ]
        return max(candidates, key=lambda row: int(row.get("id", 0)), default=None)

    def _backfill(
        self,
        run: dict[str, Any],
        dataset_code: str,
        *,
        stop_rules: set[str],
        start_index: int = 0,
    ) -> list[dict[str, Any]]:
        dataset = graph.find_entity("数据集", dataset_code) or {}
        steps: list[dict[str, Any]] = run.get("步骤") or []
        run_version = str(run.get("数据集版本") or "")
        filled: list[dict[str, Any]] = []
        interrupted = False

        for index in range(start_index, len(steps)):
            step = steps[index]
            code = str(step.get("规则编号") or "")
            if step.get("状态") == "成功":
                continue
            if step.get("状态") == "跳过":
                continue
            rule = graph.rule_by_code(code)
            if rule is None:
                step["状态"] = "失败"
                step["失败原因"] = "缓存规则不存在或已归档"
                interrupted = code in stop_rules
                run["失败起点"] = index
                run["最终失败节点"] = code
                run["状态"] = "中断"
                break
            if code in stop_rules:
                step["状态"] = "失败"
                step["失败原因"] = "回填中断：快照写入失败，等待从该节点重试"
                step["快照版本"] = ""
                run["失败起点"] = index
                run["最终失败节点"] = code
                run["状态"] = "中断"
                rule["刷新失败原因"] = step["失败原因"]
                graph.normalize_rule_pending(rule)
                interrupted = True
                break
            if rule.get("status") != "已启用":
                step["状态"] = "跳过"
                step["失败原因"] = ""
                continue

            target_dataset = graph.find_entity("数据集", str(rule.get("缓存目标") or "")) or dataset
            snapshot_version = run_version or str(target_dataset.get("数据集版本") or "v1")
            _append_snapshot(rule, target_dataset, snapshot_version)
            step["状态"] = "成功"
            step["失败原因"] = ""
            step["快照版本"] = snapshot_version
            rule["当前快照版本"] = snapshot_version
            rule["待刷新"] = False
            rule["刷新来源"] = ""
            rule["刷新失败原因"] = ""
            rule["最近回填"] = graph.now_text()
            filled.append({
                "规则编号": code,
                "缓存目标": rule.get("缓存目标", ""),
                "快照版本": snapshot_version,
                "状态": "已回填",
            })

        if not interrupted:
            run["状态"] = "已完成" if all(
                step.get("状态") in ("成功", "跳过") for step in steps
            ) else "待回填"
            if run["状态"] == "已完成":
                run["失败起点"] = -1
                run["最终失败节点"] = ""
        run["最近更新"] = graph.now_text()
        return filled


def _is_positive_int(value: Any) -> bool:
    try:
        return int(str(value).strip()) > 0
    except (TypeError, ValueError):
        return False


def _next_dataset_version(previous: str) -> str:
    prefix = ""
    for char in previous:
        if char.isalpha():
            prefix += char
        else:
            break
    number = previous[len(prefix):]
    try:
        return f"{prefix or 'd'}{int(number or 0) + 1}"
    except ValueError:
        return f"{prefix or 'd'}1"


def _append_snapshot(rule: dict[str, Any], dataset: dict[str, Any], snapshot_version: str) -> None:
    """回填新快照：旧快照保留为历史，历史命中记录不受影响。

    如果当前快照已经是同一个数据集版本（例如查询模板变化触发的失效），不重复写一条
    相同版本的快照，只刷新生成时间。
    """
    dataset_version = str(dataset.get("数据集版本") or "")
    current = next(
        (
            row for row in store.rows("cache_snapshot")
            if int(row.get("rule_id", 0)) == int(rule.get("id", 0)) and row.get("状态") == "当前"
        ),
        None,
    )
    if current is not None and str(current.get("快照版本")) == snapshot_version \
            and str(current.get("数据集版本")) == dataset_version:
        current["生成时间"] = graph.now_text()
        return
    for row in store.rows("cache_snapshot"):
        if int(row.get("rule_id", 0)) == int(rule.get("id", 0)) and row.get("状态") == "当前":
            row["状态"] = "历史"
    snapshots = store.rows("cache_snapshot")
    snapshots.append({
        "id": max((int(row.get("id", 0)) for row in snapshots), default=0) + 1,
        "rule_id": int(rule.get("id", 0)),
        "规则编号": rule.get("规则编号", ""),
        "快照版本": snapshot_version,
        "数据集编号": dataset.get("数据集编号", rule.get("缓存目标", "")),
        "数据集版本": dataset.get("数据集版本", ""),
        "生成时间": graph.now_text(),
        "行数": _row_count(dataset),
        "状态": "当前",
    })


def _row_count(dataset: dict[str, Any]) -> int:
    raw = str(dataset.get("行数上限") or "").strip()
    return int(raw) if raw.isdigit() else 0
