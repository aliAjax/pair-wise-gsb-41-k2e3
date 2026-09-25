"""Catastrophe insurance claim triage and settlement service."""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from advance_pool import CANCELLED, HELD, WAITING, AdvancePool

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "catastrophe_claims.db"
TERMINAL = {"duplicate", "approved", "rejected", "closed"}
# 案件被退回后回到的状态：等待主管重新分配
RETURNED_STATE = "triaged"
TRANSITIONS = {
    "received": {"triaged"},
    "triaged": {"assigned", "escalated"},
    "assigned": {"survey", "escalated"},
    "survey": {"review", "escalated"},
    "review": {"approved", "rejected", "escalated"},
    "escalated": {"assigned", "review", "rejected"},
}


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def require_role(role: str, allowed: set[str], action: str) -> None:
    if role not in allowed:
        raise DomainError("角色无权执行：%s" % action, 403)


def actor_id(actor: str) -> str:
    actor = (actor or "").strip()
    if not actor:
        raise DomainError("缺少操作人")
    return actor


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(a))


def coordinate(value: Any, label: str, low: float, high: float) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError) as exc:
        raise DomainError("%s必须是数值" % label) from exc
    if not low <= value <= high:
        raise DomainError("%s超出有效范围" % label)
    return value


class CatastropheClaimService:
    def __init__(self, db_path: str | os.PathLike[str] = DEFAULT_DB):
        self.db_path = str(db_path)
        self.pool = AdvancePool()
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS claims (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_no TEXT NOT NULL UNIQUE,
                    event_id TEXT NOT NULL,
                    region TEXT NOT NULL,
                    peril_type TEXT NOT NULL,
                    policy_no TEXT NOT NULL,
                    claimant_ref TEXT NOT NULL,
                    latitude REAL NOT NULL,
                    longitude REAL NOT NULL,
                    estimated_loss REAL NOT NULL,
                    urgent_need INTEGER NOT NULL DEFAULT 0,
                    fraud_score REAL NOT NULL DEFAULT 0,
                    priority_score REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'received',
                    assignee TEXT,
                    surveyor TEXT,
                    lodging_required INTEGER NOT NULL DEFAULT 0,
                    remote_review INTEGER NOT NULL DEFAULT 0,
                    emergency_advance REAL NOT NULL DEFAULT 0,
                    final_payout REAL,
                    duplicate_of INTEGER REFERENCES claims(id),
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evidence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_id INTEGER NOT NULL REFERENCES claims(id),
                    sha256 TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    source TEXT NOT NULL,
                    submitter TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'received',
                    created_at TEXT NOT NULL,
                    UNIQUE(claim_id,sha256)
                );
                CREATE TABLE IF NOT EXISTS survey_notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_id INTEGER NOT NULL REFERENCES claims(id),
                    surveyor TEXT NOT NULL,
                    damage_ratio REAL NOT NULL,
                    findings TEXT NOT NULL,
                    recommendation TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_id INTEGER NOT NULL REFERENCES claims(id),
                    kind TEXT NOT NULL,
                    amount REAL NOT NULL,
                    approved_by TEXT NOT NULL,
                    reference TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS timeline (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_id INTEGER REFERENCES claims(id),
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_claims_queue ON claims(status, priority_score DESC, created_at);
                CREATE INDEX IF NOT EXISTS idx_evidence_hash ON evidence(sha256);
                """
            )
            AdvancePool.create_schema(conn)

    def _audit(self, conn: sqlite3.Connection, claim_id: int | None, actor: str, action: str, details: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO timeline(claim_id,actor,action,details,created_at) VALUES(?,?,?,?,?)",
            (claim_id, actor, action, json.dumps(details, ensure_ascii=False, sort_keys=True), utcnow()),
        )

    def _claim(self, conn: sqlite3.Connection, claim_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
        if not row:
            raise DomainError("理赔案件不存在", 404)
        return row

    def create_claim(self, actor: str, role: str, claim_no: str, event_id: str,
                     region: str, peril_type: str, policy_no: str, claimant_ref: str,
                     latitude: float, longitude: float, estimated_loss: float,
                     urgent_need: bool = False, lodging_required: bool = False) -> dict[str, Any]:
        actor = actor_id(actor)
        require_role(role, {"intake", "supervisor"}, "创建报案")
        values = [claim_no, event_id, region, peril_type, policy_no, claimant_ref]
        if not all(str(v).strip() for v in values):
            raise DomainError("案件必需字段不能为空")
        lat = coordinate(latitude, "纬度", -90, 90)
        lon = coordinate(longitude, "经度", -180, 180)
        try:
            estimated_loss = float(estimated_loss)
        except (TypeError, ValueError) as exc:
            raise DomainError("预估损失必须是数值") from exc
        if estimated_loss < 0:
            raise DomainError("预估损失不能为负数")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = utcnow()
            duplicate_of = None
            candidates = conn.execute(
                """SELECT * FROM claims WHERE event_id=? AND policy_no=? AND status<>'duplicate'
                   ORDER BY id DESC LIMIT 50""",
                (event_id.strip(), policy_no.strip()),
            ).fetchall()
            for row in candidates:
                within_time = abs((datetime.fromisoformat(now) - datetime.fromisoformat(row["created_at"])).total_seconds()) <= 172800
                loss_close = abs(row["estimated_loss"] - estimated_loss) <= max(1000.0, row["estimated_loss"] * 0.1)
                if within_time and loss_close and haversine_km(lat, lon, row["latitude"], row["longitude"]) <= 3.0:
                    duplicate_of = row["id"]
                    break
            status = "duplicate" if duplicate_of else "received"
            try:
                cur = conn.execute(
                    """INSERT INTO claims(claim_no,event_id,region,peril_type,policy_no,claimant_ref,latitude,longitude,
                       estimated_loss,urgent_need,lodging_required,status,duplicate_of,created_by,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (claim_no.strip(), event_id.strip(), region.strip(), peril_type.strip(), policy_no.strip(),
                     claimant_ref.strip(), lat, lon, estimated_loss, int(bool(urgent_need)), int(bool(lodging_required)),
                     status, duplicate_of, actor, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("报案编号已存在", 409) from exc
            self._audit(conn, cur.lastrowid, actor, "claim.created", {"duplicate_of": duplicate_of})
            if duplicate_of:
                self._audit(conn, duplicate_of, actor, "claim.duplicate_detected", {"new_claim": claim_no.strip()})
            return dict(self._claim(conn, cur.lastrowid))

    def triage_claim(self, actor: str, role: str, claim_id: int, expected_version: int,
                     fraud_score: float = 0.0, remote_review: bool = False) -> dict[str, Any]:
        actor = actor_id(actor)
        require_role(role, {"supervisor"}, "案件分级")
        try:
            fraud_score = float(fraud_score)
        except (TypeError, ValueError) as exc:
            raise DomainError("欺诈评分必须是数值") from exc
        if not 0 <= fraud_score <= 1:
            raise DomainError("欺诈评分应在 0 到 1 之间")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            claim = self._claim(conn, claim_id)
            if claim["status"] != "received":
                raise DomainError("只有待分级案件可以分级", 409)
            if claim["version"] != int(expected_version):
                raise DomainError("案件已变化，请刷新后重试", 409)
            priority = min(100.0, claim["estimated_loss"] / 100000.0 * 25 + (40 if claim["urgent_need"] else 0) + fraud_score * 20 + (10 if claim["lodging_required"] else 0))
            new_status = "escalated" if fraud_score >= 0.8 else "triaged"
            conn.execute(
                "UPDATE claims SET fraud_score=?,priority_score=?,remote_review=?,status=?,version=version+1,updated_at=? WHERE id=?",
                (fraud_score, priority, int(bool(remote_review)), new_status, utcnow(), claim_id),
            )
            self._audit(conn, claim_id, actor, "claim.triaged", {"priority": priority, "status": new_status})
            return dict(self._claim(conn, claim_id))

    def assign_claim(self, actor: str, role: str, claim_id: int, assignee: str,
                     expected_version: int, surveyor: str | None = None) -> dict[str, Any]:
        actor = actor_id(actor)
        require_role(role, {"supervisor"}, "分配案件")
        assignee = assignee.strip()
        if not assignee:
            raise DomainError("查勘负责人不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            claim = self._claim(conn, claim_id)
            if claim["status"] not in {"triaged", "escalated", "assigned"}:
                raise DomainError("当前状态不能分配", 409)
            if claim["version"] != int(expected_version):
                raise DomainError("案件已变化，请刷新后重试", 409)
            conn.execute(
                """UPDATE claims SET assignee=?,surveyor=?,status='assigned',version=version+1,updated_at=?
                   WHERE id=? AND version=?""",
                (assignee, surveyor.strip() if surveyor else None, utcnow(), claim_id, expected_version),
            )
            self._audit(conn, claim_id, actor, "claim.assigned", {"assignee": assignee, "surveyor": surveyor})
            return dict(self._claim(conn, claim_id))

    def add_evidence(self, actor: str, role: str, claim_id: int, sha256: str,
                     filename: str, source: str) -> dict[str, Any]:
        actor = actor_id(actor)
        require_role(role, {"intake", "adjuster", "surveyor", "supervisor"}, "添加损失证据")
        sha256 = sha256.strip().lower()
        if len(sha256) != 64 or any(ch not in "0123456789abcdef" for ch in sha256):
            raise DomainError("证据哈希必须是 64 位 SHA-256 十六进制")
        if not filename.strip() or not source.strip():
            raise DomainError("证据文件名和来源不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            claim = self._claim(conn, claim_id)
            if claim["status"] in TERMINAL:
                raise DomainError("已结束案件不能添加证据", 409)
            existing = conn.execute("SELECT * FROM evidence WHERE claim_id=? AND sha256=?", (claim_id, sha256)).fetchone()
            if existing:
                return dict(existing)
            cur = conn.execute(
                "INSERT INTO evidence(claim_id,sha256,filename,source,submitter,created_at) VALUES(?,?,?,?,?,?)",
                (claim_id, sha256, filename.strip(), source.strip(), actor, utcnow()),
            )
            hash_claims = [r["claim_id"] for r in conn.execute(
                "SELECT DISTINCT claim_id FROM evidence WHERE sha256=?", (sha256,)
            ).fetchall()]
            suspicious = len(hash_claims) >= 3
            if suspicious:
                for cid in hash_claims:
                    conn.execute(
                        "UPDATE claims SET fraud_score=MAX(fraud_score,0.95),status='escalated',version=version+1,updated_at=? WHERE id=? AND status<>'duplicate'",
                        (utcnow(), cid),
                    )
                self._audit(conn, claim_id, actor, "evidence.bulk_reuse_detected", {"sha256": sha256, "claim_ids": hash_claims})
            self._audit(conn, claim_id, actor, "evidence.added", {"evidence_id": cur.lastrowid, "suspicious": suspicious})
            return {"evidence": dict(conn.execute("SELECT * FROM evidence WHERE id=?", (cur.lastrowid,)).fetchone()), "bulk_reuse": suspicious, "affected_claims": hash_claims}

    def record_survey(self, actor: str, role: str, claim_id: int, damage_ratio: float,
                      findings: str, recommendation: str, expected_version: int) -> dict[str, Any]:
        actor = actor_id(actor)
        require_role(role, {"adjuster", "surveyor"}, "录入查勘结果")
        try:
            damage_ratio = float(damage_ratio)
        except (TypeError, ValueError) as exc:
            raise DomainError("损失比例必须是数值") from exc
        if not 0 <= damage_ratio <= 1 or not findings.strip() or not recommendation.strip():
            raise DomainError("损失比例或查勘内容无效")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            claim = self._claim(conn, claim_id)
            if claim["status"] not in {"assigned", "escalated"}:
                raise DomainError("当前状态不能录入查勘", 409)
            if claim["version"] != int(expected_version):
                raise DomainError("案件已变化，请刷新后重试", 409)
            if claim["assignee"] != actor and claim["surveyor"] != actor:
                raise DomainError("只有被分配的查勘人员可以录入结果", 403)
            if claim["status"] == "escalated" and claim["fraud_score"] >= 0.8:
                raise DomainError("高风险案件须先完成复核降险，不能直接提交查勘", 409)
            conn.execute(
                "INSERT INTO survey_notes(claim_id,surveyor,damage_ratio,findings,recommendation,created_at) VALUES(?,?,?,?,?,?)",
                (claim_id, actor, damage_ratio, findings.strip(), recommendation.strip(), utcnow()),
            )
            conn.execute("UPDATE claims SET status='survey',version=version+1,updated_at=? WHERE id=?", (utcnow(), claim_id))
            self._audit(conn, claim_id, actor, "survey.recorded", {"damage_ratio": damage_ratio, "recommendation": recommendation})
            return dict(self._claim(conn, claim_id))

    def submit_review(self, actor: str, role: str, claim_id: int, expected_version: int) -> dict[str, Any]:
        actor = actor_id(actor)
        require_role(role, {"adjuster", "surveyor"}, "提交核损")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            claim = self._claim(conn, claim_id)
            if claim["status"] != "survey":
                raise DomainError("只有已查勘案件可以提交核损", 409)
            if claim["version"] != int(expected_version):
                raise DomainError("案件已变化，请刷新后重试", 409)
            if not conn.execute("SELECT 1 FROM survey_notes WHERE claim_id=?", (claim_id,)).fetchone():
                raise DomainError("缺少查勘记录", 409)
            conn.execute("UPDATE claims SET status='review',version=version+1,updated_at=? WHERE id=?", (utcnow(), claim_id))
            self._audit(conn, claim_id, actor, "claim.review_submitted", {})
            return dict(self._claim(conn, claim_id))

    def _advance_eligible(self, claim: sqlite3.Row, limit: float) -> str | None:
        """业务判断：该案件当前是否允许占用预付额度。None 表示允许。"""
        if claim["status"] in {"duplicate", "approved", "rejected", "closed"}:
            return "当前案件状态不能预付"
        if not claim["urgent_need"]:
            return "非紧急案件不能预付"
        if claim["fraud_score"] >= 0.8:
            return "高风险案件不能预付"
        if claim["duplicate_of"]:
            return "重复报案不能预付"
        return None

    def _pump_waiting(self, conn: sqlite3.Connection, event_id: str, actor: str) -> list[dict[str, Any]]:
        """额度释放后按申请先后续放：合格的队头申请占用剩余额度并放款。

        遇到额度仍不足的队头即停止（更晚申请不得插队）；
        排队期间丧失预付资格的申请直接取消，继续处理后面的申请。
        """
        released_now = []
        while True:
            row = AdvancePool.head_waiting(conn, event_id)
            if row is None:
                break
            target = self._claim(conn, row["claim_id"])
            limit = target["estimated_loss"] * 0.2
            reason = self._advance_eligible(target, limit)
            now = utcnow()
            if reason is not None or target["emergency_advance"] + row["amount"] > limit + 1e-9:
                detail = reason or "累计预付将超过预估损失的20%"
                AdvancePool.cancel(conn, row["id"], detail, now)
                self._audit(conn, target["id"], actor, "advance.waiting_cancelled",
                            {"reservation_id": row["id"], "reason": detail})
                conn.execute("UPDATE claims SET version=version+1,updated_at=? WHERE id=?", (now, target["id"]))
                continue
            if AdvancePool.remaining(conn, event_id) + 1e-9 < row["amount"]:
                break  # 队头仍放不下，后续申请即使更小也不能越过它
            conn.execute(
                "INSERT INTO payments(claim_id,kind,amount,approved_by,reference,created_at) VALUES(?,?,?,?,?,?)",
                (target["id"], "emergency_advance", row["amount"], actor, row["payment_reference"], now),
            )
            AdvancePool.mark_held(conn, row["id"], now)
            conn.execute(
                "UPDATE claims SET emergency_advance=emergency_advance+?,version=version+1,updated_at=? WHERE id=?",
                (row["amount"], now, target["id"]),
            )
            self._audit(conn, target["id"], actor, "advance.released_from_queue",
                        {"amount": row["amount"], "reference": row["payment_reference"], "reservation_id": row["id"]})
            released_now.append(dict(conn.execute(
                "SELECT * FROM advance_reservations WHERE id=?", (row["id"],)
            ).fetchone()))
        return released_now

    def _release_and_pump(self, conn: sqlite3.Connection, claim: sqlite3.Row,
                          actor: str, reason: str) -> list[dict[str, Any]]:
        released = AdvancePool.release_for_claim(conn, claim["id"], reason, utcnow())
        if released > 0:
            self._audit(conn, claim["id"], actor, "advance.quota_released",
                        {"amount": released, "reason": reason, "event_id": claim["event_id"]})
        return self._pump_waiting(conn, claim["event_id"], actor)

    def emergency_advance(self, actor: str, role: str, claim_id: int, amount: float,
                          expected_version: int, reference: str) -> dict[str, Any]:
        actor = actor_id(actor)
        require_role(role, {"supervisor"}, "批准紧急预付")
        try:
            amount = float(amount)
        except (TypeError, ValueError) as exc:
            raise DomainError("预付金额必须是数值") from exc
        reference = (reference or "").strip()
        if not reference:
            raise DomainError("付款参考号不能为空")
        if amount <= 0:
            raise DomainError("预付金额必须大于0", 409)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            claim = self._claim(conn, claim_id)
            if claim["version"] != int(expected_version):
                raise DomainError("案件已变化，请刷新后重试", 409)
            limit = claim["estimated_loss"] * 0.2
            ineligible = self._advance_eligible(claim, limit)
            if ineligible is not None:
                raise DomainError(ineligible, 409)
            if amount > limit:
                raise DomainError("预付金额必须不超过预估损失的20%", 409)
            if claim["emergency_advance"] + amount > limit + 1e-9:
                raise DomainError("累计预付超过上限", 409)
            if AdvancePool.reference_busy(conn, reference):
                raise DomainError("付款参考号已存在", 409)
            if AdvancePool.has_active_waiting(conn, claim_id):
                raise DomainError("该案件已有待放行申请，不能重复提交", 409)
            quota = AdvancePool.quota_row(conn, claim["event_id"])
            if quota is None:
                raise DomainError("事件尚未录入紧急预付总额度，请先由主管设置", 409)
            now = utcnow()
            reservation = self.pool.reserve(conn, claim["event_id"], claim_id, amount, reference, now)
            result: dict[str, Any]
            if reservation["status"] == HELD:
                conn.execute(
                    "INSERT INTO payments(claim_id,kind,amount,approved_by,reference,created_at) VALUES(?,?,?,?,?,?)",
                    (claim_id, "emergency_advance", amount, actor, reference, now),
                )
                conn.execute("UPDATE claims SET emergency_advance=emergency_advance+?,version=version+1,updated_at=? WHERE id=?", (amount, now, claim_id))
                self._audit(conn, claim_id, actor, "payment.emergency_advance",
                            {"amount": amount, "reference": reference, "reservation_id": reservation["id"]})
                result = dict(self._claim(conn, claim_id))
                result["advance_status"] = HELD
                result["advance_shortfall"] = 0.0
            else:
                # 剩余额度不足：停在待放行，不产生付款、不占用案件预付余额
                shortfall = amount - AdvancePool.remaining(conn, claim["event_id"])
                conn.execute("UPDATE claims SET version=version+1,updated_at=? WHERE id=?", (now, claim_id))
                self._audit(conn, claim_id, actor, "advance.waiting",
                            {"amount": amount, "reference": reference, "reservation_id": reservation["id"],
                             "shortfall": round(max(0.0, shortfall), 2)})
                result = dict(self._claim(conn, claim_id))
                result["advance_status"] = WAITING
                result["advance_shortfall"] = round(max(0.0, shortfall), 2)
            result["reservation_id"] = reservation["id"]
            result["quota"] = AdvancePool.summary(conn, claim["event_id"])
            return result

    def finalize_claim(self, actor: str, role: str, claim_id: int, decision: str,
                       payout: float, expected_version: int, reason: str = "") -> dict[str, Any]:
        actor = actor_id(actor)
        require_role(role, {"supervisor"}, "最终核定")
        if decision not in {"approve", "reject"}:
            raise DomainError("核定决定无效")
        try:
            payout = float(payout)
        except (TypeError, ValueError) as exc:
            raise DomainError("核定金额必须是数值") from exc
        if payout < 0:
            raise DomainError("核定金额不能为负数")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            claim = self._claim(conn, claim_id)
            if claim["status"] != "review":
                raise DomainError("只有待复核案件可以最终核定", 409)
            if claim["version"] != int(expected_version):
                raise DomainError("案件已变化，请刷新后重试", 409)
            if claim["duplicate_of"]:
                raise DomainError("重复报案不能核定赔付", 409)
            if claim["fraud_score"] >= 0.8 and decision == "approve":
                raise DomainError("高风险案件未解除风险标记，不能赔付", 409)
            if decision == "approve" and payout > claim["estimated_loss"]:
                raise DomainError("核定金额不能超过预估损失", 409)
            if decision == "reject" and not reason.strip():
                raise DomainError("拒赔必须填写理由", 409)
            status = "approved" if decision == "approve" else "rejected"
            conn.execute(
                "UPDATE claims SET status=?,final_payout=?,version=version+1,updated_at=? WHERE id=? AND version=?",
                (status, payout if decision == "approve" else 0, utcnow(), claim_id, expected_version),
            )
            self._audit(conn, claim_id, actor, "claim.finalized", {"decision": decision, "payout": payout, "reason": reason.strip()})
            # 核定完成（含拒赔）：释放本案件未用占用并续放后续待放行申请
            released = self._release_and_pump(conn, claim, actor, "核定完成" if decision == "approve" else "案件拒赔")
            result = dict(self._claim(conn, claim_id))
            result["released_advances"] = released
            result["quota"] = AdvancePool.summary(conn, claim["event_id"])
            return result

    def return_claim(self, actor: str, role: str, claim_id: int, expected_version: int,
                     reason: str = "") -> dict[str, Any]:
        """主管退回案件（查勘/核损阶段），案件回到待分配，预付占用一并释放。"""
        actor = actor_id(actor)
        require_role(role, {"supervisor"}, "退回案件")
        if not reason.strip():
            raise DomainError("退回必须填写理由")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            claim = self._claim(conn, claim_id)
            if claim["status"] not in {"assigned", "survey", "review", "escalated"}:
                raise DomainError("当前状态不能退回", 409)
            if claim["version"] != int(expected_version):
                raise DomainError("案件已变化，请刷新后重试", 409)
            conn.execute(
                """UPDATE claims SET status=?,assignee=NULL,surveyor=NULL,version=version+1,updated_at=?
                   WHERE id=?""",
                (RETURNED_STATE, utcnow(), claim_id),
            )
            self._audit(conn, claim_id, actor, "claim.returned", {"reason": reason.strip()})
            released = self._release_and_pump(conn, claim, actor, "案件退回")
            result = dict(self._claim(conn, claim_id))
            result["released_advances"] = released
            result["quota"] = AdvancePool.summary(conn, claim["event_id"])
            return result

    def set_advance_quota(self, actor: str, role: str, event_id: str, total_amount: float,
                          expected_version: int | None = None) -> dict[str, Any]:
        actor = actor_id(actor)
        require_role(role, {"supervisor"}, "录入紧急预付总额度")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                quota = self.pool.set_quota(conn, event_id, total_amount, actor, utcnow(), expected_version)
            except ValueError as exc:
                raise DomainError(str(exc)) from exc
            except LookupError as exc:
                raise DomainError(str(exc), 409) from exc
            self._audit(conn, None, actor, "advance.quota_set",
                        {"event_id": quota["event_id"], "total_amount": quota["total_amount"],
                         "version": quota["version"]})
            return AdvancePool.summary(conn, quota["event_id"])

    def advance_quota_view(self, role: str, event_id: str | None = None) -> Any:
        if role not in {"intake", "supervisor", "adjuster", "surveyor", "auditor"}:
            raise DomainError("角色无权查看预付额度", 403)
        with self.connect() as conn:
            if event_id:
                summary = AdvancePool.summary(conn, event_id.strip())
                if summary is None:
                    raise DomainError("该事件尚未录入紧急预付总额度", 404)
                return summary
            return {"events": AdvancePool.list_events(conn)}

    def queue(self, role: str = "viewer", actor: str = "") -> list[dict[str, Any]]:
        if role not in {"intake", "supervisor", "adjuster", "surveyor", "auditor"}:
            raise DomainError("角色无权查看理赔队列", 403)
        with self.connect() as conn:
            if role in {"adjuster", "surveyor"}:
                rows = conn.execute(
                    "SELECT * FROM claims WHERE (assignee=? OR surveyor=?) AND status NOT IN ('approved','rejected','duplicate') ORDER BY priority_score DESC,created_at",
                    (actor, actor),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM claims ORDER BY priority_score DESC,created_at").fetchall()
        return [dict(r) for r in rows]

    def state(self, actor: str = "", role: str = "viewer") -> dict[str, Any]:
        allowed = role in {"intake", "supervisor", "adjuster", "surveyor", "auditor"}
        if not allowed:
            return {"claims": [], "evidence": [], "payments": [], "timeline": [], "access_limited": True}
        with self.connect() as conn:
            if role in {"adjuster", "surveyor"}:
                claims = [dict(r) for r in conn.execute(
                    "SELECT * FROM claims WHERE assignee=? OR surveyor=? ORDER BY priority_score DESC,id DESC", (actor, actor)
                ).fetchall()]
            else:
                claims = [dict(r) for r in conn.execute("SELECT * FROM claims ORDER BY priority_score DESC,id DESC").fetchall()]
            ids = [c["id"] for c in claims]
            if ids:
                marks = ",".join("?" for _ in ids)
                evidence = [dict(r) for r in conn.execute("SELECT * FROM evidence WHERE claim_id IN (%s) ORDER BY id DESC" % marks, ids).fetchall()]
                payments = [dict(r) for r in conn.execute("SELECT * FROM payments WHERE claim_id IN (%s) ORDER BY id DESC" % marks, ids).fetchall()]
                timeline = [dict(r) for r in conn.execute("SELECT * FROM timeline WHERE claim_id IN (%s) ORDER BY id DESC LIMIT 300" % marks, ids).fetchall()]
            else:
                evidence, payments, timeline = [], [], []
        return {"claims": claims, "evidence": evidence, "payments": payments, "timeline": timeline, "access_limited": False}

    def seed_demo(self) -> dict[str, Any]:
        with self.connect() as conn:
            if conn.execute("SELECT COUNT(*) AS c FROM claims").fetchone()["c"]:
                return {"seeded": False, "reason": "已有数据"}
        c1 = self.create_claim("intake-demo", "intake", "CLM-DEMO-001", "TY2026", "沿海A区", "洪水", "P-1001", "R-01", 30.1, 121.2, 500000, True, True)
        self.create_claim("intake-demo", "intake", "CLM-DEMO-002", "TY2026", "沿海A区", "洪水", "P-1002", "R-02", 30.2, 121.3, 240000, False, False)
        self.set_advance_quota("sup-demo", "supervisor", "TY2026", 200000)
        return {"seeded": True, "first_claim_id": c1["id"]}


class ApiHandler(BaseHTTPRequestHandler):
    service: CatastropheClaimService

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _headers(self) -> tuple[str, str]:
        return self.headers.get("X-User", ""), self.headers.get("X-Role", "viewer")

    def _json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 2_000_000:
            raise DomainError("请求体过大", 413)
        if not length:
            return {}
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DomainError("请求体不是有效 JSON") from exc
        if not isinstance(value, dict):
            raise DomainError("JSON 请求体必须是对象")
        return value

    def do_GET(self) -> None:
        try:
            parsed = urlparse(self.path)
            path, query = parsed.path, parse_qs(parsed.query)
            if path in {"/", "/index.html"}:
                body = (ROOT / "static" / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/health":
                self._send(200, {"status": "ok", "service": "catastrophe-claims"})
            elif path == "/api/state":
                self._send(200, self.service.state(*self._headers()))
            elif path == "/api/queue":
                actor, role = self._headers()
                self._send(200, {"queue": self.service.queue(role, actor)})
            elif path == "/api/advance-quota":
                actor, role = self._headers()
                self._send(200, self.service.advance_quota_view(role, query.get("event_id", [""])[0]))
            else:
                self._send(404, {"error": "接口不存在"})
        except DomainError as exc:
            self._send(exc.status, {"error": str(exc)})

    def do_POST(self) -> None:
        try:
            path, data, (actor, role) = urlparse(self.path).path, self._json(), self._headers()
            if path == "/api/claims":
                result = self.service.create_claim(actor, role, **data)
            elif path == "/api/claims/triage":
                result = self.service.triage_claim(actor, role, **data)
            elif path == "/api/claims/assign":
                result = self.service.assign_claim(actor, role, **data)
            elif path == "/api/evidence":
                result = self.service.add_evidence(actor, role, **data)
            elif path == "/api/claims/survey":
                result = self.service.record_survey(actor, role, **data)
            elif path == "/api/claims/submit-review":
                result = self.service.submit_review(actor, role, **data)
            elif path == "/api/claims/emergency-advance":
                result = self.service.emergency_advance(actor, role, **data)
            elif path == "/api/claims/finalize":
                result = self.service.finalize_claim(actor, role, **data)
            elif path == "/api/claims/return":
                result = self.service.return_claim(actor, role, **data)
            elif path == "/api/advance-quota":
                result = self.service.set_advance_quota(actor, role, **data)
            else:
                raise DomainError("接口不存在", 404)
            self._send(201, result)
        except DomainError as exc:
            self._send(exc.status, {"error": str(exc)})
        except (KeyError, TypeError, ValueError) as exc:
            self._send(400, {"error": "请求参数错误: %s" % exc})
        except Exception as exc:
            self._send(500, {"error": "服务器内部错误", "detail": str(exc)})

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def serve(service: CatastropheClaimService, host: str, port: int) -> None:
    ApiHandler.service = service
    server = ThreadingHTTPServer((host, port), ApiHandler)
    print("Catastrophe claim service listening on http://%s:%s" % (host, port))
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="巨灾保险理赔调度服务")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8207)
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--seed", action="store_true")
    args = parser.parse_args()
    service = CatastropheClaimService(args.db)
    if args.init:
        print(json.dumps(service.seed_demo() if args.seed else {"initialized": True, "db": args.db}, ensure_ascii=False))
        return
    serve(service, args.host, args.port)


if __name__ == "__main__":
    main()
