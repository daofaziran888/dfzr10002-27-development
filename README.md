# 数据分析工作台

面向企业数据分析师与数据产品经理的一站式数据分析工作台。提供指标口径管理、聚合查询、分析实验设计、数据服务发布与应用集成的完整链路。

这是一个前后端分离的管理平台：前端 React + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 React + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 指标口径管理 | `indicator` | 业务指标 | 指标编号、指标名称、计算公式 |
| 分析数据源 | `datasource` | 数据源连接 | 连接编号、连接名称、数据库类型 |
| 聚合查询配置 | `query` | 查询模板 | 模板编号、模板名称、查询语句 |
| 分析实验管理 | `experiment` | 分析实验 | 实验编号、实验名称、分析数据集 |
| 分析数据集 | `dataset` | 数据集 | 数据集编号、数据集名称、来源表名 |
| 可视化图表配置 | `chart` | 图表配置 | 图表编号、图表名称、图表类型 |
| 分析报告管理 | `report` | 分析报告 | 报告编号、报告标题、关联数据集 |
| 数据服务发布 | `service` | 数据服务 | 服务编号、服务名称、输出数据集 |
| 数据看板 | `dashboard` | 看板面板 | 面板编号、面板名称、布局配置 |
| 分析维度管理 | `dimension` | 分析维度 | 维度编号、维度名称、维度表名 |
| 全局筛选器 | `filter` | 筛选器 | 筛选器编号、筛选器名称、关联字段 |
| 定时分析任务 | `schedule` | 定时任务 | 任务编号、任务名称、分析类型 |
| 数据导出配置 | `export` | 导出模板 | 模板编号、模板名称、数据源绑定 |
| 查询缓存管理 | `cache` | 缓存规则 | 规则编号、缓存目标、缓存时间 |
| 分析模型管理 | `model` | 分析模型 | 模型编号、模型名称、模型类型 |
| 分析操作日志 | `log` | 操作日志 | 日志编号、操作类型、操作对象 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 查询缓存管理：依赖失效与预加载回填

主对象是缓存规则（`cache`），缓存目标必须是已登记的分析数据集编号。数据服务只是缓存
规则的下游引用方，它的待刷新标记由缓存规则当前状态统一回算，不允许数据服务自己改写。

- **依赖保存**：`POST /api/cache/{id}/dependencies`，请求体 `values` 带 `上游依赖`
  （数据集 / 查询模板 / 缓存规则）与 `下游引用`（数据服务）。依赖去重、校验目标存在，
  自依赖与环在保存阶段就拒绝；发布时 `POST /api/cache/{id}/publish` 会再校验一次并固化
  一份 `发布依赖快照`。
- **冲突优先级**：同一缓存目标只允许一条已启用规则，共享规则 > 本团队规则 > 其它团队
  规则；同优先级按规则编号小者优先（先发布者保留）。跨团队缓存规则依赖只允许指向共享
  规则。
- **刷新与失效**：`POST /api/cache/refresh`（values 带 `数据集编号`）按依赖层级把下游
  缓存规则置为待刷新，并同步其它业务面的查询模板与数据服务清单
  （`GET /api/query`、`GET /api/service` 的 `待刷新` / `刷新来源` 字段）。
- **预加载回填**：`POST /api/cache/preload` 默认回填最近一次刷新；回填中断时失败起点
  记在刷新记录（`GET /api/cache/runs`）里，再次调用从最早失败节点重试。回填写入新快照，
  旧快照转历史，历史命中记录（`GET /api/cache/hits`）始终保留。
- **依赖图**：`GET /api/cache/graph` 返回节点、边与现存环，前端在查询缓存管理页展示。
- **权限**：身份由请求头 `X-Operator` / `X-Operator-Team` / `X-Operator-Permissions`
  传入（中文按 URL 编码），前端顶部可切换操作人预设。越权团队只能查看；
  `cache:invalidate:edit` 控制失效条件修改，`cache:shared:manage` 控制共享规则的失效
  条件维护，共享规则的其它字段始终只读。

示例：刷新 `DS-001` 后，`CACH-0001` 与依赖它的 `CACH-0004` 按依赖顺序待刷新，
`QUER-0001` / `QUER-0002`、`SERV-0001` / `SERV-0004` 同步显示待刷新；预加载从失败节点
重试后全部恢复；循环依赖在保存或发布前即被拒绝。

领域：数据工程
子方向：数据分析与数据应用
