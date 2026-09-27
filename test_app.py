import base64
import tempfile
import unittest
from pathlib import Path

from app import BusinessError, ProvenanceStore


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ProvenanceStore(Path(self.tmp.name) / "test.db")
        self.store.seed()

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_provenance_and_return_review_flow(self):
        source = self.store.add_source("staff", "馆藏购藏档案", "archive", "ACC-1999-7")
        obj = self.store.create_object("staff", "M-1999-7", "青铜器", "礼器", "市博物馆", "1999年入藏，来源待持续核验。")
        event = self.store.add_event("staff", obj["id"], "acquisition", "1999-07-01", "", "本市", "从私人藏家购入", source["id"], "public")
        evidence = self.store.upload_evidence("staff", obj["id"], "purchase.pdf", base64.b64encode(b"purchase record").decode(), "internal", event["id"])
        self.assertEqual(len(evidence["sha256"]), 64)
        updated = self.store.update_object("staff", obj["id"], {"public_summary": "已完成首轮来源整理。"})
        self.assertEqual(updated["version"], 3)
        claim = self.store.create_claim("claimant1", obj["id"], "王氏家族", "返还藏品")
        self.store.transition_claim("reviewer1", claim["id"], "under_review", "材料齐全，进入调查。")
        self.store.transition_claim("reviewer1", claim["id"], "negotiating", "双方开始协商返还安排。")
        self.store.transition_claim("reviewer1", claim["id"], "resolved_return", "签署返还协议。")
        public_view = self.store.get_object("public", obj["id"])
        self.assertNotIn("current_holder", public_view)
        self.assertEqual(len(public_view["events"]), 1)
        self.assertEqual(public_view["claims"][0]["status"], "resolved_return")
        claimant_view = self.store.get_object("claimant1", obj["id"])
        self.assertEqual(len(claimant_view["claims"]), 1)
        self.assertGreaterEqual(len(self.store.object_history("reviewer1", obj["id"])), 6)

    def test_visibility_and_claim_stage_invariants(self):
        obj = self.store.create_object("staff", "M-2001-2", "手稿", "纸质", "资料室", "公开简介。")
        claim = self.store.create_claim("claimant1", obj["id"], "捐赠人后代", "归还手稿")
        with self.assertRaises(BusinessError) as ctx:
            self.store.transition_claim("reviewer1", claim["id"], "resolved_return", "直接结束。")
        self.assertEqual(ctx.exception.code, "invalid_transition")
        self.assertNotIn("claimant_id", self.store.get_object("public", obj["id"])["claims"][0])
        with self.assertRaises(BusinessError) as ctx:
            self.store.add_event("public", obj["id"], "note", "2020-01-01", "", "馆内", "未授权事件", None, "public")
        self.assertEqual(ctx.exception.status, 403)

    def test_supplement_request_flow_and_transition_guard(self):
        obj = self.store.create_object("staff", "M-2010-9", "版画", "纸本", "陈列部", "公开简介。")
        claim = self.store.create_claim("claimant1", obj["id"], "李氏后人", "返还版画")
        self.store.transition_claim("reviewer1", claim["id"], "under_review", "进入审查。")

        # 审查员提出两类补件要求，主张自动进入“补件中”。
        req1 = self.store.create_supplement_request("reviewer1", claim["id"], "亲属关系证明", "请提供户籍或公证材料。")
        req2 = self.store.create_supplement_request("reviewer1", claim["id"], "原始收藏凭证", "请补充入藏前的持有证明。")
        self.assertEqual(self.store.get_object("reviewer1", obj["id"])["claims"][0]["status"], "awaiting_materials")

        # 主张人不能自行提出或关闭补件项。
        with self.assertRaises(BusinessError) as ctx:
            self.store.create_supplement_request("claimant1", claim["id"], "其他", "无权提出补件要求。")
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.store.close_supplement_request("claimant1", req1["id"], "我认为已补齐。")
        self.assertEqual(ctx.exception.status, 403)

        # 未补齐时协商与完成返还被拦截，错误指出缺少的类别；驳回仍可继续。
        with self.assertRaises(BusinessError) as ctx:
            self.store.transition_claim("reviewer1", claim["id"], "negotiating", "尝试进入协商。")
        self.assertEqual(ctx.exception.code, "supplement_pending")
        self.assertEqual(set(ctx.exception.details["missing_categories"]), {"亲属关系证明", "原始收藏凭证"})
        rejected = self.store.create_claim("claimant1", obj["id"], "另一支后人", "另案主张")
        self.store.transition_claim("reviewer1", rejected["id"], "under_review", "另案审查。")
        self.store.transition_claim("reviewer1", rejected["id"], "rejected", "主张依据不足，驳回。")

        # 主张人上传一份或几份材料作出回应。
        files = [
            {"filename": "family-tree.pdf", "content_b64": base64.b64encode(b"family tree").decode()},
            {"filename": "notary.jpg", "content_b64": base64.b64encode(b"notary scan").decode()},
        ]
        self.store.upload_supplement_materials("claimant1", req1["id"], files)
        # 已回应但审查员尚未核查结束，补件项仍算未处理完，继续拦截。
        with self.assertRaises(BusinessError) as ctx:
            self.store.transition_claim("reviewer1", claim["id"], "negotiating", "只回应未核查。")
        self.assertEqual(set(ctx.exception.details["missing_categories"]), {"亲属关系证明", "原始收藏凭证"})

        # 没有核查意见或材料时审查员不能结束；核查后才能结束该项。
        with self.assertRaises(BusinessError) as ctx:
            self.store.close_supplement_request("reviewer1", req2["id"], "短")
        self.assertEqual(ctx.exception.code, "review_note_required")
        with self.assertRaises(BusinessError) as ctx:
            self.store.close_supplement_request("reviewer1", req2["id"], "材料还没交，无法核查。")
        self.assertEqual(ctx.exception.code, "no_materials_reviewed")
        self.store.upload_supplement_materials(
            "claimant1", req2["id"],
            [{"filename": "old-photo.png", "content_b64": base64.b64encode(b"photo").decode()}],
        )
        self.store.close_supplement_request("reviewer1", req1["id"], "亲属关系材料核对无误。")
        with self.assertRaises(BusinessError) as ctx:
            self.store.transition_claim("reviewer1", claim["id"], "negotiating", "还有一项未核查。")
        self.assertEqual(ctx.exception.details["missing_categories"], ["原始收藏凭证"])
        self.store.close_supplement_request("reviewer1", req2["id"], "收藏凭证与馆藏记录吻合。")
        self.store.transition_claim("reviewer1", claim["id"], "negotiating", "材料齐备，开始协商。")
        self.store.transition_claim("reviewer1", claim["id"], "resolved_return", "签署返还协议。")

        # 公众只知道补件状态，看不到材料类别、说明、材料明细和内部核查意见。
        public_view = self.store.get_object("public", obj["id"])
        public_claim = [c for c in public_view["claims"] if c["status"] == "resolved_return"][0]
        self.assertTrue(all(set(sr) == {"id", "claim_id", "status"} for sr in public_claim["supplement_requests"]))

        # 主张人可以看到自己补件项的明细和审查员核查意见。
        claimant_view = self.store.get_object("claimant1", obj["id"])
        my_claim = [c for c in claimant_view["claims"] if c["id"] == claim["id"]][0]
        categories = {sr["material_category"] for sr in my_claim["supplement_requests"]}
        self.assertEqual(categories, {"亲属关系证明", "原始收藏凭证"})
        self.assertTrue(all(sr["status"] == "closed" for sr in my_claim["supplement_requests"]))
        self.assertTrue(any(len(sr["materials"]) == 2 for sr in my_claim["supplement_requests"]))

        # 历史快照保留补件记录（不含材料二进制）。
        history = self.store.object_history("reviewer1", obj["id"])
        latest = self.store.history_detail("reviewer1", obj["id"], history[-1]["version"])
        self.assertEqual(len(latest["snapshot"]["supplement_requests"]), 2)
        for sr in latest["snapshot"]["supplement_requests"]:
            self.assertNotIn("content", sr)
            self.assertIn("review_note", sr)

        # 材料下载：审查员可看，公众被拒绝。
        material_id = my_claim["supplement_requests"][0]["materials"][0]["id"]
        self.assertEqual(self.store.get_supplement_material("reviewer1", material_id)["filename"], "family-tree.pdf")
        with self.assertRaises(BusinessError) as ctx:
            self.store.get_supplement_material("public", material_id)
        self.assertEqual(ctx.exception.status, 403)


if __name__ == "__main__":
    unittest.main()
