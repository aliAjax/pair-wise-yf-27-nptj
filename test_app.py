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

    def test_supplement_request_response_review_flow(self):
        obj = self.store.create_object("staff", "M-2024-1", "木雕", "木器", "市博物馆", "公开简介。")
        claim = self.store.create_claim("claimant1", obj["id"], "林氏后裔", "返还木雕")
        self.store.transition_claim("reviewer1", claim["id"], "under_review", "受理，开始来源审查。")
        # 审查员提出补件要求，登记类别与说明。
        req = self.store.create_supplement_request("reviewer1", claim["id"], "亲属关系公证书", "请补充能证明继承关系的公证材料。")
        self.assertEqual(req["status"], "open")
        # 主张人看到待补项，公众只见补件中标记。
        claimant_view = self.store.get_object("claimant1", obj["id"])
        self.assertEqual(claimant_view["supplement_requests"][0]["material_category"], "亲属关系公证书")
        public_view = self.store.get_object("public", obj["id"])
        self.assertTrue(public_view["claims"][0]["supplement_pending"])
        self.assertNotIn("material_category", public_view["supplement_requests"][0])
        self.assertNotIn("description", public_view["supplement_requests"][0])
        # 主张人一次回应多份材料。
        resp = self.store.respond_supplement("claimant1", req["id"], [
            {"filename": "kinship.pdf", "content_b64": base64.b64encode(b"notary doc").decode()},
            {"filename": "family.jpg", "content_b64": base64.b64encode(b"photo bytes").decode()},
        ])
        self.assertEqual(resp["status"], "submitted")
        self.assertEqual(len(resp["materials"]), 2)
        # 仍可继续追加材料。
        self.store.respond_supplement("claimant1", req["id"], [
            {"filename": "extra.txt", "content_b64": base64.b64encode(b"more").decode()}])
        # 未核查关闭时，进入协商与完成返还都被拦截，并指出缺少类别。
        for target in ("negotiating", "resolved_return"):
            with self.assertRaises(BusinessError) as ctx:
                self.store.transition_claim("reviewer1", claim["id"], target, "尝试继续推进。")
            self.assertEqual(ctx.exception.code, "supplement_pending")
            self.assertEqual(ctx.exception.details["missing_categories"], ["亲属关系公证书"])
        # 审查员核查后才能关闭；无核查意见或无材料都不行。
        with self.assertRaises(BusinessError):
            self.store.review_supplement("reviewer1", req["id"], "好")
        with self.assertRaises(BusinessError):
            self.store.review_supplement("reviewer1", req["id"], "采信")
        # 主张人不能自行关闭。
        with self.assertRaises(BusinessError) as ctx:
            self.store.review_supplement("claimant1", req["id"], "材料齐全请关闭。")
        self.assertEqual(ctx.exception.status, 403)
        self.store.review_supplement("reviewer1", req["id"], "公证书真实有效，继承关系成立。")
        # 关闭后可以继续流转。
        moved = self.store.transition_claim("reviewer1", claim["id"], "negotiating", "材料齐备，进入协商。")
        self.assertEqual(moved["status"], "negotiating")
        self.assertFalse(self.store.get_object("public", obj["id"])["claims"][0]["supplement_pending"])

    def test_supplement_does_not_block_rejection_and_keeps_snapshot(self):
        obj = self.store.create_object("staff", "M-2024-2", "陶器", "陶器", "市博物馆", "公开简介。")
        claim = self.store.create_claim("claimant1", obj["id"], "某家族", "返还陶器")
        self.store.transition_claim("reviewer1", claim["id"], "under_review", "进入审查。")
        req = self.store.create_supplement_request("reviewer1", claim["id"], "所有权证明", "缺少原始所有权凭证。")
        # 即使补件项未关闭，驳回仍可继续。
        rejected = self.store.transition_claim("reviewer1", claim["id"], "rejected", "主体不适格，驳回主张。")
        self.assertEqual(rejected["status"], "rejected")
        # 主张已终态，不能再回应或继续补件。
        with self.assertRaises(BusinessError) as ctx:
            self.store.respond_supplement("claimant1", req["id"], [
                {"filename": "x.pdf", "content_b64": base64.b64encode(b"x").decode()}])
        self.assertEqual(ctx.exception.code, "claim_closed")
        with self.assertRaises(BusinessError) as ctx:
            self.store.create_supplement_request("reviewer1", claim["id"], "其它材料", "终态后不能再提补件。")
        self.assertEqual(ctx.exception.code, "claim_closed")
        # 历史快照保留补件记录（含类别、说明、材料元数据），且不含材料二进制。
        versions = self.store.object_history("reviewer1", obj["id"])
        latest = self.store.history_detail("reviewer1", obj["id"], versions[-1]["version"])
        snap = latest["snapshot"]
        self.assertEqual(snap["supplement_requests"][0]["material_category"], "所有权证明")
        self.assertIn("description", snap["supplement_requests"][0])
        self.assertEqual(snap["supplement_requests"][0]["materials"], [])  # 驳回前未上传材料

    def test_supplement_access_control(self):
        obj = self.store.create_object("staff", "M-2024-3", "织物", "丝织", "市博物馆", "公开简介。")
        claim = self.store.create_claim("claimant1", obj["id"], "后人", "返还织物")
        self.store.transition_claim("reviewer1", claim["id"], "under_review", "进入审查。")
        req = self.store.create_supplement_request("reviewer1", claim["id"], "身份证明", "请补充身份证明。")
        self.store.respond_supplement("claimant1", req["id"], [
            {"filename": "id.pdf", "content_b64": base64.b64encode(b"id card").decode()}])
        # 公众不能看补件材料；审查员可下载。
        with self.assertRaises(BusinessError) as ctx:
            self.store.download_supplement_material("public", 1)
        self.assertEqual(ctx.exception.status, 403)
        material = self.store.download_supplement_material("reviewer1", 1)
        self.assertEqual(material["filename"], "id.pdf")
        self.assertEqual(material["content_b64"], base64.b64encode(b"id card").decode())
        # 审查员不能在没有材料时空核查；先回应再关闭已在上面覆盖，这里校验 open 状态不能关闭。
        req2 = self.store.create_supplement_request("reviewer1", claim["id"], "传承说明", "请补充家族传承经过说明。")
        with self.assertRaises(BusinessError) as ctx:
            self.store.review_supplement("reviewer1", req2["id"], "材料尚未提交无法核查通过。")
        self.assertEqual(ctx.exception.code, "no_materials")



if __name__ == "__main__":
    unittest.main()
