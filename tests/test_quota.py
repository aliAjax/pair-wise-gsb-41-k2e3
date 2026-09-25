import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import CatastropheClaimService, DomainError  # noqa: E402


class EventAdvanceQuotaTest(unittest.TestCase):
    """同一灾害事件的紧急预付共享额度池：按申请先后占用、不足则待放行、结束后释放。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "quota.db"
        self.service = CatastropheClaimService(self.db)
        self.service.set_event_pool("sup", "supervisor", "TY-2026", 100000)

    def tearDown(self):
        self.tmp.cleanup()

    def claim(self, number, loss=200000, urgent=True, event="TY-2026"):
        return self.service.create_claim(
            "in1", "intake", number, event, "A区", "台风", "P-" + number, "R-" + number,
            30.1, 121.1, loss, urgent, False,
        )

    def advance(self, claim, amount, ref):
        # 返回值是案件字典（附带 advance 信息），version 已是最新，可直接链式传递
        return self.service.emergency_advance("sup", "supervisor", claim["id"], amount, claim["version"], ref)

    def to_review(self, claim):
        claim = self.service.triage_claim("sup", "supervisor", claim["id"], claim["version"], 0.1, False)
        claim = self.service.assign_claim("sup", "supervisor", claim["id"], "adj1", claim["version"])
        claim = self.service.record_survey("adj1", "adjuster", claim["id"], 0.5, "结构受损", "赔付", claim["version"])
        return self.service.submit_review("adj1", "adjuster", claim["id"], claim["version"])

    def detail(self, event="TY-2026"):
        return self.service.advance_pool_detail("supervisor", event)

    def fill_pool_and_pending(self):
        """两案各放行 40000（占满 8 万），第三案 40000 停在待放行、缺口 2 万。"""
        c1 = self.advance(self.claim("Q-001"), 40000, "REF-1")
        c2 = self.advance(self.claim("Q-002"), 40000, "REF-2")
        c3 = self.advance(self.claim("Q-003"), 40000, "REF-3")
        self.assertEqual("approved", c1["advance"]["status"])
        self.assertEqual("approved", c2["advance"]["status"])
        self.assertEqual("pending", c3["advance"]["status"])
        return c1, c2, c3

    def test_fifo_occupation_then_pending_with_gap(self):
        _, _, c3 = self.fill_pool_and_pending()
        self.assertEqual(20000, c3["advance"]["gap"])
        self.assertEqual(0, c3["emergency_advance"])
        detail = self.detail()
        self.assertEqual(80000, detail["occupied"])
        self.assertEqual(20000, detail["available"])
        self.assertEqual(1, len(detail["pending"]))
        self.assertEqual(20000, detail["pending"][0]["gap"])
        self.assertEqual(2, len(self.service.state("sup", "supervisor")["payments"]))

    def test_head_of_line_blocking(self):
        big1 = self.advance(self.claim("Q-010", loss=300000), 60000, "REF-10")
        self.assertEqual("approved", big1["advance"]["status"])
        big2 = self.advance(self.claim("Q-011", loss=300000), 60000, "REF-11")
        self.assertEqual("pending", big2["advance"]["status"])
        self.assertEqual(20000, big2["advance"]["gap"])
        small = self.advance(self.claim("Q-012", loss=100000), 20000, "REF-12")
        self.assertEqual("pending", small["advance"]["status"])
        detail = self.detail()
        self.assertEqual(60000, detail["occupied"])
        self.assertEqual(2, len(detail["pending"]))
        self.assertEqual(20000, detail["pending"][1]["gap"])

    def test_finalize_releases_hold_and_promotes_next(self):
        c1, _, c3 = self.fill_pool_and_pending()
        c1 = self.to_review(c1)
        c1 = self.service.finalize_claim("sup", "supervisor", c1["id"], "approve", 150000, c1["version"])
        self.assertEqual("approved", c1["status"])
        detail = self.detail()
        self.assertEqual(0, len(detail["pending"]))
        self.assertEqual(80000, detail["occupied"])
        self.assertEqual({"Q-002", "Q-003"}, {h["claim_no"] for h in detail["holds"]})
        released = [h for h in detail["history"] if h["status"] == "released"]
        self.assertEqual(1, len(released))
        self.assertEqual("核定完成释放", released[0]["close_reason"])
        self.assertEqual(3, len(self.service.state("sup", "supervisor")["payments"]))
        c3_now = [c for c in self.service.state("sup", "supervisor")["claims"] if c["claim_no"] == "Q-003"][0]
        self.assertEqual(40000, c3_now["emergency_advance"])

    def test_reject_releases_hold_and_promotes_next(self):
        c1, _, _ = self.fill_pool_and_pending()
        c1 = self.to_review(c1)
        c1 = self.service.finalize_claim("sup", "supervisor", c1["id"], "reject", 0, c1["version"], "不属于保险责任")
        self.assertEqual("rejected", c1["status"])
        detail = self.detail()
        self.assertEqual(0, len(detail["pending"]))
        self.assertEqual(80000, detail["occupied"])
        released = [h for h in detail["history"] if h["status"] == "released"]
        self.assertEqual("拒赔释放", released[0]["close_reason"])

    def test_return_releases_hold_and_promotes_next(self):
        c1, _, _ = self.fill_pool_and_pending()
        c1 = self.to_review(c1)
        c1 = self.service.return_claim("sup", "supervisor", c1["id"], c1["version"], "查勘材料不全")
        self.assertEqual("assigned", c1["status"])
        detail = self.detail()
        self.assertEqual(0, len(detail["pending"]))
        self.assertEqual(80000, detail["occupied"])
        released = [h for h in detail["history"] if h["status"] == "released"]
        self.assertEqual("退回释放", released[0]["close_reason"])

    def test_pool_increase_promotes_pending(self):
        self.fill_pool_and_pending()
        result = self.service.set_event_pool("sup", "supervisor", "TY-2026", 150000)
        self.assertEqual(1, len(result["promoted"]))
        detail = self.detail()
        self.assertEqual(120000, detail["occupied"])
        self.assertEqual(0, len(detail["pending"]))
        self.assertEqual(3, len(self.service.state("sup", "supervisor")["payments"]))

    def test_pending_cancelled_when_case_finalized(self):
        _, _, c3 = self.fill_pool_and_pending()
        c3 = self.to_review(c3)
        c3 = self.service.finalize_claim("sup", "supervisor", c3["id"], "approve", 100000, c3["version"])
        self.assertEqual("approved", c3["status"])
        detail = self.detail()
        self.assertEqual(0, len(detail["pending"]))
        cancelled = [h for h in detail["history"] if h["status"] == "cancelled"]
        self.assertEqual(1, len(cancelled))
        self.assertEqual("Q-003", cancelled[0]["claim_no"])
        self.assertEqual(80000, detail["occupied"])

    def test_pool_required_before_advance(self):
        claim = self.claim("Q-020", event="TY-NEW")
        with self.assertRaises(DomainError) as ctx:
            self.advance(claim, 10000, "REF-20")
        self.assertEqual(409, ctx.exception.status)
        self.assertIn("尚未录入", str(ctx.exception))

    def test_pool_permission_and_validation(self):
        with self.assertRaises(DomainError) as ctx:
            self.service.set_event_pool("adj1", "adjuster", "EV-X", 5000)
        self.assertEqual(403, ctx.exception.status)
        with self.assertRaises(DomainError):
            self.service.set_event_pool("sup", "supervisor", "EV-X", 0)
        c1 = self.advance(self.claim("Q-021"), 40000, "REF-21")
        self.assertEqual("approved", c1["advance"]["status"])
        with self.assertRaises(DomainError) as ctx2:
            self.service.set_event_pool("sup", "supervisor", "TY-2026", 30000)
        self.assertEqual(409, ctx2.exception.status)

    def test_twenty_percent_limit_still_enforced(self):
        claim = self.claim("Q-030", loss=100000)
        with self.assertRaises(DomainError) as ctx:
            self.advance(claim, 20001, "REF-30")
        self.assertEqual(409, ctx.exception.status)

    def test_second_pending_request_blocked(self):
        self.advance(self.claim("Q-040", loss=300000), 60000, "REF-40")
        pending = self.advance(self.claim("Q-041", loss=300000), 60000, "REF-41")
        self.assertEqual("pending", pending["advance"]["status"])
        with self.assertRaises(DomainError) as ctx:
            self.advance(pending, 10000, "REF-42")
        self.assertEqual(409, ctx.exception.status)

    def test_duplicate_reference_rejected(self):
        self.advance(self.claim("Q-050"), 40000, "REF-50")
        with self.assertRaises(DomainError) as ctx:
            self.advance(self.claim("Q-051"), 40000, "REF-50")
        self.assertEqual(409, ctx.exception.status)

    def test_return_requires_supervisor_and_reason(self):
        c1 = self.advance(self.claim("Q-060"), 40000, "REF-60")
        c1 = self.to_review(c1)
        with self.assertRaises(DomainError) as ctx:
            self.service.return_claim("adj1", "adjuster", c1["id"], c1["version"], "材料不全")
        self.assertEqual(403, ctx.exception.status)
        with self.assertRaises(DomainError):
            self.service.return_claim("sup", "supervisor", c1["id"], c1["version"], "")

    def test_reopen_keeps_pool_state(self):
        self.fill_pool_and_pending()
        reopened = CatastropheClaimService(self.db)
        detail = reopened.advance_pool_detail("auditor", "TY-2026")
        self.assertEqual(100000, detail["pool"]["total_quota"])
        self.assertEqual(80000, detail["occupied"])
        self.assertEqual(1, len(detail["pending"]))
        self.assertEqual(20000, detail["pending"][0]["gap"])
        pools = reopened.list_advance_pools("auditor")
        self.assertEqual(1, len(pools))
        self.assertEqual(1, pools[0]["pending_count"])


if __name__ == "__main__":
    unittest.main()
