# 课堂反馈闭环

事件溯源的纯 Python 服务端：版本化记录、幂等命令、SQLite 持久化与 JSON API 边界。

## 模型

每条反馈记录包含：负责人 `owner`、有效期 `valid_until`、修订号 `revision`（每次修订 +1）。

状态机：

```
submitted -> acknowledged -> processing -> resolved -> closed
   │             │                │           └────> processing（可返工）
   └─────────────┴────────────────┴──> withdrawn（终态，仅经撤回命令）
```

- 所有命令只追加事件（`feedback_submitted` / `state_transitioned` / `record_withdrawn` / `record_amended`），历史永不改写。
- 撤回不删除记录，只产生一条新的 `record_withdrawn` 事件，可在事件历史中审计。
- 命令可带 `idempotency_key`：相同键 + 相同命令返回当前结果且不重复落事件；相同键复用于不同命令报 409。

## 模块

- `service_09261_004/workflow.py`：领域模型、事件、状态折叠与命令校验（非法顺序抛 `InvalidTransition`）
- `service_09261_004/store.py`：SQLite 只追加事件仓储 + 幂等键表
- `service_09261_004/service.py`：应用服务，命令落库；新实例从事件流完整恢复（含幂等键）
- `service_09261_004/api.py`：JSON API 适配器（`dispatch(service, method, path, body)`）

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/records` | 创建 `{id, owner, valid_until, actor?, idempotency_key?}` |
| POST | `/records/{id}/transitions` | 推进状态 `{new_state, actor}` |
| POST | `/records/{id}/withdrawal` | 撤回 `{actor, reason?}`（只追加） |
| POST | `/records/{id}/amendments` | 修订 `{actor, owner?, valid_until?}`，revision +1 |
| GET | `/records` / `/records/{id}` | 快照（含 `expired` 标记） |
| GET | `/records/{id}/events` | 事件历史 |

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q service_09261_004 tests`
