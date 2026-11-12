import { useCallback, useMemo, useState } from 'react'

import { getActiveOperator, request } from '@/api/client'

const ENDPOINT = '/api/cache'
const columns = [
  '规则编号',
  '缓存目标',
  '缓存时间',
  '失效条件',
  '预加载设置',
  '内存上限',
  '淘汰策略',
  '是否启用',
]
const actions = ['提交校验', '启用配置', '停用配置']
const statuses = ['草稿', '待校验', '已启用', '已停用']
const DEPENDENCY_TYPES = ['数据集', '查询模板', '缓存规则']
const DOWNSTREAM_TYPES = ['数据服务']

type Row = Record<string, any>
type Status = '待刷新' | '待回填' | '成功' | '失败' | '跳过'
type Dependency = { 类型: string; 编号: string }
type SnapshotNode = {
  顺序: number
  id: number
  规则编号: string
  缓存目标: string
  归属团队: string
  状态: Status
  失败原因: string
  快照版本: string
}
type RefreshRun = {
  id: number
  来源类型: string
  来源编号: string
  触发人: string
  触发时间: string
  状态: string
  数据集版本?: string
  失败起点?: number
  最终失败节点?: string
  步骤: SnapshotNode[]
}

function statusClass(status: string): string {
  if (status === '待刷新' || status === '待回填' || status === '中断') return 'status-warn'
  if (status === '失败') return 'status-danger'
  if (status === '成功' || status === '已完成') return 'status-ok'
  return 'status-muted'
}

export default function CacheView() {
  const operator = getActiveOperator()
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [selected, setSelected] = useState<Row | null>(null)
  const [graphData, setGraphData] = useState<{ nodes: Row[]; edges: Row[]; cycles: string[][] }>({ nodes: [], edges: [], cycles: [] })
  const [hits, setHits] = useState<Row[]>([])
  const [snapshots, setSnapshots] = useState<Row[]>([])
  const [runs, setRuns] = useState<RefreshRun[]>([])
  const [filterDatasets, setFilterDatasets] = useState<Row[]>([])
  const [selectedDataset, setSelectedDataset] = useState('DS-001')
  const [draftUpstream, setDraftUpstream] = useState<Dependency[]>([])
  const [draftDownstream, setDraftDownstream] = useState<Dependency[]>([])
  const [draftInvalidation, setDraftInvalidation] = useState('')
  const [failAt, setFailAt] = useState('')
  const [lookupType, setLookupType] = useState('数据集')
  const [lookupCode, setLookupCode] = useState('')
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const loadJson = useCallback(async (path: string) => {
    const response = await request(path)
    if (!response.ok) throw new Error(`接口返回 ${response.status}`)
    return response.json()
  }, [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const payload = await loadJson(`${ENDPOINT}?${query}`)
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '查询缓存管理列表读取失败')
    }
  }, [filters, loadJson])

  const reloadAux = useCallback(async (ruleId?: number) => {
    try {
      const [graphPayload, hitPayload, snapshotPayload, runPayload] = await Promise.all([
        loadJson(`${ENDPOINT}/graph`),
        loadJson(`${ENDPOINT}/hits?size=50${ruleId ? `&rule_id=${ruleId}` : ''}`),
        loadJson(`${ENDPOINT}/snapshots${ruleId ? `?rule_id=${ruleId}` : ''}`),
        loadJson(`${ENDPOINT}/runs?size=10`),
      ])
      setGraphData(graphPayload)
      setHits(hitPayload.items ?? [])
      setSnapshots(snapshotPayload.items ?? [])
      setRuns(runPayload.items ?? [])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '依赖图读取失败')
    }
  }, [loadJson])

  const initialLoad = useCallback(async () => {
    await reload()
    await reloadAux()
    try {
      const datasets = await loadJson('/api/dataset?size=200')
      setFilterDatasets((datasets.items ?? []).filter((item: Row) => String(item.status) !== '已停用'))
    } catch {
      /* 依赖选择器不可用时不影响主列表 */
    }
  }, [loadJson, reload, reloadAux])

  const refreshAll = useCallback(async () => {
    await reload()
    await reloadAux(selected?.id)
    await initialLoad()
  }, [reload, reloadAux, initialLoad, selected])

  const selectRow = useCallback(async (row: Row) => {
    setSelected(row)
    setDraftUpstream(row['上游依赖'] ?? [])
    setDraftDownstream(row['下游引用'] ?? [])
    setDraftInvalidation(String(row['失效条件'] ?? ''))
    setNotice('')
    await reloadAux(row.id)
  }, [reloadAux])

  const selectedTeam = selected ? String(selected['归属团队'] ?? '') : ''
  const selectedShared = Boolean(selected?.['共享规则'])
  const canEditRule = operator.permissions.includes('cache:edit') && selectedTeam === operator.team && !selectedShared
  const canEditInvalidation = selectedShared
    ? operator.permissions.includes('cache:shared:manage')
    : selectedTeam === operator.team && operator.permissions.includes('cache:invalidate:edit')
  const canActOnRow = (row: Row) =>
    operator.permissions.includes('cache:edit') &&
    !Boolean(row['共享规则']) &&
    String(row['归属团队'] ?? '') === operator.team

  async function postAction(path: string, body: Record<string, unknown>, successMessage: string) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(path, {
        method: 'POST',
        body: JSON.stringify({ values: body }),
      })
      const payload = await response.json()
      if (!payload.ok) {
        setErrorMessage(String(payload.message ?? '操作未生效'))
        await refreshAll()
        return
      }
      setNotice(successMessage)
      await refreshAll()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '查询缓存管理操作失败')
    }
  }

  async function runAction(action: string, row: Row) {
    await postAction(`${ENDPOINT}/${row.id}/actions`, { action }, `缓存规则 ${row['规则编号']} 已${action}`)
  }

  async function saveInvalidation() {
    if (!selected) return
    await postAction(
      `${ENDPOINT}/${selected.id}`,
      { 失效条件: draftInvalidation },
      `缓存规则 ${selected['规则编号']} 的失效条件已保存`,
    )
  }

  function addDependency(kind: 'upstream' | 'downstream') {
    const code = lookupCode.trim()
    if (!code) {
      setErrorMessage('请先填写依赖编号')
      return
    }
    const target = kind === 'upstream' ? DEPENDENCY_TYPES : DOWNSTREAM_TYPES
    if (!target.includes(lookupType)) {
      setErrorMessage(`下游引用只接受：${DOWNSTREAM_TYPES.join('、')}`)
      return
    }
    const setter = kind === 'upstream' ? setDraftUpstream : setDraftDownstream
    setter((prev) => (
      prev.some((item) => item.类型 === lookupType && item.编号 === code)
        ? prev
        : [...prev, { 类型: lookupType, 编号: code }]
    ))
    setLookupCode('')
  }

  function removeDependency(kind: 'upstream' | 'downstream', index: number) {
    const setter = kind === 'upstream' ? setDraftUpstream : setDraftDownstream
    setter((prev) => prev.filter((_, position) => position !== index))
  }

  async function saveDependencies() {
    if (!selected) return
    await postAction(
      `${ENDPOINT}/${selected.id}/dependencies`,
      { 上游依赖: draftUpstream, 下游引用: draftDownstream },
      `缓存规则 ${selected['规则编号']} 的上下游关系已保存`,
    )
  }

  async function publishSelected() {
    if (!selected) return
    await postAction(`${ENDPOINT}/${selected.id}/publish`, {}, `缓存规则 ${selected['规则编号']} 已发布`)
  }

  async function refreshDataset() {
    await postAction(
      `${ENDPOINT}/refresh`,
      { 数据集编号: selectedDataset },
      `数据集 ${selectedDataset} 已刷新，依赖缓存进入待刷新`,
    )
  }

  async function retryPreload(runId?: number) {
    await postAction(
      `${ENDPOINT}/preload`,
      { run_id: runId, 模拟失败节点: failAt ? failAt.split(',').map((item) => item.trim()) : [] },
      '预加载回填已执行',
    )
  }

  const latestRun = runs[0]
  const pendingServices = graphData.nodes.length
    ? graphData.nodes
    : []
  const selectedGraph = useMemo(
    () => (selected ? graphData.nodes.find((node) => node.id === selected.id) : undefined),
    [graphData.nodes, selected],
  )

  return (
    <section className="page" data-module="cache">
      <header className="page-head">
        <div>
          <h2>查询缓存管理管理</h2>
          <p className="page-desc">维护分析查询的缓存策略、失效机制与预热配置；刷新数据集后按依赖顺序失效并回填新快照。</p>
        </div>
        <div className="page-actions">
          <span className="team-chip">当前身份：{operator.name}（{operator.team}）</span>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出查询缓存管理清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        <article className="stat-card">
          <span className="stat-label">缓存规则总数</span>
          <strong className="stat-value">{total}</strong>
        </article>
        <article className="stat-card">
          <span className="stat-label">待刷新缓存规则</span>
          <strong className="stat-value">
            {graphData.nodes.filter((node) => node['待刷新']).length}
          </strong>
        </article>
        <article className="stat-card">
          <span className="stat-label">依赖图节点 / 边</span>
          <strong className="stat-value">
            {graphData.nodes.length} / {graphData.edges.length}
          </strong>
        </article>
        <article className="stat-card">
          <span className="stat-label">历史命中记录</span>
          <strong className="stat-value">{hits.length}</strong>
        </article>
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
        <button className="btn ghost" type="button" onClick={() => void initialLoad()}>载入依赖图</button>
      </form>

      {graphData.cycles.length > 0 && (
        <div className="banner danger">
          检测到循环依赖：{graphData.cycles.map((cycle) => cycle.join(' → ')).join('；')}，发布前会被拒绝
        </div>
      )}

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>待刷新</th>
            <th>归属团队</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr
              key={String(row.id)}
              className={selected?.id === row.id ? 'row-selected' : undefined}
              onClick={() => void selectRow(row)}
            >
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td>
                {row['待刷新']
                  ? <span className="status-badge status-warn">待刷新{row['刷新来源'] ? `·${row['刷新来源']}` : ''}</span>
                  : <span className="status-badge status-muted">—</span>}
              </td>
              <td>{row['归属团队']}{row['共享规则'] ? '（共享）' : ''}</td>
              <td className="row-actions">
                {actions.map((action) => (
                  <button
                    key={action}
                    className="link"
                    type="button"
                    disabled={!canActOnRow(row)}
                    onClick={(event) => { event.stopPropagation(); void runAction(action, row) }}
                  >
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 3}>暂无查询缓存管理数据，可先登记缓存规则</td>
            </tr>
          )}
        </tbody>
      </table>

      <div className="detail-grid">
        <article className="panel">
          <h3>缓存规则明细</h3>
          {!selected && <p className="panel-hint">在列表里选择一条缓存规则查看上下游关系。</p>}
          {selected && (
            <>
              <dl className="detail-list">
                <dt>规则编号 / 缓存目标</dt><dd>{selected['规则编号']} · {selected['缓存目标']}（规则与目标必须一致）</dd>
                <dt>状态 / 待刷新</dt>
                <dd>
                  <span className="status-badge">{selected.status}</span>
                  {selected['待刷新']
                    ? <span className="status-badge status-warn">待刷新·{selected['刷新来源']}</span>
                    : <span className="status-badge status-muted">无待刷新</span>}
                </dd>
                <dt>归属团队</dt><dd>{selected['归属团队']}{selected['共享规则'] ? '（共享缓存，只读）' : ''}</dd>
                <dt>当前快照 / 最近回填</dt><dd>{selected['当前快照版本']} · {selected['最近回填'] || '尚未回填'}</dd>
                <dt>依赖版本 / 最近发布</dt><dd>v{selected['依赖版本']} · {(selected['发布依赖快照'] ?? []).length} 条已发布依赖</dd>
                <dt>下游数据服务</dt><dd>{(selectedGraph?.['下游数据服务'] ?? []).join('、') || '—'}</dd>
                <dt>关联查询模板</dt><dd>{(selectedGraph?.['上游依赖'] ?? []).filter((item: Dependency) => item.类型 === '查询模板').map((item: Dependency) => item.编号).join('、') || '—'}</dd>
                <dt>历史命中</dt><dd>{selected['命中记录数'] ?? 0} 条（失效后仍保留）</dd>
              </dl>
              {selected['刷新失败原因'] && <p className="error-text">上次回填失败：{selected['刷新失败原因']}</p>}
              {!canEditRule && (
                <p className="panel-hint">
                  {selectedShared
                    ? '共享缓存规则保持只读，仅失效条件可按权限修改。'
                    : `规则归属「${selectedTeam}」，当前团队「${operator.team}」只能查看不能改动。`}
                </p>
              )}
              <div className="field-row">
                <label className="filter-item">
                  <span>失效条件{canEditInvalidation ? '' : '（无权修改）'}</span>
                  <input
                    value={draftInvalidation}
                    disabled={!canEditInvalidation}
                    onChange={(event) => setDraftInvalidation(event.target.value)}
                  />
                </label>
                <button className="btn" type="button" disabled={!canEditInvalidation} onClick={() => void saveInvalidation()}>
                  保存失效条件
                </button>
              </div>
            </>
          )}
        </article>

        <article className="panel">
          <h3>失效依赖图</h3>
          <div className="dependency-editor">
            <div className="dep-column">
              <strong>上游依赖</strong>
              <ul className="dep-list">
                {draftUpstream.map((item, index) => (
                  <li key={`${item.类型}-${item.编号}`}>
                    <span>{item.类型} {item.编号}</span>
                    {canEditRule && (
                      <button className="link danger" type="button" onClick={() => removeDependency('upstream', index)}>移除</button>
                    )}
                  </li>
                ))}
                {draftUpstream.length === 0 && <li className="panel-hint">暂无上游依赖</li>}
              </ul>
            </div>
            <div className="dep-column">
              <strong>下游引用</strong>
              <ul className="dep-list">
                {draftDownstream.map((item, index) => (
                  <li key={`${item.类型}-${item.编号}`}>
                    <span>{item.类型} {item.编号}</span>
                    {canEditRule && (
                      <button className="link danger" type="button" onClick={() => removeDependency('downstream', index)}>移除</button>
                    )}
                  </li>
                ))}
                {draftDownstream.length === 0 && <li className="panel-hint">暂无下游引用</li>}
              </ul>
            </div>
          </div>
          <div className="field-row">
            <label className="filter-item">
              <span>类型</span>
              <select value={lookupType} onChange={(event) => setLookupType(event.target.value)}>
                {[...DEPENDENCY_TYPES, ...DOWNSTREAM_TYPES].map((type) => (
                  <option key={type} value={type}>{type}</option>
                ))}
              </select>
            </label>
            <label className="filter-item">
              <span>编号</span>
              <input
                value={lookupCode}
                placeholder="如 DS-001 / QUER-0001 / CACH-0002 / SERV-0003"
                onChange={(event) => setLookupCode(event.target.value)}
              />
            </label>
            <button className="btn" type="button" disabled={!canEditRule} onClick={() => addDependency('upstream')}>加为上游</button>
            <button className="btn" type="button" disabled={!canEditRule} onClick={() => addDependency('downstream')}>加为下游</button>
          </div>
          <div className="field-row">
            <button className="btn primary" type="button" disabled={!canEditRule} onClick={() => void saveDependencies()}>
              保存依赖关系
            </button>
            <button className="btn" type="button" disabled={!canEditRule} onClick={() => void publishSelected()}>
              发布规则
            </button>
          </div>
          {graphData.edges.length > 0 && (
            <table className="mini-table">
              <thead><tr><th>起点</th><th>类型</th><th>终点</th></tr></thead>
              <tbody>
                {graphData.edges.map((edge, index) => (
                  <tr key={`${edge.from}-${edge.to}-${index}`}>
                    <td>{edge.from}</td><td>{edge.类型}</td><td>{edge.to}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </article>
      </div>

      <div className="detail-grid">
        <article className="panel">
          <h3>数据集刷新与预加载回填</h3>
          <div className="field-row">
            <label className="filter-item">
              <span>缓存目标（数据集）</span>
              <select value={selectedDataset} onChange={(event) => setSelectedDataset(event.target.value)}>
                {filterDatasets.map((dataset) => (
                  <option key={dataset['数据集编号']} value={dataset['数据集编号']}>
                    {dataset['数据集编号']} · {dataset['数据集名称']}
                  </option>
                ))}
              </select>
            </label>
            <label className="filter-item">
              <span>回填失败节点（预加载时模拟中断，逗号分隔）</span>
              <input value={failAt} placeholder="如 CACH-0004" onChange={(event) => setFailAt(event.target.value)} />
            </label>
            <button className="btn primary" type="button" onClick={() => void refreshDataset()}>刷新数据集</button>
            <button className="btn" type="button" onClick={() => void retryPreload()}>预加载回填 / 从失败节点重试</button>
          </div>
          {latestRun && (
            <>
              <p className="panel-hint">
                刷新记录 #{latestRun.id}：{latestRun.来源编号} 数据集版本 {latestRun.数据集版本 ?? '—'}，
                触发人 {latestRun.触发人}，状态 <span className="status-badge">{latestRun.状态}</span>
                {latestRun.失败起点 !== undefined && latestRun.失败起点 >= 0 && `，失败起点第 ${latestRun.失败起点 + 1} 步`}
              </p>
              <table className="mini-table">
                <thead>
                  <tr><th>顺序</th><th>规则编号</th><th>缓存目标</th><th>状态</th><th>快照版本</th><th>失败原因</th></tr>
                </thead>
                <tbody>
                  {latestRun.步骤.map((step) => (
                    <tr key={step.顺序}>
                      <td>{step.顺序}</td>
                      <td>{step.规则编号}</td>
                      <td>{step.缓存目标}</td>
                      <td><span className={`status-badge ${statusClass(step.状态)}`}>{step.状态}</span></td>
                      <td>{step.快照版本 || '—'}</td>
                      <td>{step.失败原因 || '—'}</td>
                    </tr>
                  ))}
                  {latestRun.步骤.length === 0 && (
                    <tr><td colSpan={6} className="empty-state">该数据集没有受影响的缓存规则</td></tr>
                  )}
                </tbody>
              </table>
            </>
          )}
          {!latestRun && <p className="panel-hint">还没有刷新记录，先选择数据集并刷新。</p>}
          {latestRun && latestRun.失败起点 !== undefined && latestRun.失败起点 >= 0 && (
            <button className="btn" type="button" onClick={() => void retryPreload(latestRun.id)}>
              从失败节点 {latestRun.最终失败节点} 重试
            </button>
          )}
        </article>

        <article className="panel">
          <h3>下游数据服务待刷新清单</h3>
          <ul className="dep-list">
            {pendingServices.map((node) => {
              const services: string[] = node['下游数据服务'] ?? []
              return services.map((code: string) => (
                <li key={`${node.规则编号}-${code}`}>
                  <span>
                    {code} · 由 {node['规则编号']}（{node['缓存目标']}）
                    {node['待刷新']
                      ? <span className="status-badge status-warn">待刷新</span>
                      : <span className="status-badge status-muted">已同步</span>}
                  </span>
                </li>
              ))
            })}
          </ul>
          {graphData.nodes.every((node) => (node['下游数据服务'] ?? []).length === 0) && (
            <p className="panel-hint">暂无下游数据服务引用。</p>
          )}
        </article>
      </div>

      <div className="detail-grid">
        <article className="panel">
          <h3>缓存快照历史</h3>
          <table className="mini-table">
            <thead><tr><th>规则编号</th><th>快照版本</th><th>数据集</th><th>生成时间</th><th>状态</th></tr></thead>
            <tbody>
              {snapshots.map((snapshot) => (
                <tr key={String(snapshot.id)}>
                  <td>{snapshot['规则编号']}</td>
                  <td>{snapshot['快照版本']}</td>
                  <td>{snapshot['数据集编号']}（{snapshot['数据集版本']}）</td>
                  <td>{snapshot['生成时间']}</td>
                  <td><span className={`status-badge ${snapshot['状态'] === '当前' ? 'status-ok' : 'status-muted'}`}>{snapshot['状态']}</span></td>
                </tr>
              ))}
              {snapshots.length === 0 && <tr><td colSpan={5} className="empty-state">暂无快照</td></tr>}
            </tbody>
          </table>
        </article>
        <article className="panel">
          <h3>历史命中记录（只读保留）</h3>
          <table className="mini-table">
            <thead><tr><th>规则编号</th><th>命中时间</th><th>查询模板</th><th>数据服务</th><th>耗时</th><th>命中</th></tr></thead>
            <tbody>
              {hits.map((hit) => (
                <tr key={String(hit.id)}>
                  <td>{hit['规则编号']}</td>
                  <td>{hit['命中时间']}</td>
                  <td>{hit['查询模板']}</td>
                  <td>{hit['数据服务']}</td>
                  <td>{hit['命中耗时']}ms</td>
                  <td><span className={`status-badge ${hit['是否命中'] ? 'status-ok' : 'status-muted'}`}>{hit['是否命中'] ? '命中' : '未命中'}</span></td>
                </tr>
              ))}
              {hits.length === 0 && <tr><td colSpan={6} className="empty-state">暂无命中记录</td></tr>}
            </tbody>
          </table>
        </article>
      </div>

      <footer className="page-foot">
        <span>共 {total} 条查询缓存管理记录</span>
        {notice && <span className="notice-text">{notice}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
