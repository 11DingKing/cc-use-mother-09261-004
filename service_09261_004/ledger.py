"""课堂反馈闭环记录簿：只追加事件的版本化台账。"""
from dataclasses import dataclass, asdict, replace
from datetime import date, datetime, timezone
from .store import SQLiteEventStore

REGISTERED, REVISED, WITHDRAWN = "registered", "revised", "withdrawn"
ACTIVE, CLOSED = "active", "withdrawn"


class LedgerError(ValueError):
    """记录簿错误基类。"""


class OrderError(LedgerError):
    """非法顺序：修订号跳号、撤回后仍有后续事件。"""


class DuplicateError(LedgerError):
    """重复请求：记录编号已存在。"""


class NotFoundError(LedgerError):
    """记录不存在。"""


def _text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise LedgerError("%s must be a non-empty string" % field)
    return value.strip()


def _date(value, field):
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError:
        raise LedgerError("invalid %s: %r" % (field, value))


def _period(valid_from, valid_until):
    if valid_from > valid_until:
        raise LedgerError("valid_until before valid_from")


@dataclass(frozen=True)
class FeedbackRecord:
    """反馈记录投影：负责人、有效期、修订号；撤回只是状态，不抹历史。"""
    id: str
    owner: str
    valid_from: str
    valid_until: str
    summary: str
    revision: int
    status: str = ACTIVE
    withdrawn_reason: str = ""
    updated_by: str = ""


class Ledger:
    """课堂反馈记录簿。

    登记 / 修订 / 撤回全部作为新事件追加到事件库；
    记录状态由事件流推导，历史事件永不修改或删除。
    打开已有数据的库请用 Ledger.recover(store)。
    """

    def __init__(self, store=None):
        self.store = store or SQLiteEventStore()
        self.events = []
        self.records = {}
        self.replies = {}

    # ---- 命令 ----

    def register(self, record_id, owner, valid_from, valid_until, summary, actor, key=None):
        """登记新记录，修订号从 1 开始；key 为幂等键。"""
        hit = self._replay(key)
        if hit is not None:
            return hit
        record_id = _text(record_id, "id")
        if record_id in self.records:
            raise DuplicateError("record %r exists" % record_id)
        record = FeedbackRecord(record_id, _text(owner, "owner"),
                                _date(valid_from, "valid_from"), _date(valid_until, "valid_until"),
                                _text(summary, "summary"), 1, updated_by=_text(actor, "actor"))
        _period(record.valid_from, record.valid_until)
        data = {"owner": record.owner, "valid_from": record.valid_from,
                "valid_until": record.valid_until, "summary": record.summary}
        return self._commit(REGISTERED, record, data, record.updated_by, key)

    def revise(self, record_id, revision, actor, owner=None, valid_from=None,
               valid_until=None, summary=None, key=None):
        """修订记录：revision 必须等于当前修订号 +1。"""
        hit = self._replay(key)
        if hit is not None:
            return hit
        current = self._open(record_id)
        self._expect(current, revision)
        data = {}
        if owner is not None:
            data["owner"] = _text(owner, "owner")
        if valid_from is not None:
            data["valid_from"] = _date(valid_from, "valid_from")
        if valid_until is not None:
            data["valid_until"] = _date(valid_until, "valid_until")
        if summary is not None:
            data["summary"] = _text(summary, "summary")
        if not data:
            raise LedgerError("nothing to revise")
        record = replace(current, revision=current.revision + 1,
                         updated_by=_text(actor, "actor"), **data)
        _period(record.valid_from, record.valid_until)
        return self._commit(REVISED, record, data, record.updated_by, key)

    def withdraw(self, record_id, revision, reason, actor, key=None):
        """撤回记录：只追加撤回事件，记录与全部历史保留。"""
        hit = self._replay(key)
        if hit is not None:
            return hit
        current = self._open(record_id)
        self._expect(current, revision)
        record = replace(current, status=CLOSED, withdrawn_reason=_text(reason, "reason"),
                         revision=current.revision + 1, updated_by=_text(actor, "actor"))
        return self._commit(WITHDRAWN, record, {"reason": record.withdrawn_reason},
                            record.updated_by, key)

    # ---- 查询 ----

    def get(self, record_id):
        try:
            return self.records[record_id]
        except KeyError:
            raise NotFoundError("record %r not found" % record_id)

    def snapshot(self):
        return [asdict(self.records[k]) for k in sorted(self.records)]

    def history(self, record_id):
        """某条记录的全部事件，按全局顺序；撤回不抹除历史。"""
        return [dict(e) for e in self.events if e["record_id"] == record_id]

    # ---- 数据恢复 ----

    @classmethod
    def recover(cls, store):
        """重放事件库重建记录簿；事件流有缺口或乱序即报错。"""
        ledger = cls(store)
        seq = 0
        for event in store.load():
            seq += 1
            if event["seq"] != seq:
                raise OrderError("event seq gap at %d" % seq)
            ledger._apply(event)
        return ledger

    # ---- 内部 ----

    def _commit(self, kind, record, data, actor, key):
        event = {"record_id": record.id, "kind": kind, "revision": record.revision,
                 "actor": actor, "data": data, "idem_key": key,
                 "at": datetime.now(timezone.utc).isoformat()}
        event["seq"] = self.store.append(event, key)
        self.events.append(event)
        self.records[record.id] = record
        if key is not None:
            self.replies[key] = record
        return record

    def _apply(self, event):
        """重放单个事件：每条记录内修订号必须连续，撤回即终态。"""
        record_id, kind = event["record_id"], event["kind"]
        current = self.records.get(record_id)
        if current is None:
            if kind != REGISTERED or event["revision"] != 1:
                raise OrderError("first event of %r must be registered@1" % record_id)
        else:
            if current.status == CLOSED:
                raise OrderError("event after withdrawal of %r" % record_id)
            if event["revision"] != current.revision + 1:
                raise OrderError("revision gap in %r" % record_id)
        data = event["data"]
        if kind == REGISTERED:
            record = FeedbackRecord(record_id, data["owner"], data["valid_from"],
                                    data["valid_until"], data["summary"], 1,
                                    updated_by=event["actor"])
        elif kind == REVISED:
            record = replace(current,
                             owner=data.get("owner", current.owner),
                             valid_from=data.get("valid_from", current.valid_from),
                             valid_until=data.get("valid_until", current.valid_until),
                             summary=data.get("summary", current.summary),
                             revision=event["revision"], updated_by=event["actor"])
        elif kind == WITHDRAWN:
            record = replace(current, status=CLOSED, withdrawn_reason=data["reason"],
                             revision=event["revision"], updated_by=event["actor"])
        else:
            raise OrderError("unknown event kind %r" % kind)
        _period(record.valid_from, record.valid_until)
        self.events.append(dict(event))
        self.records[record_id] = record
        if event.get("idem_key") is not None:
            self.replies[event["idem_key"]] = record

    def _replay(self, key):
        if key is not None:
            return self.replies.get(key)
        return None

    def _open(self, record_id):
        current = self.records.get(record_id)
        if current is None:
            raise NotFoundError("record %r not found" % record_id)
        if current.status == CLOSED:
            raise OrderError("record %r withdrawn" % record_id)
        return current

    @staticmethod
    def _expect(current, revision):
        if revision != current.revision + 1:
            raise OrderError("revision %r out of order, expect %d"
                             % (revision, current.revision + 1))
