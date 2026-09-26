"""课堂反馈闭环：事件溯源工作流。

规则：
- 每条反馈记录有负责人 ``owner``、有效期 ``valid_until`` 与修订号 ``revision``。
- 所有命令都只追加事件；撤回产生 ``RecordWithdrawn`` 事件，历史永不删除或改写。
- 当前状态由完整事件流回放得到，可随时从持久化的事件中恢复。
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone

# 状态：submitted -> acknowledged -> processing -> resolved -> closed
# 任一生效状态可 -> withdrawn（终态）。
ALLOWED_TRANSITIONS = {
    "submitted": {"acknowledged", "withdrawn"},
    "acknowledged": {"processing", "withdrawn"},
    "processing": {"resolved", "withdrawn"},
    "resolved": {"closed", "processing"},
    "closed": set(),
    "withdrawn": set(),
}

TERMINAL_STATES = {"closed", "withdrawn"}


class WorkflowError(Exception):
    """业务错误基类。"""


class InvalidRequest(WorkflowError):
    """请求参数非法（400）。"""


class RecordNotFound(WorkflowError):
    """反馈记录不存在（404）。"""


class DuplicateRecord(WorkflowError):
    """反馈记录 ID 重复（409）。"""


class InvalidTransition(WorkflowError):
    """状态迁移顺序非法（409）。"""


class ConflictingIdempotencyKey(WorkflowError):
    """幂等键被用于不同请求（409）。"""


def _now(now=None):
    return (now or datetime.now(timezone.utc)).isoformat()


def _parse_dt(value, field_name):
    if not isinstance(value, str) or not value:
        raise InvalidRequest("%s required" % field_name)
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise InvalidRequest("bad %s: %s" % (field_name, value)) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# 事件：只追加，不可变。
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Event:
    kind = "event"
    record_id: str = ""
    actor: str = ""
    timestamp: str = ""
    seq: int = 0


@dataclass(frozen=True)
class FeedbackSubmitted(Event):
    kind = "feedback_submitted"
    owner: str = ""
    valid_until: str = ""


@dataclass(frozen=True)
class StateTransitioned(Event):
    kind = "state_transitioned"
    old_state: str = ""
    new_state: str = ""


@dataclass(frozen=True)
class RecordWithdrawn(Event):
    kind = "record_withdrawn"
    reason: str = ""


@dataclass(frozen=True)
class RecordAmended(Event):
    kind = "record_amended"
    previous_owner: str = ""
    new_owner: str = None
    previous_valid_until: str = ""
    new_valid_until: str = None


_EVENT_TYPES = {
    cls.kind: cls
    for cls in (
        FeedbackSubmitted,
        StateTransitioned,
        RecordWithdrawn,
        RecordAmended,
    )
}


def event_to_dict(event):
    data = asdict(event)
    data["type"] = event.kind
    return data


def event_from_dict(data):
    data = dict(data)
    kind = data.pop("type")
    cls = _EVENT_TYPES.get(kind)
    if cls is None:
        raise InvalidRequest("unknown event type: %s" % kind)
    fields = {f for f in cls.__dataclass_fields__}
    return cls(**{k: v for k, v in data.items() if k in fields})


# ---------------------------------------------------------------------------
# 记录：由事件流折叠出的当前状态。
# ---------------------------------------------------------------------------
@dataclass
class Record:
    id: str
    owner: str
    valid_until: str
    state: str = "submitted"
    revision: int = 1
    created_at: str = ""
    updated_at: str = ""
    withdrawn_reason: str = None

    def is_expired(self, now=None):
        return _parse_dt(self.valid_until, "valid_until") <= (now or datetime.now(timezone.utc))

    def to_dict(self, now=None):
        data = asdict(self)
        data["expired"] = self.is_expired(now)
        return data


class Workflow:
    """纯内存的命令处理与事件折叠；持久化由外层仓储负责。"""

    def __init__(self):
        self._records = {}
        self._events = []
        # 幂等键 -> (record_id, 命令签名)
        self._keys = {}

    # -- 回放 ---------------------------------------------------------------
    def load(self, events, keys=None):
        """用事件流（与已登记的幂等键）重建当前状态。"""
        self._records = {}
        self._events = []
        self._keys = dict(keys or {})
        for event in events:
            self._apply(event)
            self._events.append(event)

    @property
    def events(self):
        return list(self._events)

    def _apply(self, event):
        rid = event.record_id
        if isinstance(event, FeedbackSubmitted):
            self._records[rid] = Record(
                id=rid,
                owner=event.owner,
                valid_until=event.valid_until,
                created_at=event.timestamp,
                updated_at=event.timestamp,
            )
            return
        rec = self._records.get(rid)
        if rec is None:
            raise InvalidRequest("event for unknown record: %s" % rid)
        rec.updated_at = event.timestamp
        if isinstance(event, StateTransitioned):
            rec.state = event.new_state
        elif isinstance(event, RecordWithdrawn):
            rec.state = "withdrawn"
            rec.withdrawn_reason = event.reason
        elif isinstance(event, RecordAmended):
            if event.new_owner is not None:
                rec.owner = event.new_owner
            if event.new_valid_until is not None:
                rec.valid_until = event.new_valid_until
            rec.revision += 1

    def _emit(self, event, key=None, signature=None):
        seq = len(self._events) + 1
        event = type(event)(**{**asdict(event), "seq": seq})
        self._apply(event)
        self._events.append(event)
        if key:
            self._keys[key] = (event.record_id, signature)
        return event

    def _replay_key(self, key, signature):
        """命中幂等键返回 record_id；键被复用给不同命令则报错。"""
        if not key:
            return None
        hit = self._keys.get(key)
        if hit is None:
            return None
        record_id, original = hit
        if original != signature:
            raise ConflictingIdempotencyKey("idempotency key reused for a different request")
        return record_id

    # -- 命令 ---------------------------------------------------------------
    def create(self, record_id, owner, valid_until, actor=None,
               idempotency_key=None, now=None):
        signature = ("create", record_id)
        hit = self._replay_key(idempotency_key, signature)
        if hit is not None:
            return self.snapshot_record(hit)

        if not record_id or not isinstance(record_id, str):
            raise InvalidRequest("record_id required")
        if record_id in self._records:
            raise DuplicateRecord("record already exists: %s" % record_id)
        if not owner or not isinstance(owner, str):
            raise InvalidRequest("owner required")
        expires = _parse_dt(valid_until, "valid_until")
        if expires <= (now or datetime.now(timezone.utc)):
            raise InvalidRequest("valid_until must be in the future")

        self._emit(
            FeedbackSubmitted(
                record_id=record_id,
                actor=actor or owner,
                timestamp=_now(now),
                owner=owner,
                valid_until=expires.isoformat(),
            ),
            key=idempotency_key,
            signature=signature,
        )
        return self.snapshot_record(record_id)

    def transition(self, record_id, new_state, actor,
                   idempotency_key=None, now=None):
        rec = self._require(record_id)
        if new_state == "withdrawn":
            raise InvalidTransition("withdrawal must go through withdraw()")
        signature = ("transition", record_id, new_state)
        hit = self._replay_key(idempotency_key, signature)
        if hit is not None:
            return self.snapshot_record(hit)
        if not actor:
            raise InvalidRequest("actor required")
        if new_state not in ALLOWED_TRANSITIONS.get(rec.state, set()):
            raise InvalidTransition(
                "cannot move %s from %s" % (new_state, rec.state)
            )
        self._emit(
            StateTransitioned(
                record_id=record_id,
                actor=actor,
                timestamp=_now(now),
                old_state=rec.state,
                new_state=new_state,
            ),
            key=idempotency_key,
            signature=signature,
        )
        return self.snapshot_record(record_id)

    def withdraw(self, record_id, actor, reason="",
                 idempotency_key=None, now=None):
        """撤回：不删除记录，只追加一条 ``RecordWithdrawn`` 事件。"""
        rec = self._require(record_id)
        signature = ("withdraw", record_id)
        hit = self._replay_key(idempotency_key, signature)
        if hit is not None:
            return self.snapshot_record(hit)
        if not actor:
            raise InvalidRequest("actor required")
        if "withdrawn" not in ALLOWED_TRANSITIONS.get(rec.state, set()):
            raise InvalidTransition("cannot withdraw from %s" % rec.state)
        self._emit(
            RecordWithdrawn(
                record_id=record_id,
                actor=actor,
                timestamp=_now(now),
                reason=reason or "",
            ),
            key=idempotency_key,
            signature=signature,
        )
        return self.snapshot_record(record_id)

    def amend(self, record_id, actor, owner=None, valid_until=None,
              idempotency_key=None, now=None):
        """修订负责人/有效期：产生事件且修订号 +1。"""
        rec = self._require(record_id)
        signature = ("amend", record_id, owner, valid_until)
        hit = self._replay_key(idempotency_key, signature)
        if hit is not None:
            return self.snapshot_record(hit)
        if not actor:
            raise InvalidRequest("actor required")
        if rec.state in TERMINAL_STATES:
            raise InvalidTransition("cannot amend a %s record" % rec.state)
        if owner is None and valid_until is None:
            raise InvalidRequest("nothing to amend")
        if owner is not None and not owner:
            raise InvalidRequest("owner required")
        new_valid_until = None
        if valid_until is not None:
            expires = _parse_dt(valid_until, "valid_until")
            if expires <= (now or datetime.now(timezone.utc)):
                raise InvalidRequest("valid_until must be in the future")
            new_valid_until = expires.isoformat()
        self._emit(
            RecordAmended(
                record_id=record_id,
                actor=actor,
                timestamp=_now(now),
                previous_owner=rec.owner,
                new_owner=owner,
                previous_valid_until=rec.valid_until,
                new_valid_until=new_valid_until,
            ),
            key=idempotency_key,
            signature=signature,
        )
        return self.snapshot_record(record_id)

    # -- 查询 ---------------------------------------------------------------
    def _require(self, record_id):
        rec = self._records.get(record_id)
        if rec is None:
            raise RecordNotFound("no such record: %s" % record_id)
        return rec

    def snapshot_record(self, record_id, now=None):
        return self._require(record_id).to_dict(now)

    def snapshot(self, now=None):
        return [self._records[k].to_dict(now) for k in sorted(self._records)]
