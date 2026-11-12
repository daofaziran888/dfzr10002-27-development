/** 统一请求封装：拼后端地址、抛网络错误、给页脚留一句可读的说明。 */
const API_BASE = import.meta.env.VITE_API_BASE ?? ''

export type OperatorPreset = {
  key: string
  name: string
  team: string
  permissions: string[]
}

/** 当前操作人预设：切换后随每个请求发送 X-Operator 头，后端据此判断缓存规则权限。 */
export const OPERATOR_PRESETS: OperatorPreset[] = [
  {
    key: 'platform-admin',
    name: '值班管理员',
    team: '数据平台组',
    permissions: ['cache:edit', 'cache:invalidate:edit', 'cache:refresh', 'cache:shared:manage'],
  },
  {
    key: 'platform-no-invalidate',
    name: '平台实习生',
    team: '数据平台组',
    permissions: ['cache:edit', 'cache:refresh'],
  },
  {
    key: 'growth-viewer',
    name: '增长分析组访客',
    team: '增长分析组',
    permissions: [],
  },
  {
    key: 'audit-readonly',
    name: '审计只读岗',
    team: '审计组',
    permissions: ['cache:shared:manage'],
  },
]

let activeOperator: OperatorPreset = OPERATOR_PRESETS[0]

export function getActiveOperator(): OperatorPreset {
  return activeOperator
}

export function setActiveOperator(preset: OperatorPreset): void {
  activeOperator = preset
}

export function operatorHeaders(): Record<string, string> {
  // HTTP 头只能承载 ASCII，中文名称按 URL 编码传输，后端解码后用于权限判断。
  return {
    'X-Operator': encodeURIComponent(activeOperator.name),
    'X-Operator-Team': encodeURIComponent(activeOperator.team),
    'X-Operator-Permissions': activeOperator.permissions.join(','),
  }
}

export function request(path: string, init?: RequestInit): Promise<Response> {
  const url = path.startsWith('http') ? path : `${API_BASE}${path}`
  return fetch(url, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...operatorHeaders(),
      ...(init?.headers ?? {}),
    },
  }).catch((error: unknown) => {
    const detail = error instanceof Error ? error.message : '请求未送达'
    throw new Error(`接口请求失败：${detail}`)
  })
}

export async function fetchJson<T>(path: string): Promise<T> {
  const response = await request(path)
  if (!response.ok) {
    throw new Error(`接口返回 ${response.status}，数据未更新`)
  }
  return (await response.json()) as T
}
