# 移民案件期限与材料管理

纯Python标准库实现的移民案件期限与材料管理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、法定天数、补件期限和材料完整性和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询（含两阶段转办表与归属办事处列）。
- `src/service.py`：用例编排、权限检查、乐观并发、审计与跨办事处转办。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、失败场景与两阶段转办测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8329
```

默认端口为`8329`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`，归属办事处取`X-Org`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。
- `POST /api/records/{id}/transfers`：第一阶段发起转办，请求体为`{"expected_version":3,"to_office":"office-south"}`，需带转出方`X-Org`。
- `POST /api/transfers/{id}/confirm`：第二阶段接收方确认，需带接收方`X-Org`；重复确认幂等返回同一结果。
- `GET /api/transfers/{id}`：转办单详情。
- `GET /api/records/{id}/transfers`：案件全部转办单（含已作废单据）。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`；转办发起与确认必须提供`X-Org`。

## 跨办事处两阶段转办

- **第一阶段（发起）**：转出方按案件当前修订号发起，系统冻结原补件期限快照（`deadline_day`、`evidence_due_day`等）随单保存。每个案件同时最多一张待确认转办单，数据库唯一部分索引保证两个办事处同时转同一案时只接受先到一单。
- **冻结期**：接收方确认前，双方均不能`submit`或`decide`（返回409）。
- **第二阶段（确认）**：接收方确认后归属在单事务内一次切换（案件内容与修订号不变）；重复确认返回同一结果，不重复切换、不重复计数。
- **等待期间更新**：案件在等待期间又被更新时，旧转办单随该次写入在同一事务作废，接收方确认得到“请转出方重新发起”的409提示；按新修订号重发即可。
- **写入失败**：发起或确认在单事务内失败则整体回滚，原单仍为`pending`，可恢复确认。
- **旧数据回填**：历史案件没有归属时，首次转办发起在同一事务内按转出方办事处回填（不占修订号），并记录`office_backfilled`审计，原有审计记录照常可查。
- **统计**：`GET /api/stats`在原按状态计数之外新增`total`与`by_office`（已接管案件只在当前归属办事处计一次，未归属归入`unassigned`）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突，以及两阶段转办的冻结期、作废重发、并发单飞、写入失败恢复、归属统计和旧数据回填。
