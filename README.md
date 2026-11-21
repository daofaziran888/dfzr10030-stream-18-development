# 管道工坊 · 实时数仓与流式管道平台

面向实时数据接入、流式转换、变更捕获、窗口聚合、状态管理、断流恢复与多维数据集下发的实时数仓与流式管道开发平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
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
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 源端绑定 | `source_binding` | 数据源端 | 源端编号、源端类型、连接地址 |
| 变更捕获 | `cdc_task` | CDC任务 | 任务编号、源数据库、监控表 |
| 流式转换 | `stream_transform` | 转换算子 | 算子编号、输入流、输出流 |
| 窗口聚合 | `window_agg` | 时间窗口 | 窗口编号、聚合指标、窗口类型 |
| 状态管理 | `state_store` | 算子状态 | 状态编号、关联算子、存储后端 |
| 规则引擎 | `rule_engine` | 业务规则 | 规则编号、规则条件、触发动作 |
| 水位管理 | `watermark_mgr` | 事件水位 | 水位编号、关联流、水位类型 |
| 数据落盘 | `sink_writer` | 输出槽位 | 槽位编号、输出类型、目标地址 |
| 背压控制 | `backpressure` | 背压策略 | 策略编号、关联算子、队列上限 |
| 断流恢复 | `checkpoint` | 检查点 | 检查点编号、关联流水线、位点时间 |
| 流式质检 | `data_quality` | 质检规则 | 规则编号、关联流、检查项 |
| Schema演进 | `schema_evolve` | 结构变更 | 变更编号、关联流、变更类型 |
| 管道编排 | `pipeline_orch` | 管道DAG | 管道编号、管道名称、节点列表 |
| 去重管理 | `dedup_mgr` | 去重策略 | 策略编号、关联流、去重键 |
| 指标计算 | `metric_calc` | 实时指标 | 指标编号、指标名称、计算公式 |
| 事件路由 | `event_router` | 路由规则 | 规则编号、事件类型、匹配条件 |
| 历史回放 | `replay_batch` | 回放任务 | 任务编号、数据源、起始时间 |
| 分组分区 | `grp_partition` | 分组策略 | 策略编号、关联流、分区键 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 去重换键迁移

`dedup_mgr` 页面下半部分是换键迁移工作台，把「旧订单键 -> 新组合键」的一次迁移
拆成可回查的批次台账：

- 批次登记时声明旧键字段、新组合键定义、旧批次范围与来源授权；
- 核对时逐条判定键口径（旧键 / 双键 / 新键），同一事件标识或同一事件指纹只保留
  一个有效归属；来源不在授权内的按「跨来源修改分区」拒绝并退回源端；
- 旧键把多笔事件压成一笔、新组合键把它们分开，且分区要切换而旧批次尚未确认时，
  整组标记为有效归属冲突，确认旧批次后重算即可解除；
- 同时带两种键、且旧分区与目标分区不同的记录组成「双键分区迁移差额」，移交后由
  `grp_partition` 消费：按事件标识关联目标归属、回写分区切换清单，无权接管的记录
  退回源端；已交付批次保持只读，不改动其结论；
- 批次交付后仍可按旧键回查事件台账与两代键对账，迁移中断后同样可用。

接口：

```text
GET  /api/dedup_mgr/migrations                 批次列表
POST /api/dedup_mgr/migrations                 登记批次
GET  /api/dedup_mgr/migrations/{id}            批次明细（摘要 / 事件 / 差额 / 接管结论）
POST /api/dedup_mgr/migrations/{id}/events     登记待核对事件
POST /api/dedup_mgr/migrations/{id}/actions    开始核对 / 确认旧批次 / 移交差额 / 中断迁移 / 继续迁移 / 交付批次
GET  /api/dedup_mgr/migrations/{id}/legacy     按旧键只读回查
GET  /api/dedup_mgr/migrations/{id}/reconciliation  两代键对账
POST /api/grp_partition/{group_id}/migrations/consume  消费差额并回写切换清单
GET  /api/grp_partition/switch-entries          分区切换清单
```

领域：数据工程
子方向：数仓与数据湖
