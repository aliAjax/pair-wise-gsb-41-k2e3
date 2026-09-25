"""灾害事件紧急预付额度池（额度占用层）。

只负责额度的登记、占用、释放和按申请先后放行，不判断案件是否符合
预付条件（业务判断在 app.py 服务层，页面在 static/）。所有写操作都
接收调用方传入的 sqlite3.Connection，与案件更新处于同一事务；数据
落在同一个 SQLite 文件中，服务或页面重开后仍能接上。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS event_pools (
    event_id TEXT PRIMARY KEY,
    total_quota REAL NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS advance_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    claim_id INTEGER NOT NULL,
    claim_no TEXT NOT NULL,
    amount REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    requested_by TEXT NOT NULL,
    reference TEXT NOT NULL,
    created_at TEXT NOT NULL,
    approved_at TEXT,
    closed_at TEXT,
    close_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_advance_event ON advance_requests(event_id, status, id);
CREATE INDEX IF NOT EXISTS idx_advance_claim ON advance_requests(claim_id, status);
"""

PENDING = "pending"      # 待放行：额度不足，排队等待
APPROVED = "approved"    # 已放行：占用额度，预付款已批准
RELEASED = "released"    # 已释放：案件拒赔/退回/核定完成后交还占用
CANCELLED = "cancelled"  # 已取消：案件结束或不再满足预付条件


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> dict | None:
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def _all(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def upsert_pool(conn: sqlite3.Connection, event_id: str, total_quota: float, actor: str) -> dict:
    now = utcnow()
    if get_pool(conn, event_id):
        conn.execute(
            "UPDATE event_pools SET total_quota=?,updated_at=?,version=version+1 WHERE event_id=?",
            (total_quota, now, event_id),
        )
    else:
        conn.execute(
            "INSERT INTO event_pools(event_id,total_quota,created_by,created_at,updated_at) VALUES(?,?,?,?,?)",
            (event_id, total_quota, actor, now, now),
        )
    return get_pool(conn, event_id)


def get_pool(conn: sqlite3.Connection, event_id: str) -> dict | None:
    return _one(conn, "SELECT * FROM event_pools WHERE event_id=?", (event_id,))


def occupied_amount(conn: sqlite3.Connection, event_id: str) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(amount),0) AS s FROM advance_requests WHERE event_id=? AND status=?",
        (event_id, APPROVED),
    ).fetchone()
    return round(row["s"], 2)


def list_pools(conn: sqlite3.Connection) -> list[dict]:
    pools = _all(conn, "SELECT * FROM event_pools ORDER BY event_id")
    for pool in pools:
        occupied = occupied_amount(conn, pool["event_id"])
        pool["occupied"] = occupied
        pool["available"] = round(pool["total_quota"] - occupied, 2)
        pool["pending_count"] = conn.execute(
            "SELECT COUNT(*) AS c FROM advance_requests WHERE event_id=? AND status=?",
            (pool["event_id"], PENDING),
        ).fetchone()["c"]
    return pools


def create_request(conn: sqlite3.Connection, event_id: str, claim_id: int, claim_no: str,
                   amount: float, actor: str, reference: str) -> dict:
    cur = conn.execute(
        """INSERT INTO advance_requests(event_id,claim_id,claim_no,amount,status,requested_by,reference,created_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (event_id, claim_id, claim_no, amount, PENDING, actor, reference, utcnow()),
    )
    return get_request(conn, cur.lastrowid)


def get_request(conn: sqlite3.Connection, request_id: int) -> dict | None:
    return _one(conn, "SELECT * FROM advance_requests WHERE id=?", (request_id,))


def pending_requests(conn: sqlite3.Connection, event_id: str) -> list[dict]:
    return _all(
        conn,
        "SELECT * FROM advance_requests WHERE event_id=? AND status=? ORDER BY id",
        (event_id, PENDING),
    )


def pending_request_for_claim(conn: sqlite3.Connection, claim_id: int) -> dict | None:
    return _one(
        conn,
        "SELECT * FROM advance_requests WHERE claim_id=? AND status=? ORDER BY id LIMIT 1",
        (claim_id, PENDING),
    )


def reference_in_use(conn: sqlite3.Connection, reference: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM advance_requests WHERE reference=? AND status IN (?,?) LIMIT 1",
        (reference, PENDING, APPROVED),
    ).fetchone() is not None


def cancel_request(conn: sqlite3.Connection, request_id: int, reason: str) -> dict | None:
    conn.execute(
        "UPDATE advance_requests SET status=?,closed_at=?,close_reason=? WHERE id=? AND status=?",
        (CANCELLED, utcnow(), reason, request_id, PENDING),
    )
    return get_request(conn, request_id)


def cancel_pending_for_claim(conn: sqlite3.Connection, claim_id: int, reason: str) -> list[dict]:
    rows = _all(
        conn,
        "SELECT * FROM advance_requests WHERE claim_id=? AND status=? ORDER BY id",
        (claim_id, PENDING),
    )
    for row in rows:
        cancel_request(conn, row["id"], reason)
    return rows


def release_claim_holds(conn: sqlite3.Connection, claim_id: int, reason: str) -> list[dict]:
    """把案件已放行的占用全部交还额度池，返回被释放的申请。"""
    rows = _all(
        conn,
        "SELECT * FROM advance_requests WHERE claim_id=? AND status=? ORDER BY id",
        (claim_id, APPROVED),
    )
    now = utcnow()
    for row in rows:
        conn.execute(
            "UPDATE advance_requests SET status=?,closed_at=?,close_reason=? WHERE id=?",
            (RELEASED, now, reason, row["id"]),
        )
    return rows


def process_queue(conn: sqlite3.Connection, event_id: str) -> list[dict]:
    """按申请先后放行待放行队列：队首放得下就放行，放不下就整体停住。"""
    pool = get_pool(conn, event_id)
    if not pool:
        return []
    available = round(pool["total_quota"] - occupied_amount(conn, event_id), 2)
    promoted = []
    for req in pending_requests(conn, event_id):
        if req["amount"] > available + 1e-9:
            break
        now = utcnow()
        conn.execute(
            "UPDATE advance_requests SET status=?,approved_at=? WHERE id=? AND status=?",
            (APPROVED, now, req["id"], PENDING),
        )
        available = round(available - req["amount"], 2)
        promoted.append({**req, "status": APPROVED, "approved_at": now})
    return promoted


def pool_detail(conn: sqlite3.Connection, event_id: str) -> dict | None:
    """单事件视图：额度、占用、剩余，以及带缺口的待放行清单。"""
    pool = get_pool(conn, event_id)
    if not pool:
        return None
    occupied = occupied_amount(conn, event_id)
    available = round(pool["total_quota"] - occupied, 2)
    rows = _all(conn, "SELECT * FROM advance_requests WHERE event_id=? ORDER BY id", (event_id,))
    running = available
    pending, holds, history = [], [], []
    for row in rows:
        if row["status"] == PENDING:
            # 缺口按排在前面的待放行申请先占用剩余额度来累计
            row["gap"] = max(0.0, round(row["amount"] - running, 2))
            running = round(max(0.0, running - row["amount"]), 2)
            pending.append(row)
        elif row["status"] == APPROVED:
            holds.append(row)
        else:
            history.append(row)
    return {
        "pool": pool,
        "occupied": occupied,
        "available": available,
        "pending": pending,
        "holds": holds,
        "history": history,
    }
