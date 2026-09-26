"""课堂反馈记录簿测试：非法顺序、重复请求、数据恢复。"""
import os
import tempfile
import unittest

from service_09261_004.api import dispatch_ledger
from service_09261_004.ledger import (Ledger, DuplicateError, LedgerError,
                                      NotFoundError, OrderError)
from service_09261_004.store import SQLiteEventStore

OWNER_A, OWNER_B = "王老师", "李老师"
FROM, UNTIL = "2026-09-01", "2026-12-31"


def fresh(summary="课堂反馈"):
    ledger = Ledger()
    ledger.register("r1", OWNER_A, FROM, UNTIL, summary, "admin")
    return ledger


class TestOrdering(unittest.TestCase):
    """非法顺序：修订号跳号、撤回后续写、非法有效期。"""

    def test_revision_skip_rejected(self):
        ledger = fresh()
        with self.assertRaises(OrderError):
            ledger.revise("r1", 3, "admin", summary="跳过 2 号")
        with self.assertRaises(OrderError):
            ledger.withdraw("r1", 5, "跳号撤回", "admin")

    def test_stale_revision_rejected(self):
        ledger = fresh()
        ledger.revise("r1", 2, "admin", summary="第二次")
        with self.assertRaises(OrderError):
            ledger.revise("r1", 2, "admin", summary="旧修订号重放")

    def test_withdraw_is_terminal_but_keeps_history(self):
        ledger = fresh()
        ledger.revise("r1", 2, "admin", summary="补充细节")
        ledger.withdraw("r1", 3, "学生撤课", "admin")
        with self.assertRaises(OrderError):
            ledger.revise("r1", 4, "admin", summary="撤回后修订")
        with self.assertRaises(OrderError):
            ledger.withdraw("r1", 4, "再次撤回", "admin")
        record = ledger.get("r1")
        self.assertEqual(record.status, "withdrawn")
        self.assertEqual(record.revision, 3)
        kinds = [e["kind"] for e in ledger.history("r1")]
        self.assertEqual(kinds, ["registered", "revised", "withdrawn"])

    def test_duplicate_record_rejected(self):
        ledger = fresh()
        with self.assertRaises(DuplicateError):
            ledger.register("r1", OWNER_B, FROM, UNTIL, "重复登记", "admin")

    def test_unknown_record(self):
        ledger = fresh()
        with self.assertRaises(NotFoundError):
            ledger.revise("ghost", 1, "admin", summary="x")
        with self.assertRaises(NotFoundError):
            ledger.withdraw("ghost", 1, "x", "admin")
        with self.assertRaises(NotFoundError):
            ledger.get("ghost")

    def test_invalid_period(self):
        ledger = fresh()
        with self.assertRaises(LedgerError):
            ledger.register("r2", OWNER_A, UNTIL, FROM, "日期颠倒", "admin")
        with self.assertRaises(LedgerError):
            ledger.register("r3", OWNER_A, "not-a-date", UNTIL, "坏日期", "admin")
        with self.assertRaises(LedgerError):
            ledger.revise("r1", 2, "admin", valid_until="2026-08-01")

    def test_empty_revision_rejected(self):
        ledger = fresh()
        with self.assertRaises(LedgerError):
            ledger.revise("r1", 2, "admin")


class TestIdempotency(unittest.TestCase):
    """重复请求：同一幂等键只产生一个事件，返回首次结果。"""

    def test_register_twice_same_key(self):
        ledger = Ledger()
        a = ledger.register("r1", OWNER_A, FROM, UNTIL, "作业量", "admin", key="k-1")
        b = ledger.register("r1", OWNER_A, FROM, UNTIL, "作业量", "admin", key="k-1")
        self.assertEqual(a, b)
        self.assertEqual(ledger.get("r1").revision, 1)
        self.assertEqual(len(ledger.history("r1")), 1)

    def test_same_key_different_payload_returns_first(self):
        ledger = Ledger()
        a = ledger.register("r1", OWNER_A, FROM, UNTIL, "作业量", "admin", key="k-1")
        b = ledger.register("r2", OWNER_B, FROM, UNTIL, "另一条", "admin", key="k-1")
        self.assertEqual(a, b)
        with self.assertRaises(NotFoundError):
            ledger.get("r2")

    def test_revise_twice_same_key(self):
        ledger = fresh()
        a = ledger.revise("r1", 2, "admin", summary="新表述", key="k-2")
        b = ledger.revise("r1", 2, "admin", summary="新表述", key="k-2")
        self.assertEqual(a, b)
        self.assertEqual(ledger.get("r1").revision, 2)
        self.assertEqual(len(ledger.history("r1")), 2)

    def test_withdraw_twice_same_key(self):
        ledger = fresh()
        a = ledger.withdraw("r1", 2, "误录", "admin", key="k-3")
        b = ledger.withdraw("r1", 2, "误录", "admin", key="k-3")
        self.assertEqual(a, b)
        self.assertEqual(len(ledger.history("r1")), 2)

    def test_different_keys_both_apply(self):
        ledger = fresh()
        ledger.revise("r1", 2, "admin", summary="甲", key="k-a")
        ledger.revise("r1", 3, "admin", summary="乙", key="k-b")
        record = ledger.get("r1")
        self.assertEqual((record.revision, record.summary), (3, "乙"))


class TestRecovery(unittest.TestCase):
    """数据恢复：重放事件流重建状态，损坏的事件流要报错。"""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        os.unlink(self.path)
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))

    def build_origin(self):
        ledger = Ledger(SQLiteEventStore(self.path))
        ledger.register("r1", OWNER_A, FROM, UNTIL, "作业量", "admin", key="k-1")
        ledger.revise("r1", 2, "admin", owner=OWNER_B, key="k-2")
        ledger.register("r2", OWNER_A, "2026-09-15", "2026-10-15", "课堂互动", "admin")
        ledger.withdraw("r2", 2, "重复工单", "admin", key="k-3")
        return ledger

    def test_recover_rebuilds_same_state(self):
        origin = self.build_origin()
        restored = Ledger.recover(SQLiteEventStore(self.path))
        self.assertEqual(restored.snapshot(), origin.snapshot())
        self.assertEqual(restored.get("r1").owner, OWNER_B)
        self.assertEqual(restored.get("r2").status, "withdrawn")
        self.assertEqual([e["kind"] for e in restored.history("r2")],
                         ["registered", "withdrawn"])

    def test_recover_keeps_idempotency(self):
        self.build_origin()
        restored = Ledger.recover(SQLiteEventStore(self.path))
        again = restored.register("r1", OWNER_A, FROM, UNTIL, "作业量", "admin", key="k-1")
        self.assertEqual((again.revision, again.owner), (1, OWNER_A))  # 首次登记时的回执
        self.assertEqual(restored.get("r1").revision, 2)               # 当前状态不受影响
        self.assertEqual(len(restored.events), 4)                      # 没有追加新事件

    def test_recover_then_continue(self):
        self.build_origin()
        restored = Ledger.recover(SQLiteEventStore(self.path))
        record = restored.revise("r1", 3, "admin", summary="恢复后继续")
        self.assertEqual(record.revision, 3)
        reopened = Ledger.recover(SQLiteEventStore(self.path))
        self.assertEqual(reopened.get("r1").summary, "恢复后继续")

    def test_recover_detects_revision_gap(self):
        store = SQLiteEventStore(self.path)
        Ledger(store).register("r1", OWNER_A, FROM, UNTIL, "s", "admin")
        store.append({"record_id": "r1", "kind": "revised", "revision": 3,
                      "actor": "admin", "data": {"summary": "损坏"},
                      "at": "2026-09-26T00:00:00+00:00"})
        with self.assertRaises(OrderError):
            Ledger.recover(SQLiteEventStore(self.path))

    def test_recover_detects_event_after_withdrawal(self):
        store = SQLiteEventStore(self.path)
        ledger = Ledger(store)
        ledger.register("r1", OWNER_A, FROM, UNTIL, "s", "admin")
        ledger.withdraw("r1", 2, "误录", "admin")
        store.append({"record_id": "r1", "kind": "revised", "revision": 3,
                      "actor": "admin", "data": {"summary": "损坏"},
                      "at": "2026-09-26T00:00:00+00:00"})
        with self.assertRaises(OrderError):
            Ledger.recover(SQLiteEventStore(self.path))

    def test_recover_detects_seq_gap(self):
        store = SQLiteEventStore(self.path)
        ledger = Ledger(store)
        ledger.register("r1", OWNER_A, FROM, UNTIL, "s", "admin")
        ledger.revise("r1", 2, "admin", summary="二")
        ledger.revise("r1", 3, "admin", summary="三")
        store.db.execute("DELETE FROM events WHERE seq=2")
        store.db.commit()
        with self.assertRaises(OrderError):
            Ledger.recover(SQLiteEventStore(self.path))

    def test_recover_empty_store(self):
        self.assertEqual(Ledger.recover(SQLiteEventStore(self.path)).snapshot(), [])


class TestApi(unittest.TestCase):
    """JSON 边界：路由、状态码与错误映射。"""

    def setUp(self):
        self.ledger = Ledger()

    def test_full_cycle(self):
        status, body = dispatch_ledger(self.ledger, "POST", "/records",
                                       {"id": "r1", "owner": OWNER_A, "valid_from": FROM,
                                        "valid_until": UNTIL, "summary": "作业量",
                                        "actor": "admin", "idempotency_key": "k-1"})
        self.assertEqual((status, body["revision"]), (201, 1))
        status, body = dispatch_ledger(self.ledger, "POST", "/records",
                                       {"id": "r1", "owner": OWNER_A, "valid_from": FROM,
                                        "valid_until": UNTIL, "summary": "作业量",
                                        "actor": "admin", "idempotency_key": "k-1"})
        self.assertEqual(status, 201)
        self.assertEqual(len(self.ledger.history("r1")), 1)
        status, body = dispatch_ledger(self.ledger, "POST", "/records/r1/revisions",
                                       {"revision": 2, "actor": "admin", "owner": OWNER_B})
        self.assertEqual((status, body["owner"]), (200, OWNER_B))
        status, body = dispatch_ledger(self.ledger, "POST", "/records/r1/withdrawal",
                                       {"revision": 3, "reason": "误录", "actor": "admin"})
        self.assertEqual((status, body["status"]), (200, "withdrawn"))
        status, body = dispatch_ledger(self.ledger, "GET", "/records/r1")
        self.assertEqual((status, body["revision"]), (200, 3))
        status, body = dispatch_ledger(self.ledger, "GET", "/records")
        self.assertEqual((status, len(body)), (200, 1))

    def test_error_mapping(self):
        dispatch_ledger(self.ledger, "POST", "/records",
                        {"id": "r1", "owner": OWNER_A, "valid_from": FROM,
                         "valid_until": UNTIL, "summary": "s", "actor": "admin"})
        status, _ = dispatch_ledger(self.ledger, "POST", "/records/r1/revisions",
                                    {"revision": 9, "actor": "admin", "summary": "跳号"})
        self.assertEqual(status, 409)
        status, _ = dispatch_ledger(self.ledger, "POST", "/records/ghost/revisions",
                                    {"revision": 1, "actor": "admin", "summary": "x"})
        self.assertEqual(status, 404)
        status, _ = dispatch_ledger(self.ledger, "POST", "/records", {"id": "r2"})
        self.assertEqual(status, 400)
        status, _ = dispatch_ledger(self.ledger, "GET", "/nope")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
