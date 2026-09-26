"""课堂反馈闭环：非法顺序 / 重复请求 / 数据恢复 自动化测试。"""
import os
import tempfile
import unittest
from datetime import datetime, timezone, timedelta

from service_09261_004 import FeedbackService, SQLiteStore, Workflow
from service_09261_004.workflow import (
    InvalidRequest,
    RecordNotFound,
    DuplicateRecord,
    InvalidTransition,
    ConflictingIdempotencyKey,
)
from service_09261_004.api import dispatch

NOW = datetime(2026, 9, 26, 9, 0, tzinfo=timezone.utc)
FUTURE = (NOW + timedelta(days=7)).isoformat()
LATER = (NOW + timedelta(days=14)).isoformat()


def make_service(path=":memory:"):
    return FeedbackService(SQLiteStore(path))


class TestRecordBasics(unittest.TestCase):
    def test_record_has_owner_validity_and_revision(self):
        svc = make_service()
        rec = svc.create_feedback("fb-1", "张老师", FUTURE, now=NOW)
        self.assertEqual(rec["owner"], "张老师")
        self.assertEqual(rec["valid_until"], FUTURE)
        self.assertEqual(rec["revision"], 1)
        self.assertEqual(rec["state"], "submitted")
        self.assertFalse(rec["expired"])

    def test_past_validity_rejected(self):
        svc = make_service()
        with self.assertRaises(InvalidRequest):
            svc.create_feedback("fb-1", "张老师",
                                (NOW - timedelta(seconds=1)).isoformat(), now=NOW)

    def test_expired_flag_after_validity(self):
        svc = make_service()
        svc.create_feedback("fb-1", "张老师", FUTURE, now=NOW)
        rec = svc.get_record("fb-1", now=NOW + timedelta(days=8))
        self.assertTrue(rec["expired"])

    def test_missing_owner_rejected(self):
        svc = make_service()
        with self.assertRaises(InvalidRequest):
            svc.create_feedback("fb-1", "", FUTURE, now=NOW)


class TestIllegalOrder(unittest.TestCase):
    def setUp(self):
        self.svc = make_service()
        self.svc.create_feedback("fb-1", "张老师", FUTURE, now=NOW)

    def test_skip_state_is_illegal(self):
        # submitted 不能直接跳到 processing / resolved / closed。
        for bad in ("processing", "resolved", "closed"):
            with self.assertRaises(InvalidTransition):
                self.svc.transition("fb-1", bad, "张老师", now=NOW)

    def test_legal_path_then_reverse_is_illegal(self):
        self.svc.transition("fb-1", "acknowledged", "张老师", now=NOW)
        self.svc.transition("fb-1", "processing", "张老师", now=NOW)
        self.svc.transition("fb-1", "resolved", "张老师", now=NOW)
        self.svc.transition("fb-1", "closed", "张老师", now=NOW)
        with self.assertRaises(InvalidTransition):
            self.svc.transition("fb-1", "processing", "张老师", now=NOW)

    def test_withdraw_is_terminal(self):
        self.svc.withdraw("fb-1", "李主任", "误报", now=NOW)
        with self.assertRaises(InvalidTransition):
            self.svc.transition("fb-1", "acknowledged", "张老师", now=NOW)
        with self.assertRaises(InvalidTransition):
            self.svc.withdraw("fb-1", "李主任", now=NOW)

    def test_withdraw_must_use_dedicated_command(self):
        with self.assertRaises(InvalidTransition):
            self.svc.transition("fb-1", "withdrawn", "张老师", now=NOW)

    def test_amend_terminal_record_illegal(self):
        self.svc.withdraw("fb-1", "李主任", now=NOW)
        with self.assertRaises(InvalidTransition):
            self.svc.amend("fb-1", "李主任", owner="王老师", now=NOW)

    def test_unknown_record(self):
        with self.assertRaises(RecordNotFound):
            self.svc.transition("nope", "acknowledged", "张老师", now=NOW)

    def test_amend_requires_a_change(self):
        with self.assertRaises(InvalidRequest):
            self.svc.amend("fb-1", "张老师", now=NOW)


class TestWithdrawalAppendsOnly(unittest.TestCase):
    def test_withdrawal_creates_new_event_and_keeps_history(self):
        svc = make_service()
        svc.create_feedback("fb-1", "张老师", FUTURE, now=NOW)
        n_before = svc.store.event_count()
        svc.withdraw("fb-1", "李主任", "重复提交", now=NOW)
        self.assertEqual(svc.store.event_count(), n_before + 1)

        rec = svc.get_record("fb-1")
        self.assertEqual(rec["state"], "withdrawn")
        self.assertEqual(rec["withdrawn_reason"], "重复提交")
        # 记录仍在，没有被删除。
        self.assertEqual([r["id"] for r in svc.snapshot()], ["fb-1"])

        kinds = [e.kind for e in svc.flow.events]
        self.assertEqual(kinds, ["feedback_submitted", "record_withdrawn"])
        # 事件连续编号、只追加。
        self.assertEqual([e.seq for e in svc.flow.events], [1, 2])


class TestDuplicateRequests(unittest.TestCase):
    def test_repeated_create_with_same_key_is_idempotent(self):
        svc = make_service()
        first = svc.create_feedback("fb-1", "张老师", FUTURE,
                                    idempotency_key="k-1", now=NOW)
        second = svc.create_feedback("fb-1", "张老师", FUTURE,
                                     idempotency_key="k-1", now=NOW)
        self.assertEqual(first, second)
        self.assertEqual(svc.store.event_count(), 1)

    def test_repeated_transition_with_same_key_is_idempotent(self):
        svc = make_service()
        svc.create_feedback("fb-1", "张老师", FUTURE, now=NOW)
        svc.transition("fb-1", "acknowledged", "张老师",
                       idempotency_key="k-2", now=NOW)
        svc.transition("fb-1", "acknowledged", "张老师",
                       idempotency_key="k-2", now=NOW)
        self.assertEqual(svc.get_record("fb-1")["state"], "acknowledged")
        self.assertEqual(svc.store.event_count(), 2)

    def test_same_key_different_request_conflicts(self):
        svc = make_service()
        svc.create_feedback("fb-1", "张老师", FUTURE,
                            idempotency_key="k-9", now=NOW)
        with self.assertRaises(ConflictingIdempotencyKey):
            svc.create_feedback("fb-2", "王老师", FUTURE,
                                idempotency_key="k-9", now=NOW)
        # 冲突请求未产生任何事件。
        self.assertEqual(svc.store.event_count(), 1)

    def test_duplicate_create_without_key_rejected(self):
        svc = make_service()
        svc.create_feedback("fb-1", "张老师", FUTURE, now=NOW)
        with self.assertRaises(DuplicateRecord):
            svc.create_feedback("fb-1", "张老师", FUTURE, now=NOW)

    def test_rejected_command_writes_nothing(self):
        svc = make_service()
        svc.create_feedback("fb-1", "张老师", FUTURE, now=NOW)
        # 非法迁移即使带幂等键，也不得落事件、不得登记键。
        with self.assertRaises(InvalidTransition):
            svc.transition("fb-1", "resolved", "张老师",
                           idempotency_key="k-x", now=NOW)
        self.assertEqual(svc.store.event_count(), 1)
        self.assertEqual(svc.store.load_keys(), {})


class TestRecovery(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        os.remove(self.path)

    def _build_history(self):
        svc = make_service(self.path)
        svc.create_feedback("fb-1", "张老师", FUTURE,
                            idempotency_key="create-1", now=NOW)
        svc.transition("fb-1", "acknowledged", "张老师", now=NOW)
        svc.amend("fb-1", "李主任", owner="王老师", now=NOW)
        svc.withdraw("fb-1", "李主任", "误报",
                     idempotency_key="wd-1", now=NOW)
        return svc

    def test_state_rebuilt_from_event_log(self):
        original = self._build_history()
        expected = original.snapshot(now=NOW)
        event_count = original.store.event_count()

        # 新进程：指向同一个 SQLite 文件，从事件流恢复。
        revived = make_service(self.path)
        self.assertEqual(revived.recover(), event_count)
        self.assertEqual(revived.snapshot(now=NOW), expected)

        rec = revived.get_record("fb-1")
        self.assertEqual(rec["state"], "withdrawn")
        self.assertEqual(rec["owner"], "王老师")
        self.assertEqual(rec["revision"], 2)
        self.assertEqual([e.kind for e in revived.flow.events],
                         ["feedback_submitted", "state_transitioned",
                          "record_amended", "record_withdrawn"])

    def test_idempotency_survives_restart(self):
        self._build_history()
        revived = make_service(self.path)
        before = revived.store.event_count()
        # 重启后重放旧的幂等键，应命中且不追加事件。
        rec = revived.create_feedback(
            "fb-1", "张老师", FUTURE, idempotency_key="create-1", now=NOW)
        self.assertEqual(rec["revision"], 2)
        self.assertEqual(revived.store.event_count(), before)

        # 旧的撤回键重放同样只返回当前状态。
        rec = revived.withdraw("fb-1", "李主任", "误报",
                               idempotency_key="wd-1", now=NOW)
        self.assertEqual(rec["state"], "withdrawn")
        self.assertEqual(revived.store.event_count(), before)

    def test_new_commands_continue_after_recovery(self):
        self._build_history()
        revived = make_service(self.path)
        revived.create_feedback("fb-2", "陈老师", LATER, now=NOW)
        self.assertEqual(revived.store.event_count(), 5)

        again = make_service(self.path)
        self.assertEqual(
            sorted(r["id"] for r in again.snapshot()), ["fb-1", "fb-2"]
        )


class TestRevisionOnAmend(unittest.TestCase):
    def test_each_amend_bumps_revision_and_appends_event(self):
        svc = make_service()
        svc.create_feedback("fb-1", "张老师", FUTURE, now=NOW)
        svc.amend("fb-1", "张老师", owner="王老师", now=NOW)
        self.assertEqual(svc.get_record("fb-1")["revision"], 2)
        svc.amend("fb-1", "王老师", valid_until=LATER, now=NOW)
        self.assertEqual(svc.get_record("fb-1")["revision"], 3)
        self.assertEqual(svc.get_record("fb-1")["valid_until"], LATER)
        kinds = [e.kind for e in svc.flow.events]
        self.assertEqual(kinds.count("record_amended"), 2)


class TestJSONAPI(unittest.TestCase):
    def setUp(self):
        self.svc = make_service()

    def test_dispatch_happy_path_and_errors(self):
        status, body = dispatch(self.svc, "POST", "/records",
                                {"id": "fb-1", "owner": "张老师",
                                 "valid_until": FUTURE})
        self.assertEqual(status, 201)
        self.assertEqual(body["revision"], 1)

        status, body = dispatch(self.svc, "POST", "/records/fb-1/transitions",
                                {"new_state": "processing", "actor": "张老师"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "InvalidTransition")

        status, _ = dispatch(self.svc, "POST", "/records/fb-1/transitions",
                             {"new_state": "acknowledged", "actor": "张老师"})
        self.assertEqual(status, 200)

        status, body = dispatch(self.svc, "POST", "/records",
                                {"id": "fb-1", "owner": "张老师"})
        self.assertEqual(status, 400)

        status, body = dispatch(self.svc, "GET", "/records/missing")
        self.assertEqual(status, 404)

    def test_dispatch_idempotency_and_withdrawal_history(self):
        dispatch(self.svc, "POST", "/records",
                 {"id": "fb-1", "owner": "张老师", "valid_until": FUTURE,
                  "idempotency_key": "api-1"})
        dispatch(self.svc, "POST", "/records",
                 {"id": "fb-1", "owner": "张老师", "valid_until": FUTURE,
                  "idempotency_key": "api-1"})
        self.assertEqual(self.svc.store.event_count(), 1)

        status, _ = dispatch(self.svc, "POST", "/records/fb-1/withdrawal",
                             {"actor": "李主任", "reason": "误报"})
        self.assertEqual(status, 200)
        status, events = dispatch(self.svc, "GET", "/records/fb-1/events")
        self.assertEqual(status, 200)
        self.assertEqual([e["type"] for e in events],
                         ["feedback_submitted", "record_withdrawn"])


if __name__ == "__main__":
    unittest.main()
