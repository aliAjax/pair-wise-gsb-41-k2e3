import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import CatastropheClaimService, DomainError  # noqa: E402


class CatastropheClaimFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = CatastropheClaimService(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.tmp.cleanup()

    def claim(self, number, policy="P-1", lat=30.1, loss=500000, urgent=False):
        return self.service.create_claim(
            "intake1", "intake", number, "TY-2026", "A区", "flood", policy, "R-" + number,
            lat, 121.1, loss, urgent, True,
        )

    def urgent_ready(self, number, policy, loss=500000, lat=30.1):
        """建案、分级、分配，得到一个可申请紧急预付（预估损失20%）的案件。"""
        claim = self.claim(number, policy=policy, lat=lat, loss=loss, urgent=True)
        claim = self.service.triage_claim("sup1", "supervisor", claim["id"], claim["version"], 0.1)
        claim = self.service.assign_claim("sup1", "supervisor", claim["id"], "adjuster1", claim["version"])
        return claim

    def test_complete_claim_lifecycle_with_emergency_advance(self):
        claim = self.claim("C-001", urgent=True)
        claim = self.service.triage_claim("sup1", "supervisor", claim["id"], claim["version"], 0.1, True)
        claim = self.service.assign_claim("sup1", "supervisor", claim["id"], "adjuster1", claim["version"], "survey1")
        self.service.set_advance_quota("sup1", "supervisor", "TY-2026", 200000)
        claim = self.service.emergency_advance("sup1", "supervisor", claim["id"], 50000, claim["version"], "ADV-001")
        self.assertEqual("held", claim["advance_status"])
        evidence = self.service.add_evidence("adjuster1", "adjuster", claim["id"], "a" * 64, "loss.jpg", "field")
        self.assertFalse(evidence["bulk_reuse"])
        claim = self.service.record_survey("adjuster1", "adjuster", claim["id"], 0.6, "结构受损", "部分赔付", claim["version"])
        claim = self.service.submit_review("adjuster1", "adjuster", claim["id"], claim["version"])
        claim = self.service.finalize_claim("sup1", "supervisor", claim["id"], "approve", 280000, claim["version"])
        self.assertEqual("approved", claim["status"])
        self.assertEqual(280000, claim["final_payout"])
        self.assertEqual(1, len(self.service.state("sup1", "supervisor")["payments"]))

    def test_duplicate_and_version_conflict(self):
        first = self.claim("C-010", policy="P-10")
        second = self.claim("C-011", policy="P-10", lat=30.11)
        self.assertEqual("duplicate", second["status"])
        self.assertEqual(first["id"], second["duplicate_of"])
        triaged = self.service.triage_claim("sup1", "supervisor", first["id"], first["version"])
        with self.assertRaises(DomainError) as ctx:
            self.service.assign_claim("sup1", "supervisor", first["id"], "adjuster1", first["version"])
        self.assertEqual(409, ctx.exception.status)
        assigned = self.service.assign_claim("sup1", "supervisor", first["id"], "adjuster1", triaged["version"])
        self.assertEqual("assigned", assigned["status"])

    def test_bulk_forged_evidence_and_permissions(self):
        claims = [self.claim("C-%03d" % i, policy="P-%03d" % i, lat=30 + i / 100) for i in range(1, 4)]
        shared = "b" * 64
        last = None
        for claim in claims:
            last = self.service.add_evidence("intake1", "intake", claim["id"], shared, "same.pdf", "batch-import")
        self.assertTrue(last["bulk_reuse"])
        self.assertGreaterEqual(len(last["affected_claims"]), 3)
        with self.assertRaises(DomainError) as ctx:
            self.service.queue("viewer", "viewer")
        self.assertEqual(403, ctx.exception.status)
        with self.assertRaises(DomainError) as ctx2:
            self.service.add_evidence("adjuster1", "adjuster", claims[0]["id"], "not-a-hash", "x", "field")
        self.assertEqual(400, ctx2.exception.status)

    def test_advance_requires_event_quota(self):
        claim = self.urgent_ready("C-100", "P-100")
        with self.assertRaises(DomainError) as ctx:
            self.service.emergency_advance("sup1", "supervisor", claim["id"], 50000, claim["version"], "ADV-100")
        self.assertEqual(409, ctx.exception.status)
        # 无额度时不会产生付款
        self.assertEqual(0, len(self.service.state("sup1", "supervisor")["payments"]))

    def test_quota_fifo_queue_shortfall_and_release_on_finalize(self):
        # 总额度 15万；三个案件20%上限分别 10万、10万、6万
        c1 = self.urgent_ready("C-201", "P-201", loss=500000, lat=30.11)
        c2 = self.urgent_ready("C-202", "P-202", loss=500000, lat=30.12)
        c3 = self.urgent_ready("C-203", "P-203", loss=300000, lat=30.13)
        self.service.set_advance_quota("sup1", "supervisor", "TY-2026", 150000)

        r1 = self.service.emergency_advance("sup1", "supervisor", c1["id"], 100000, c1["version"], "ADV-201")
        self.assertEqual("held", r1["advance_status"])
        r2 = self.service.emergency_advance("sup1", "supervisor", c2["id"], 100000, c2["version"], "ADV-202")
        # 只剩5万，缺5万，停在待放行
        self.assertEqual("waiting", r2["advance_status"])
        self.assertAlmostEqual(50000, r2["advance_shortfall"], places=2)
        r3 = self.service.emergency_advance("sup1", "supervisor", c3["id"], 60000, c3["version"], "ADV-203")
        self.assertEqual("waiting", r3["advance_status"])

        view = self.service.advance_quota_view("supervisor", "TY-2026")
        self.assertEqual(150000, view["total_amount"])
        self.assertEqual(100000, view["held_total"])
        self.assertEqual(50000, view["remaining"])
        self.assertEqual([100000, 60000], [w["amount"] for w in view["waiting"]])
        self.assertAlmostEqual(50000, view["waiting"][0]["shortfall"], places=2)
        self.assertAlmostEqual(60000, view["waiting"][1]["shortfall"], places=2)
        self.assertAlmostEqual(110000, view["queue_shortfall"], places=2)
        # 待放行不产生付款、不占用案件预付余额
        self.assertEqual(1, len(self.service.state("sup1", "supervisor")["payments"]))

        # c1 走完核定完成：释放10万 -> 队头 c2 10万放行走完
        c1 = self.service.record_survey("adjuster1", "adjuster", c1["id"], 0.5, "受损", "赔付", r1["version"])
        c1 = self.service.submit_review("adjuster1", "adjuster", c1["id"], c1["version"])
        c1 = self.service.finalize_claim("sup1", "supervisor", c1["id"], "approve", 250000, c1["version"])
        self.assertEqual(1, len(c1["released_advances"]))
        view = self.service.advance_quota_view("supervisor", "TY-2026")
        self.assertEqual(100000, view["held_total"])
        self.assertEqual(1, len(view["held"]))
        self.assertEqual(c2["id"], view["held"][0]["claim_id"])
        self.assertEqual([c3["id"]], [w["claim_id"] for w in view["waiting"]])
        self.assertEqual(2, len(self.service.state("sup1", "supervisor")["payments"]))

    def test_reject_releases_holding_and_cancels_waiting(self):
        c1 = self.urgent_ready("C-301", "P-301", loss=500000, lat=30.21)
        c2 = self.urgent_ready("C-302", "P-302", loss=300000, lat=30.22)
        self.service.set_advance_quota("sup1", "supervisor", "TY-2026", 100000)
        r1 = self.service.emergency_advance("sup1", "supervisor", c1["id"], 100000, c1["version"], "ADV-301")
        self.assertEqual("held", r1["advance_status"])
        r2 = self.service.emergency_advance("sup1", "supervisor", c2["id"], 60000, c2["version"], "ADV-302")
        self.assertEqual("waiting", r2["advance_status"])

        # c1 拒赔：占用归还；c2 仍在查勘前流程但满足续放，6万从新释放额度放行走完
        c1 = self.service.record_survey("adjuster1", "adjuster", c1["id"], 0.4, "受损", "拒", r1["version"])
        c1 = self.service.submit_review("adjuster1", "adjuster", c1["id"], c1["version"])
        c1 = self.service.finalize_claim("sup1", "supervisor", c1["id"], "reject", 0, c1["version"], "虚假损失")
        self.assertEqual("rejected", c1["status"])
        view = self.service.advance_quota_view("supervisor", "TY-2026")
        self.assertEqual([c2["id"]], [h["claim_id"] for h in view["held"]])
        self.assertEqual(0, len(view["waiting"]))
        self.assertEqual(40000, view["remaining"])

    def test_return_claim_releases_quota(self):
        c1 = self.urgent_ready("C-401", "P-401", loss=500000, lat=30.31)
        c2 = self.urgent_ready("C-402", "P-402", loss=200000, lat=30.32)
        self.service.set_advance_quota("sup1", "supervisor", "TY-2026", 100000)
        r1 = self.service.emergency_advance("sup1", "supervisor", c1["id"], 100000, c1["version"], "ADV-401")
        r2 = self.service.emergency_advance("sup1", "supervisor", c2["id"], 40000, c2["version"], "ADV-402")
        self.assertEqual("waiting", r2["advance_status"])

        # 主管退回 c1：占用归还，c2 续放
        returned = self.service.return_claim("sup1", "supervisor", c1["id"], r1["version"], "查勘材料不足")
        self.assertEqual("triaged", returned["status"])
        self.assertIsNone(returned["assignee"])
        view = self.service.advance_quota_view("supervisor", "TY-2026")
        self.assertEqual(40000, view["held_total"])
        self.assertEqual(60000, view["remaining"])
        with self.assertRaises(DomainError):
            self.service.return_claim("sup1", "supervisor", c1["id"], returned["version"], "")

    def test_quota_set_permissions_and_20pct_still_enforced(self):
        claim = self.urgent_ready("C-501", "P-501", loss=500000, lat=30.41)
        with self.assertRaises(DomainError) as ctx:
            self.service.set_advance_quota("intake1", "intake", "TY-2026", 100000)
        self.assertEqual(403, ctx.exception.status)
        self.service.set_advance_quota("sup1", "supervisor", "TY-2026", 1000000)
        with self.assertRaises(DomainError):
            self.service.emergency_advance("sup1", "supervisor", claim["id"], 100001, claim["version"], "ADV-501")
        # 额度不能下调到低于已占用
        self.service.emergency_advance("sup1", "supervisor", claim["id"], 100000, claim["version"], "ADV-501")
        with self.assertRaises(DomainError):
            self.service.set_advance_quota("sup1", "supervisor", "TY-2026", 50000)


if __name__ == "__main__":
    unittest.main()
