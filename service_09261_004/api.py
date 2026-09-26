"""JSON API 适配器。

路由：
- POST /records                       创建反馈记录
- POST /records/{id}/transitions      推进状态 {new_state, actor}
- POST /records/{id}/withdrawal       撤回 {actor, reason?}
- POST /records/{id}/amendments       修订 {actor, owner?, valid_until?}
- GET  /records                       全部记录快照
- GET  /records/{id}                  单条记录
- GET  /records/{id}/events          事件历史（撤回只追加，可在此验证）

所有命令体均可带 ``idempotency_key``。
"""
from __future__ import annotations

from .workflow import (
    InvalidRequest,
    RecordNotFound,
    DuplicateRecord,
    InvalidTransition,
    ConflictingIdempotencyKey,
    WorkflowError,
    event_to_dict,
)

_ERROR_STATUS = {
    InvalidRequest: 400,
    RecordNotFound: 404,
    DuplicateRecord: 409,
    InvalidTransition: 409,
    ConflictingIdempotencyKey: 409,
}


def _error(exc):
    status = _ERROR_STATUS.get(type(exc), 500)
    return status, {"error": type(exc).__name__, "message": str(exc)}


def dispatch(service, method, path, body=None):
    body = body or {}
    parts = [p for p in path.strip("/").split("/") if p]

    try:
        if method == "POST" and path == "/records":
            return 201, service.create_feedback(
                body["id"], body["owner"], body["valid_until"],
                actor=body.get("actor"),
                idempotency_key=body.get("idempotency_key"),
            )

        if method == "POST" and len(parts) == 3 and parts[0] == "records":
            rid, action = parts[1], parts[2]
            if action == "transitions":
                return 200, service.transition(
                    rid, body["new_state"], body["actor"],
                    idempotency_key=body.get("idempotency_key"),
                )
            if action == "withdrawal":
                return 200, service.withdraw(
                    rid, body["actor"], reason=body.get("reason", ""),
                    idempotency_key=body.get("idempotency_key"),
                )
            if action == "amendments":
                return 200, service.amend(
                    rid, body["actor"],
                    owner=body.get("owner"),
                    valid_until=body.get("valid_until"),
                    idempotency_key=body.get("idempotency_key"),
                )

        if method == "GET" and path == "/records":
            return 200, service.snapshot()

        if method == "GET" and len(parts) == 2 and parts[0] == "records":
            return 200, service.get_record(parts[1])

        if method == "GET" and len(parts) == 3 and parts[0] == "records" and parts[2] == "events":
            return 200, [
                event_to_dict(e) for e in service.flow.events
                if e.record_id == parts[1]
            ]

        return 404, {"error": "not_found", "message": path}
    except KeyError as exc:
        return 400, {"error": "InvalidRequest", "message": "missing field: %s" % exc.args[0]}
    except WorkflowError as exc:
        return _error(exc)
