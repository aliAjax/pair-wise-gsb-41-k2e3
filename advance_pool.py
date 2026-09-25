"""紧急预付共享额度的占用台账（按灾害事件共享一笔总额度）。

本模块只负责额度账本本身：
- 主管录入/调整事件总额度；
- 预付申请按先后顺序占用额度，额度不足时排队等待并暴露缺口；
- 案件拒赔、退回或核定完成后，未用占用归还台账。

“谁能申请、金额是否在预估损失 20% 以内、付款如何入账”等业务判断
不属于本层，统一由 app.CatastropheClaimService 负责。台账方法都接收
业务层已经开启的事务连接，保证占用与付款在同一事务内落库。
"""
from __future__ import annotations

import sqlite3
from typing import Any

HELD = "held"            # 已占用额度并完成放款
WAITING = "waiting"      # 额度不足，待放行
RELEASED = "released"    # 案件终结/退回，占用已归还
CANCELLED = "cancelled"  # 排队期间丧失预付资格，取消等待

ACTIVE = (HELD, WAITING)
_EPS = 1e-9


class AdvancePool:
    @staticmethod
    def create_schema(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS advance_quotas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                total_amount REAL NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS advance_reservations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL,
                claim_id INTEGER NOT NULL REFERENCES claims(id),
                amount REAL NOT NULL,
                status TEXT NOT NULL,
                payment_reference TEXT,
                queued_at TEXT NOT NULL,
                decided_at TEXT,
                release_reason TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_reservations_event
                ON advance_reservations(event_id, status, id);
            CREATE INDEX IF NOT EXISTS idx_reservations_claim
                ON advance_reservations(claim_id, status);
            """
        )

    @staticmethod
    def set_quota(conn: sqlite3.Connection, event_id: str, total_amount: Any,
                  actor: str, now: str, expected_version: int | None = None) -> dict[str, Any]:
        event_id = (event_id or "").strip()
        if not event_id:
            raise ValueError("灾害事件编号不能为空")
        try:
            total_amount = float(total_amount)
        except (TypeError, ValueError) as exc:
            raise ValueError("总额度必须是数值") from exc
        if total_amount <= 0:
            raise ValueError("总额度必须大于0")
        row = conn.execute("SELECT * FROM advance_quotas WHERE event_id=?", (event_id,)).fetchone()
        if row is None:
            cur = conn.execute(
                """INSERT INTO advance_quotas(event_id,total_amount,created_by,created_at,updated_at)
                   VALUES(?,?,?,?,?)""",
                (event_id, total_amount, actor, now, now),
            )
            return dict(conn.execute("SELECT * FROM advance_quotas WHERE id=?", (cur.lastrowid,)).fetchone())
        if expected_version is not None and int(row["version"]) != int(expected_version):
            raise LookupError("额度记录已变化，请刷新后重试")
        held_total = AdvancePool.held_total(conn, event_id)
        if total_amount < held_total - _EPS:
            raise ValueError("调整后总额度不能低于当前已占用额度 %.2f" % held_total)
        conn.execute(
            "UPDATE advance_quotas SET total_amount=?,version=version+1,updated_at=? WHERE id=?",
            (total_amount, now, row["id"]),
        )
        return dict(conn.execute("SELECT * FROM advance_quotas WHERE id=?", (row["id"],)).fetchone())

    @staticmethod
    def quota_row(conn: sqlite3.Connection, event_id: str) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM advance_quotas WHERE event_id=?", ((event_id or "").strip(),)).fetchone()

    @staticmethod
    def held_total(conn: sqlite3.Connection, event_id: str) -> float:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount),0) AS s FROM advance_reservations WHERE event_id=? AND status=?",
            (event_id, HELD),
        ).fetchone()
        return float(row["s"])

    @staticmethod
    def remaining(conn: sqlite3.Connection, event_id: str) -> float:
        quota = AdvancePool.quota_row(conn, event_id)
        if quota is None:
            return 0.0
        return float(quota["total_amount"]) - AdvancePool.held_total(conn, event_id)

    @staticmethod
    def reference_busy(conn: sqlite3.Connection, reference: str) -> bool:
        reference = (reference or "").strip()
        if not reference:
            return True
        if conn.execute("SELECT 1 FROM payments WHERE reference=? LIMIT 1", (reference,)).fetchone():
            return True
        placeholders = ",".join("?" for _ in ACTIVE)
        if conn.execute(
            "SELECT 1 FROM advance_reservations WHERE payment_reference=? AND status IN (%s) LIMIT 1" % placeholders,
            (reference, *ACTIVE),
        ).fetchone():
            return True
        return False

    @staticmethod
    def has_active_waiting(conn: sqlite3.Connection, claim_id: int) -> bool:
        return bool(conn.execute(
            "SELECT 1 FROM advance_reservations WHERE claim_id=? AND status=? LIMIT 1",
            (claim_id, WAITING),
        ).fetchone())

    @staticmethod
    def reserve(conn: sqlite3.Connection, event_id: str, claim_id: int, amount: float,
                reference: str, now: str) -> dict[str, Any]:
        """尝试占用额度；剩余额度不足则排队。调用方须先确认额度台账已存在。"""
        if AdvancePool.remaining(conn, event_id) + _EPS >= amount:
            status, decided_at = HELD, now
        else:
            status, decided_at = WAITING, None
        cur = conn.execute(
            """INSERT INTO advance_reservations(event_id,claim_id,amount,status,payment_reference,queued_at,decided_at)
               VALUES(?,?,?,?,?,?,?)""",
            (event_id, claim_id, amount, status, (reference or "").strip(), now, decided_at),
        )
        return dict(conn.execute("SELECT * FROM advance_reservations WHERE id=?", (cur.lastrowid,)).fetchone())

    @staticmethod
    def head_waiting(conn: sqlite3.Connection, event_id: str) -> sqlite3.Row | None:
        """FIFO：按申请先后（自增 id）取队头待放行申请。"""
        return conn.execute(
            "SELECT * FROM advance_reservations WHERE event_id=? AND status=? ORDER BY id LIMIT 1",
            (event_id, WAITING),
        ).fetchone()

    @staticmethod
    def mark_held(conn: sqlite3.Connection, reservation_id: int, now: str) -> None:
        conn.execute(
            "UPDATE advance_reservations SET status=?,decided_at=? WHERE id=?",
            (HELD, now, reservation_id),
        )

    @staticmethod
    def cancel(conn: sqlite3.Connection, reservation_id: int, reason: str, now: str) -> None:
        conn.execute(
            "UPDATE advance_reservations SET status=?,decided_at=?,release_reason=? WHERE id=?",
            (CANCELLED, now, reason, reservation_id),
        )

    @staticmethod
    def release_for_claim(conn: sqlite3.Connection, claim_id: int, reason: str, now: str) -> float:
        """案件拒赔、退回或核定完成：已占用归还，排队中的申请作废。返回释放金额。"""
        released = conn.execute(
            "SELECT COALESCE(SUM(amount),0) AS s FROM advance_reservations WHERE claim_id=? AND status=?",
            (claim_id, HELD),
        ).fetchone()["s"]
        conn.execute(
            "UPDATE advance_reservations SET status=?,decided_at=?,release_reason=? WHERE claim_id=? AND status=?",
            (RELEASED, now, reason, claim_id, HELD),
        )
        conn.execute(
            "UPDATE advance_reservations SET status=?,decided_at=?,release_reason=? WHERE claim_id=? AND status=?",
            (CANCELLED, now, reason, claim_id, WAITING),
        )
        return float(released)

    @staticmethod
    def summary(conn: sqlite3.Connection, event_id: str) -> dict[str, Any] | None:
        quota = AdvancePool.quota_row(conn, event_id)
        if quota is None:
            return None
        held = [dict(r) for r in conn.execute(
            """SELECT r.*, c.claim_no FROM advance_reservations r
               JOIN claims c ON c.id=r.claim_id
               WHERE r.event_id=? AND r.status=? ORDER BY r.id""",
            (event_id, HELD),
        ).fetchall()]
        waiting = [dict(r) for r in conn.execute(
            """SELECT r.*, c.claim_no FROM advance_reservations r
               JOIN claims c ON c.id=r.claim_id
               WHERE r.event_id=? AND r.status=? ORDER BY r.id""",
            (event_id, WAITING),
        ).fetchall()]
        available = float(quota["total_amount"]) - sum(item["amount"] for item in held)
        for item in waiting:
            item["shortfall"] = max(0.0, item["amount"] - available)
            available = max(0.0, available - item["amount"])
        return {
            "event_id": event_id,
            "total_amount": float(quota["total_amount"]),
            "version": int(quota["version"]),
            "held_total": sum(item["amount"] for item in held),
            "waiting_total": sum(item["amount"] for item in waiting),
            "remaining": float(quota["total_amount"]) - sum(item["amount"] for item in held),
            # 要清空整条待放行队列，还需补充的总额度
            "queue_shortfall": max(0.0, sum(item["amount"] for item in waiting) - (float(quota["total_amount"]) - sum(item["amount"] for item in held))),
            "held": held,
            "waiting": waiting,
        }

    @staticmethod
    def list_events(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = conn.execute("SELECT event_id FROM advance_quotas ORDER BY id").fetchall()
        return [AdvancePool.summary(conn, row["event_id"]) for row in rows]
