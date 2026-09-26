"""应用服务：把内存工作流与 SQLite 事件仓储绑定。

每次成功命令产生的事件在返回前落库；新建实例时从事件流完整恢复，
包括幂等键登记，因此重复请求在重启后依旧被识别。
"""
from __future__ import annotations

from .store import SQLiteStore
from .workflow import Workflow


class FeedbackService:
    def __init__(self, store=None):
        self.store = store or SQLiteStore()
        self.flow = Workflow()
        self.recover()

    def recover(self):
        """从持久化事件重建内存状态（数据恢复）。"""
        events = self.store.load_events()
        keys = self.store.load_keys()
        self.flow.load(events, keys)
        return len(events)

    def _submit(self, command, key, signature):
        """执行命令；只有真正产生新事件时才落库（幂等命中不重复写）。"""
        before = len(self.flow.events)
        result = command()
        if len(self.flow.events) > before:
            self.store.append(
                self.flow.events[-1],
                idempotency_key=key,
                signature=signature,
            )
        return result

    def create_feedback(self, record_id, owner, valid_until, actor=None,
                        idempotency_key=None, now=None):
        return self._submit(
            lambda: self.flow.create(
                record_id, owner, valid_until, actor=actor,
                idempotency_key=idempotency_key, now=now,
            ),
            idempotency_key,
            ("create", record_id),
        )

    def transition(self, record_id, new_state, actor,
                   idempotency_key=None, now=None):
        return self._submit(
            lambda: self.flow.transition(
                record_id, new_state, actor,
                idempotency_key=idempotency_key, now=now,
            ),
            idempotency_key,
            ("transition", record_id, new_state),
        )

    def withdraw(self, record_id, actor, reason="",
                 idempotency_key=None, now=None):
        return self._submit(
            lambda: self.flow.withdraw(
                record_id, actor, reason=reason,
                idempotency_key=idempotency_key, now=now,
            ),
            idempotency_key,
            ("withdraw", record_id),
        )

    def amend(self, record_id, actor, owner=None, valid_until=None,
              idempotency_key=None, now=None):
        return self._submit(
            lambda: self.flow.amend(
                record_id, actor, owner=owner, valid_until=valid_until,
                idempotency_key=idempotency_key, now=now,
            ),
            idempotency_key,
            ("amend", record_id, owner, valid_until),
        )

    def snapshot(self, now=None):
        return self.flow.snapshot(now)

    def get_record(self, record_id, now=None):
        return self.flow.snapshot_record(record_id, now)
