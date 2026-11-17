# 日切放行

> 遵守 `ops/底座生成前必读_效率与技术栈.md`。FastAPI + Vue 3 + SQLite，交付须 docker compose 一键起；库用 SQLite，不挂外置 Postgres。  
> **禁止同构换皮**：本仓主链路、表结构、接口与页面均独立，不得与其它仓共用同一套 `/api/items` 壳。

## 骨架一句话

业务日分区切片登记后裁决完整度，完整才放行下游，并与消费水位对账

## 基本信息

| 项 | 值 |
|----|-----|
| 产品名（首页） | 日切放行 |
| 目录 | `04-slicegate` |
| 领域建议 | 数仓与数据湖 |
| 接口 | http://127.0.0.1:10623 |
| 库 | `data/app.db` |
| 账号 | admin / 123456 |

## 为何能出多题

迟到切片、重切、跨分区依赖、部分完整冻结、消费水位回拨

## 避开

别做成作业拓扑编排换皮；别做成纯水位大盘

## 互斥切口候选

- 迟到切片整批拒收 vs 进待补齐冻结区
- 重切失败整笔回滚 vs 保留已完整分区
- 放行与水位同步闸 vs 异步放行后纠偏

## 启动（一键）

`powershell
cd d:\document\cccodex\workspace\slicegate\slicegate-13
docker compose up -d --build
`

浏览器：http://127.0.0.1:10623/  
健康检查：GET http://127.0.0.1:10623/api/health

本机开发（可选）：

`powershell
python -m pip install -r requirements.txt
python -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 10623
python -m pytest -q
`

## 完整度豁免审批闭环

- 豁免策略册 `exemption_policy`：按业务日唯一，口径限甲、乙单独一路，首落笔锁定、当天不许再换；改选/混路一律 409 并记「豁免口径偏了」。
- 甲处置：豁免通过只代表本趟视为完整，**仍须走 `/api/slices/{id}/release` 放行入口**；审批接口不改 `release_status`，禁止审批通过顺带写成已放行。
- 乙处置：豁免通过只开放 `/api/slices/{id}/exemption-preview` 降级只读预览（不写放行态、不写水位）；正式成功放行连同水位在放行入口仍硬拒绝，须另开 `/api/slices/{id}/backfill-order` 补齐单。
- 未审批/被拒豁免不得当成完整；策略册与豁免单口径不一致（豁免口径偏了）按硬拒绝处理。
- 两名值班同时落笔：所有豁免写操作经 `BEGIN IMMEDIATE` 串行互斥，后手显式返回「豁免争用」并落审计，禁止悄悄写成成功。
- 四方可核：豁免列表 `/api/exemptions`、审批详情 `/api/exemptions/{id}`、放行闸读数 `/api/gate-readings`、策略旁注 `/api/exemption-events` 共用同一裁决函数 `gate_verdict`，读数一致。
- 登录、既有文件库 `data/app.db` 沿用；新表 `CREATE TABLE IF NOT EXISTS` 增量迁移，旧数据不动。
