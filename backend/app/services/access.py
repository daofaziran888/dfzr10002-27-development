"""操作人身份与缓存规则权限：团队归属、共享规则与失效条件编辑权限。

当前工作台没有接入统一登录，身份由请求头传入，前端在页面顶部切换操作人预设：
- X-Operator：操作人名称
- X-Operator-Team：操作人所属团队
- X-Operator-Permissions：逗号分隔的权限点

权限点约定：
- cache:edit：维护本团队缓存规则（登记、保存依赖、发布、停用）
- cache:invalidate:edit：修改缓存规则的失效条件
- cache:refresh：触发数据集刷新与预加载回填
- cache:shared:manage：维护共享缓存规则（共享规则默认只读；同时持有 cache:edit
  时才允许对共享规则执行发布、停用、保存依赖等写动作，失效条件仍单独受控）

不带请求头时按“数据平台组值班管理员”处理，方便本地 curl 与启动探针直接调用。
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import unquote

PLATFORM_TEAM = "数据平台组"
SHARED_TEAM = "全平台共享"
DEFAULT_PERMISSIONS = (
    "cache:edit",
    "cache:invalidate:edit",
    "cache:refresh",
    "cache:shared:manage",
)


@dataclass(frozen=True)
class Actor:
    name: str = "值班管理员"
    team: str = PLATFORM_TEAM
    permissions: frozenset[str] = frozenset(DEFAULT_PERMISSIONS)

    def has(self, permission: str) -> bool:
        """是否持有某个权限点；空权限集合可以查看但不能改动。"""
        return permission in self.permissions

    def describe(self) -> dict[str, object]:
        return {
            "操作人": self.name,
            "团队": self.team,
            "权限": sorted(self.permissions),
        }


DEFAULT_ACTOR = Actor()


def resolve_actor(
    operator: str | None = None,
    team: str | None = None,
    permissions: str | None = None,
) -> Actor:
    """把请求头解析成操作人；缺省时返回拥有全部缓存权限的值班管理员。"""
    if operator is None and team is None and permissions is None:
        return DEFAULT_ACTOR
    parsed = frozenset(
        item.strip() for item in (permissions or "").split(",") if item.strip()
    )
    # HTTP 头只能是 ASCII，团队与操作人名称按 URL 编码传输，这里解码回中文。
    operator_name = unquote(operator).strip() if operator else ""
    team_name = unquote(team).strip() if team else ""
    return Actor(
        name=operator_name or "值班管理员",
        team=team_name or PLATFORM_TEAM,
        permissions=parsed,
    )
