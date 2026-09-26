"""JSON API 适配器。"""
from dataclasses import asdict
from .ledger import LedgerError, NotFoundError, OrderError, DuplicateError


def dispatch(flow,method,path,body=None):
 body=body or {}
 if method=="POST" and path=="/cases": return 201,flow.create(body["id"],body["actor"],body.get("idempotency_key")).__dict__
 if method=="POST" and path.endswith("/move"): return 200,flow.move(path.split("/")[2],body["state"],body["actor"]).__dict__
 if method=="GET" and path=="/cases": return 200,flow.snapshot()
 return 404,{"error":"not_found"}


def dispatch_ledger(ledger, method, path, body=None):
    """课堂反馈记录簿的 JSON 边界：命令返回记录投影，错误映射为状态码。"""
    body = body or {}
    parts = [p for p in path.split("/") if p]
    try:
        if method == "POST" and parts == ["records"]:
            rec = ledger.register(body["id"], body["owner"], body["valid_from"],
                                  body["valid_until"], body["summary"], body["actor"],
                                  key=body.get("idempotency_key"))
            return 201, asdict(rec)
        if method == "GET" and parts == ["records"]:
            return 200, ledger.snapshot()
        if len(parts) >= 2 and parts[0] == "records":
            if method == "GET" and len(parts) == 2:
                return 200, asdict(ledger.get(parts[1]))
            if method == "POST" and len(parts) == 3 and parts[2] == "revisions":
                rec = ledger.revise(parts[1], body["revision"], body["actor"],
                                    owner=body.get("owner"), valid_from=body.get("valid_from"),
                                    valid_until=body.get("valid_until"),
                                    summary=body.get("summary"),
                                    key=body.get("idempotency_key"))
                return 200, asdict(rec)
            if method == "POST" and len(parts) == 3 and parts[2] == "withdrawal":
                rec = ledger.withdraw(parts[1], body["revision"], body["reason"],
                                      body["actor"], key=body.get("idempotency_key"))
                return 200, asdict(rec)
        return 404, {"error": "not_found"}
    except KeyError as field:
        return 400, {"error": "missing field %s" % field}
    except NotFoundError as e:
        return 404, {"error": str(e)}
    except (OrderError, DuplicateError) as e:
        return 409, {"error": str(e)}
    except LedgerError as e:
        return 400, {"error": str(e)}
