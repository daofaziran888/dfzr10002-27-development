"""查询缓存管理接口：缓存规则、失效依赖图、预加载回填与历史命中记录。

缓存规则是主对象；缓存目标是规则上的数据集编号。列表与动作接口保持原有契约
（{items, total, page, size} / {ok, message}），新增接口同样在业务层完成权限与状态校验。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.access import Actor, resolve_actor
from app.services.cache import CacheService

router = APIRouter(prefix="/api/cache", tags=["查询缓存管理"])

service = CacheService()

LIST_FIELDS = ["规则编号", "缓存目标", "缓存时间", "失效条件", "预加载设置", "内存上限", "淘汰策略", "是否启用"]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]


def _actor(
    operator: str | None,
    team: str | None,
    permissions: str | None,
) -> Actor:
    return resolve_actor(operator, team, permissions)


def _actor_headers(
    x_operator: str | None,
    x_operator_team: str | None,
    x_operator_permissions: str | None,
) -> Actor:
    return _actor(x_operator, x_operator_team, x_operator_permissions)


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按规则编号或缓存目标检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用"),
    page: int = 1,
    size: int = 20,
    x_operator: str | None = Header(default=None, alias="X-Operator"),
    x_operator_team: str | None = Header(default=None, alias="X-Operator-Team"),
    x_operator_permissions: str | None = Header(default=None, alias="X-Operator-Permissions"),
) -> PageResult[dict]:
    """按规则编号与状态过滤查询缓存管理列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/me")
def current_actor(
    x_operator: str | None = Header(default=None, alias="X-Operator"),
    x_operator_team: str | None = Header(default=None, alias="X-Operator-Team"),
    x_operator_permissions: str | None = Header(default=None, alias="X-Operator-Permissions"),
) -> dict[str, Any]:
    """当前操作人的团队与权限，前端据此展示只读提示。"""
    return _actor_headers(x_operator, x_operator_team, x_operator_permissions).describe()


@router.get("/graph")
def dependency_graph() -> dict[str, Any]:
    """缓存规则的上下游依赖图；未发布的规则也会出现在节点里。"""
    return service.dependency_graph()


@router.get("/hits", response_model=PageResult[dict])
def list_hits(
    rule_id: int | None = Query(default=None),
    keyword: str | None = Query(default=None, description="按规则编号、查询模板或数据服务检索"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """历史命中记录：只读保留，规则失效或待刷新都不会清除。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_hits(rule_id=rule_id, keyword=keyword, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/runs", response_model=PageResult[dict])
def list_runs(page: int = 1, size: int = 20) -> PageResult[dict]:
    """刷新与回填记录；失败起点保存在记录里，便于从失败节点重试。"""
    items, total = service.list_runs(page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/snapshots")
def list_snapshots(rule_id: int | None = Query(default=None)) -> dict[str, Any]:
    """缓存快照历史：回填会新增当前快照并把旧快照标为历史。"""
    rows = service.list_snapshots(rule_id)
    return {"module": "cache_snapshot", "total": len(rows), "items": rows}


@router.post("/refresh", response_model=ActionResult)
def refresh_dataset(
    payload: EntryPayload,
    x_operator: str | None = Header(default=None, alias="X-Operator"),
    x_operator_team: str | None = Header(default=None, alias="X-Operator-Team"),
    x_operator_permissions: str | None = Header(default=None, alias="X-Operator-Permissions"),
) -> ActionResult:
    """刷新分析数据集：按依赖顺序失效缓存规则，缓存规则进入待刷新。

    values 支持：数据集编号。失效后由 POST /api/cache/preload 回填新快照。
    """
    code = str(payload.values.get("数据集编号") or payload.values.get("缓存目标") or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="缺少必填字段：数据集编号")
    result, message = service.refresh_dataset(
        code,
        actor=_actor_headers(x_operator, x_operator_team, x_operator_permissions),
    )
    if result is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=result)


@router.post("/preload", response_model=ActionResult)
def preload(
    payload: EntryPayload,
    x_operator: str | None = Header(default=None, alias="X-Operator"),
    x_operator_team: str | None = Header(default=None, alias="X-Operator-Team"),
    x_operator_permissions: str | None = Header(default=None, alias="X-Operator-Permissions"),
) -> ActionResult:
    """预加载回填：默认回填最近一次刷新；回填中断后从最早失败节点重试。"""
    raw_run_id = payload.values.get("run_id") or payload.values.get("刷新记录")
    run_id = int(raw_run_id) if str(raw_run_id or "").strip().isdigit() else None
    dataset_code = str(payload.values.get("数据集编号") or "").strip() or None
    fail_at = payload.values.get("模拟失败节点") or []
    if isinstance(fail_at, str):
        fail_at = [item.strip() for item in fail_at.split(",") if item.strip()]
    run, message = service.preload(
        run_id,
        actor=_actor_headers(x_operator, x_operator_team, x_operator_permissions),
        dataset_code=dataset_code,
        fail_at_rules=[str(item) for item in fail_at],
    )
    if run is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=run)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出查询缓存管理清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "cache", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条缓存规则明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"缓存规则 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(
    payload: EntryPayload,
    x_operator: str | None = Header(default=None, alias="X-Operator"),
    x_operator_team: str | None = Header(default=None, alias="X-Operator-Team"),
    x_operator_permissions: str | None = Header(default=None, alias="X-Operator-Permissions"),
) -> ActionResult:
    """登记一条缓存规则，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(
        payload.values,
        _actor_headers(x_operator, x_operator_team, x_operator_permissions),
    )
    if entry is None:
        return ActionResult(ok=False, message=f"缓存规则未登记：{'；'.join(missing)}")
    return ActionResult(ok=True, message="缓存规则已登记", entry=entry)


@router.put("/{entry_id}", response_model=ActionResult)
def update_entry(
    entry_id: int,
    payload: EntryPayload,
    x_operator: str | None = Header(default=None, alias="X-Operator"),
    x_operator_team: str | None = Header(default=None, alias="X-Operator-Team"),
    x_operator_permissions: str | None = Header(default=None, alias="X-Operator-Permissions"),
) -> ActionResult:
    """修改缓存规则；越权团队只能查看，共享规则除失效条件外只读。"""
    entry, message = service.update_entry(
        entry_id,
        payload.values,
        _actor_headers(x_operator, x_operator_team, x_operator_permissions),
    )
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/dependencies", response_model=ActionResult)
def save_dependencies(
    entry_id: int,
    payload: EntryPayload,
    x_operator: str | None = Header(default=None, alias="X-Operator"),
    x_operator_team: str | None = Header(default=None, alias="X-Operator-Team"),
    x_operator_permissions: str | None = Header(default=None, alias="X-Operator-Permissions"),
) -> ActionResult:
    """保存上下游依赖；循环依赖、跨团队依赖与共享只读都会在保存时拒绝。"""
    entry, message = service.save_dependencies(
        entry_id,
        upstream=payload.values.get("上游依赖"),
        downstream=payload.values.get("下游引用"),
        actor=_actor_headers(x_operator, x_operator_team, x_operator_permissions),
    )
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/publish", response_model=ActionResult)
def publish(
    entry_id: int,
    x_operator: str | None = Header(default=None, alias="X-Operator"),
    x_operator_team: str | None = Header(default=None, alias="X-Operator-Team"),
    x_operator_permissions: str | None = Header(default=None, alias="X-Operator-Permissions"),
) -> ActionResult:
    """发布缓存规则：发布前校验循环依赖与缓存目标冲突，发布时记录上下游关系。"""
    entry, message = service.publish_dependencies(
        entry_id,
        _actor_headers(x_operator, x_operator_team, x_operator_permissions),
    )
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(
    entry_id: int,
    payload: EntryPayload,
    x_operator: str | None = Header(default=None, alias="X-Operator"),
    x_operator_team: str | None = Header(default=None, alias="X-Operator-Team"),
    x_operator_permissions: str | None = Header(default=None, alias="X-Operator-Permissions"),
) -> ActionResult:
    """对单条缓存规则执行提交校验、启用配置、停用配置；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(
        entry_id,
        action,
        _actor_headers(x_operator, x_operator_team, x_operator_permissions),
    )
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
