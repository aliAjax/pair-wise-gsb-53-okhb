# 移民案件期限与材料管理

纯Python标准库实现的移民案件期限与材料管理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、法定天数、补件期限和材料完整性和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算和失败场景测试。

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
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。
- `POST /api/records/{id}/transfers`：发起跨办事处转办（两阶段第一阶段），请求体为`{"expected_version":3,"data":{"to_org":"office-b"}}`，需`case_officer`/`supervisor`且`X-Org`为当前归属办事处。
- `POST /api/transfers/{id}/confirm`：接收方（`X-Org`等于转办单`to_org`）确认转办，归属一次性切换。
- `GET /api/records/{id}/transfers`：案件转办单列表。
- `GET /api/transfers/{id}`：转办单详情。
- `GET /api/office-stats`：按当前归属办事处统计案件数（已接管案件不重复计数）。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 跨办事处两阶段转办

1. **发起**：转出方带当前修订号（`expected_version`）发起，转办单快照原补件期限（`evidence_due_day`）和修订号，状态为`pending`。
2. **等待冻结**：确认前`submit`/`decide`对双方均返回409；等待期间案件发生任何更新，转办单在同一事务内被置为`voided`，确认时返回409并提示重新发起。
3. **确认**：仅接收方可确认；确认后归属在单个事务内一次切换、修订号+1。重复确认返回首次确认的同一结果（幂等）。
4. **并发**：同一案件同时只能有一单`pending`（数据库唯一索引保证先到先得），后到的一单返回409。
5. **失败恢复**：确认写入失败时事务回滚，原单仍为`pending`，可重新确认。
6. **旧数据**：旧库首次启动自动为`records`补`office`列；旧记录`office`为空，首次发起转办时按发起方办事处回填（不改变修订号），历史审计记录照常可查。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突，以及两阶段转办（冻结、作废重发、幂等确认、并发先到先得、失败恢复、计数与旧数据回填）。
