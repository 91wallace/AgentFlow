"""Transactional task/attempt/execution budget ledger with conservative recovery."""

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
import json
import re
from uuid import uuid4

from agentflow.planning import canonical, digest

from .contracts import ConflictError, IntegrityError
from .sdd import SDDStore
from .sqlite import now


_DIMENSION_STATUS = {"applied_verified", "estimated", "unavailable"}
_MEASUREMENT_STATUS = {"known", "estimated", "unknown"}
_DECIMAL = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,12})?$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


def _decimal(value: str | None, label: str) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _DECIMAL.fullmatch(value):
        raise ValueError(f"{label} must be a non-negative decimal string")
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{label} invalid") from exc


@dataclass(frozen=True)
class BudgetAccount:
    account_id: str
    project_id: str
    scope_type: str
    scope_id: str
    currency: str
    cost_limit: str | None = None
    token_limit: int | None = None
    time_limit_seconds: int | None = None
    cost_status: str = "unavailable"
    token_status: str = "unavailable"
    time_status: str = "unavailable"
    cost_evidence: str | None = None
    token_evidence: str | None = None
    time_evidence: str | None = None

    def __post_init__(self):
        for name in ("account_id", "project_id", "scope_id"):
            if not _SAFE_ID.fullmatch(getattr(self, name)):
                raise ValueError(f"{name} invalid")
        if self.scope_type not in {"execution", "task", "attempt"}:
            raise ValueError("budget scope_type invalid")
        if len(self.currency) != 3 or not self.currency.isalpha() or self.currency.upper() != self.currency:
            raise ValueError("budget currency must be ISO-like three-letter code")
        _decimal(self.cost_limit, "cost_limit")
        for value, label in ((self.token_limit, "token_limit"), (self.time_limit_seconds, "time_limit_seconds")):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{label} must be a non-negative integer")
        if any(value not in _DIMENSION_STATUS for value in
               (self.cost_status, self.token_status, self.time_status)):
            raise ValueError("budget enforcement state invalid")
        for state, evidence, label in ((self.cost_status, self.cost_evidence, "cost"),
                                       (self.token_status, self.token_evidence, "tokens"),
                                       (self.time_status, self.time_evidence, "time")):
            if state == "applied_verified" and (not isinstance(evidence, str) or not _SAFE_ID.fullmatch(evidence)):
                raise ValueError(f"{label} applied_verified requires a verification evidence identifier")
            if evidence is not None and (not isinstance(evidence, str) or not _SAFE_ID.fullmatch(evidence)):
                raise ValueError(f"{label} enforcement evidence identifier invalid")


@dataclass(frozen=True)
class BudgetVector:
    cost: str | None
    tokens: int | None
    seconds: int | None
    currency: str
    cost_state: str = "unknown"
    tokens_state: str = "unknown"
    time_state: str = "unknown"

    def __post_init__(self):
        _decimal(self.cost, "budget cost")
        for value, label in ((self.tokens, "tokens"), (self.seconds, "seconds")):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{label} must be a non-negative integer")
        if len(self.currency) != 3 or self.currency.upper() != self.currency:
            raise ValueError("budget currency invalid")
        if any(value not in _MEASUREMENT_STATUS for value in
               (self.cost_state, self.tokens_state, self.time_state)):
            raise ValueError("budget measurement state invalid")
        for value, state, label in ((self.cost, self.cost_state, "cost"),
                                    (self.tokens, self.tokens_state, "tokens"),
                                    (self.seconds, self.time_state, "time")):
            if (state in {"known", "estimated"}) != (value is not None):
                raise ValueError(f"{label} value and measurement state diverge")


@dataclass(frozen=True)
class BudgetReservation:
    reservation_id: str
    operation_id: str
    status: str
    cost_state: str
    tokens_state: str
    time_state: str


class BudgetStore(SDDStore):
    """SQLite v7 facade for immutable limits and append-only usage ledger."""

    @staticmethod
    def _prior_operation(db, operation_id: str, request_digest: str):
        row = db.execute("SELECT request_digest,result_json FROM budget_operations WHERE operation_id=?",
                         (operation_id,)).fetchone()
        if row is None:
            return None
        if row[0] != request_digest:
            raise ConflictError("budget operation_id reutilizado com parâmetros diferentes")
        return json.loads(row[1])

    @staticmethod
    def _record_operation(db, operation_id: str, request_digest: str, kind: str, result: dict):
        db.execute("INSERT INTO budget_operations VALUES(?,?,?,?,?)",
                   (operation_id, request_digest, kind, canonical(result), now()))

    def create_budget_account(self, account: BudgetAccount, operation_id: str) -> str:
        if not _SAFE_ID.fullmatch(operation_id):
            raise ValueError("budget operation_id invalid")
        snapshot = asdict(account)
        request_digest = digest(snapshot)
        with self.transaction() as db:
            prior = self._prior_operation(db, operation_id, request_digest)
            if prior:
                return prior["account_id"]
            if db.execute("SELECT 1 FROM projects WHERE project_id=?", (account.project_id,)).fetchone() is None:
                raise IntegrityError("budget project does not exist")
            existing = db.execute("SELECT snapshot_digest FROM budget_accounts WHERE account_id=?",
                                  (account.account_id,)).fetchone()
            if existing:
                raise ConflictError("budget account_id already belongs to a different immutable account")
            db.execute("INSERT INTO budget_accounts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (account.account_id, account.project_id, account.scope_type, account.scope_id,
                        account.currency, account.cost_limit, account.token_limit, account.time_limit_seconds,
                        account.cost_status, account.token_status, account.time_status, canonical(snapshot),
                        request_digest, operation_id, now()))
            result = {"account_id": account.account_id}
            self._record_operation(db, operation_id, request_digest, "account", result)
            return account.account_id

    def reserve_budget(self, operation_id: str, *, project_id: str, task_id: str,
                       execution_id: str, attempt_id: str | None, account_ids: tuple[str, ...],
                       estimate: BudgetVector) -> BudgetReservation:
        if not _SAFE_ID.fullmatch(operation_id) or not _SAFE_ID.fullmatch(execution_id):
            raise ValueError("budget operation/execution identity invalid")
        if not account_ids or len(account_ids) > 3 or len(set(account_ids)) != len(account_ids):
            raise ValueError("one to three unique budget accounts required")
        request = {"project_id": project_id, "task_id": task_id, "execution_id": execution_id,
                   "attempt_id": attempt_id, "account_ids": list(account_ids), "estimate": asdict(estimate)}
        request_digest = digest(request)
        with self.transaction() as db:
            prior = self._prior_operation(db, operation_id, request_digest)
            if prior:
                return BudgetReservation(**prior)
            accounts = db.execute(
                "SELECT account_id,project_id,scope_type,scope_id,currency,cost_limit,token_limit,time_limit_seconds,cost_status,token_status,time_status "
                "FROM budget_accounts WHERE account_id IN (%s)" % ",".join("?" for _ in account_ids), account_ids).fetchall()
            by_id = {row[0]: row for row in accounts}
            if len(by_id) != len(account_ids):
                raise ConflictError("budget account missing")
            rows = [by_id[item] for item in account_ids]
            if any(row[1] != project_id or row[4] != estimate.currency for row in rows):
                raise ConflictError("budget account project/currency mismatch")
            ranks = {"execution": 0, "task": 1, "attempt": 2}
            if [ranks[row[2]] for row in rows] != sorted(ranks[row[2]] for row in rows):
                raise ConflictError("budget accounts must be ordered execution, task, attempt")
            if len({row[2] for row in rows}) != len(rows):
                raise ConflictError("at most one account per budget scope")
            for row in rows:
                expected = {"execution": execution_id, "task": task_id, "attempt": attempt_id}[row[2]]
                if expected is None or row[3] != expected:
                    raise ConflictError("budget scope does not match reservation")
            if db.execute("SELECT 1 FROM tasks WHERE project_id=? AND task_id=?", (project_id, task_id)).fetchone() is None:
                raise IntegrityError("budget task does not exist")
            if attempt_id and db.execute("SELECT 1 FROM execution_attempts WHERE attempt_id=? AND project_id=? AND task_id=?",
                                         (attempt_id, project_id, task_id)).fetchone() is None:
                raise IntegrityError("budget attempt does not belong to task")

            reservation_id = uuid4().hex
            states = (estimate.cost_state, estimate.tokens_state, estimate.time_state)
            amounts = (estimate.cost, estimate.tokens, estimate.seconds)
            cols = ("cost", "tokens", "seconds")
            holds = []
            for row in rows:
                account_holds = []
                for index, (column, amount, measurement_state, cap, status) in enumerate(zip(
                        cols, amounts, states, (row[5], row[6], row[7]), row[8:11])):
                    committed = self._committed(db, row[0], column)
                    cap_value = _decimal(cap, f"{column} cap") if column == "cost" else cap
                    requested = (amount if amount is not None else
                                 max(0, cap_value - committed) if cap_value is not None else 0)
                    requested_num = _decimal(str(requested), column) if column == "cost" else requested
                    if cap_value is not None and committed + requested_num > cap_value:
                        raise ConflictError(f"budget {column} limit exceeded for {row[2]} scope")
                    if status == "unavailable" and cap_value is not None and measurement_state == "unknown":
                        # Hold the entire remaining cap until usage can be reconciled.
                        requested_num = max(0, cap_value - committed)
                    account_holds.append(requested_num)
                holds.append((row[0], *account_holds))
            db.execute("INSERT INTO budget_reservations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (reservation_id, operation_id, request_digest, project_id, task_id, attempt_id,
                        execution_id, estimate.currency, str(estimate.cost or "0"), estimate.tokens or 0,
                        estimate.seconds or 0, *states, "reserved", now(), now()))
            for account_id, cost_hold, tokens_hold, seconds_hold in holds:
                db.execute("INSERT INTO budget_reservation_accounts VALUES(?,?,?,?,?)",
                           (reservation_id, account_id, str(cost_hold), tokens_hold, seconds_hold))
                payload = {"measurement_states": list(states), "estimate": asdict(estimate)}
                db.execute("INSERT INTO budget_ledger(operation_id,request_digest,reservation_id,account_id,kind,cost_delta,tokens_delta,seconds_delta,payload_json,occurred_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (operation_id, request_digest, reservation_id, account_id, "reserved",
                            str(cost_hold), tokens_hold, seconds_hold, canonical(payload), now()))
            result = BudgetReservation(reservation_id, operation_id, "reserved", *states)
            self._record_operation(db, operation_id, request_digest, "reserve", asdict(result))
            return result

    @staticmethod
    def _committed(db, account_id: str, dimension: str):
        column = {"cost": "cost_delta", "tokens": "tokens_delta", "seconds": "seconds_delta"}[dimension]
        if dimension == "cost":
            return sum((Decimal(row[0]) for row in db.execute(
                "SELECT cost_delta FROM budget_ledger WHERE account_id=?", (account_id,))), Decimal(0))
        value = db.execute(f"SELECT COALESCE(SUM({column}),0) FROM budget_ledger WHERE account_id=?",
                           (account_id,)).fetchone()[0]
        return int(value)

    def reconcile_budget(self, operation_id: str, reservation_id: str, usage: BudgetVector,
                         *, source: str) -> BudgetReservation:
        if not _SAFE_ID.fullmatch(operation_id) or not _SAFE_ID.fullmatch(source):
            raise ValueError("reconciliation operation/source invalid")
        request = {"reservation_id": reservation_id, "usage": asdict(usage), "source": source}
        request_digest = digest(request)
        with self.transaction() as db:
            prior = self._prior_operation(db, operation_id, request_digest)
            if prior:
                return BudgetReservation(**prior)
            reservation = db.execute("SELECT project_id,task_id,currency,status FROM budget_reservations WHERE reservation_id=?",
                                     (reservation_id,)).fetchone()
            if reservation is None:
                raise IntegrityError("budget reservation missing")
            if reservation[2] != usage.currency:
                raise ConflictError("usage currency differs from reservation")
            if reservation[3] == "reconciled":
                raise ConflictError("budget reservation already reconciled")
            allocations = db.execute("SELECT account_id,cost_held,tokens_held,seconds_held FROM budget_reservation_accounts WHERE reservation_id=?",
                                     (reservation_id,)).fetchall()
            for account_id, held_cost, held_tokens, held_seconds in allocations:
                deltas = []
                for actual, state, held, dimension in zip(
                        (usage.cost, usage.tokens, usage.seconds),
                        (usage.cost_state, usage.tokens_state, usage.time_state),
                        (held_cost, held_tokens, held_seconds), ("cost", "tokens", "seconds")):
                    held_value = Decimal(held) if dimension == "cost" else held
                    actual_value = (Decimal(actual) if dimension == "cost" else actual)
                    deltas.append((Decimal(0) if dimension == "cost" else 0)
                                  if state == "unknown" else actual_value - held_value)
                db.execute("INSERT INTO budget_ledger(operation_id,request_digest,reservation_id,account_id,kind,cost_delta,tokens_delta,seconds_delta,payload_json,occurred_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (operation_id, request_digest, reservation_id, account_id, "reconciled",
                            str(deltas[0]), int(deltas[1]), int(deltas[2]), canonical({"usage": asdict(usage), "source": source}), now()))
                new_holds = []
                for actual, state, held in zip((usage.cost, usage.tokens, usage.seconds),
                                               (usage.cost_state, usage.tokens_state, usage.time_state),
                                               (held_cost, held_tokens, held_seconds)):
                    new_holds.append(held if state == "unknown" else actual)
                db.execute("UPDATE budget_reservation_accounts SET cost_held=?,tokens_held=?,seconds_held=? WHERE reservation_id=? AND account_id=?",
                           (str(new_holds[0]), new_holds[1], new_holds[2], reservation_id, account_id))
            states = (usage.cost_state, usage.tokens_state, usage.time_state)
            status = "unknown" if "unknown" in states else "reconciled"
            db.execute("INSERT INTO budget_reconciliations VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (operation_id, request_digest, reservation_id, usage.cost, usage.tokens, usage.seconds,
                        *states, source, now()))
            db.execute("UPDATE budget_reservations SET status=?,updated_at=? WHERE reservation_id=?",
                       (status, now(), reservation_id))
            result = BudgetReservation(reservation_id, operation_id, status, *states)
            self._record_operation(db, operation_id, request_digest, "reconcile", asdict(result))
            return result

    def recover_budget_reservations(self) -> tuple[str, ...]:
        recovered = []
        with self.transaction() as db:
            rows = db.execute("SELECT reservation_id,request_digest,status FROM budget_reservations WHERE status='reserved' ORDER BY created_at,reservation_id").fetchall()
            for reservation_id, request_digest, _ in rows:
                operation_id = f"recovery-{reservation_id}"
                prior = db.execute("SELECT request_digest FROM budget_operations WHERE operation_id=?", (operation_id,)).fetchone()
                if prior:
                    if prior[0] != request_digest:
                        raise ConflictError("budget recovery operation digest diverged")
                    continue
                accounts = db.execute("SELECT account_id FROM budget_reservation_accounts WHERE reservation_id=? ORDER BY account_id",
                                      (reservation_id,)).fetchall()
                for (account_id,) in accounts:
                    db.execute("INSERT INTO budget_ledger(operation_id,request_digest,reservation_id,account_id,kind,cost_delta,tokens_delta,seconds_delta,payload_json,occurred_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                               (operation_id, request_digest, reservation_id, account_id, "recovered_unknown",
                                "0", 0, 0, canonical({"outcome": "unknown", "reason": "interrupted_before_reconciliation"}), now()))
                db.execute("UPDATE budget_reservations SET status='unknown',updated_at=? WHERE reservation_id=?",
                           (now(), reservation_id))
                result = {"reservation_id": reservation_id, "status": "unknown"}
                self._record_operation(db, operation_id, request_digest, "recovery", result)
                recovered.append(reservation_id)
        return tuple(recovered)

    def budget_account_usage(self, account_id: str) -> dict:
        row = self.db.execute("SELECT cost_limit,token_limit,time_limit_seconds,cost_status,token_status,time_status,currency FROM budget_accounts WHERE account_id=?",
                              (account_id,)).fetchone()
        if row is None:
            raise IntegrityError("budget account missing")
        return {"account_id": account_id, "cost_committed": str(self._committed(self.db, account_id, "cost")),
                "tokens_committed": self._committed(self.db, account_id, "tokens"),
                "seconds_committed": self._committed(self.db, account_id, "seconds"),
                "cost_limit": row[0], "token_limit": row[1], "time_limit_seconds": row[2],
                "enforcement": {"cost": row[3], "tokens": row[4], "time": row[5]},
                "provider_invoice_enforcement": "unavailable", "currency": row[6]}
