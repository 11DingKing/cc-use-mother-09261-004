# 课堂反馈闭环

纯 Python 服务端基础项目，提供版本化状态、幂等命令、SQLite 持久化和 JSON API 边界。

## 课堂反馈记录簿（service_09261_004.ledger）

- 每条记录包含负责人 owner、有效期 valid_from/valid_until 和修订号 revision。
- 只追加事件：登记 / 修订 / 撤回都写入 SQLiteEventStore，历史永不修改；撤回只产生新事件，记录保留为 withdrawn 状态。
- 非法顺序防护：修订号必须连续（当前值 +1），撤回后的记录拒绝任何后续事件，有效期起止必须合法。
- 幂等命令：携带 idempotency_key 的重复请求返回首次结果，不产生新事件。
- 数据恢复：Ledger.recover(store) 重放事件流重建全部状态，并校验事件序号与修订号完整，损坏即报 OrderError。

```python
from service_09261_004 import Ledger, SQLiteEventStore

ledger = Ledger(SQLiteEventStore("ledger.sqlite3"))
ledger.register("r1", "王老师", "2026-09-01", "2026-12-31", "作业量反馈", "admin", key="k-1")
ledger.revise("r1", 2, "admin", owner="李老师")
ledger.withdraw("r1", 3, "误录", "admin")
restored = Ledger.recover(SQLiteEventStore("ledger.sqlite3"))  # 数据恢复
```

JSON 边界见 api.dispatch_ledger：POST /records、POST /records/{id}/revisions、POST /records/{id}/withdrawal、GET /records[/{id}]。

测试命令：python3 -m unittest discover -s tests -v

编译命令：python3 -m compileall -q service_09261_004 tests
