import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, NotFound, PermissionDenied, ValidationError


CREATE_DATA = {'applicant_id': 'A-200', 'case_type': 'family', 'received_day': 100, 'deadline_days': 30, 'response_day': 110, 'representation_active': True, 'required_documents': ['passport', 'sponsor_letter']}

OFFICER_A = lambda: Actor("off-a", "case_officer", "office-a")
OFFICER_B = lambda: Actor("off-b", "case_officer", "office-b")
SUP_A = lambda: Actor("sup-a", "supervisor", "office-a")
LEGAL_A = lambda: Actor("legal-a", "legal_rep", "office-a")
LEGAL_B = lambda: Actor("legal-b", "legal_rep", "office-b")


class TransferTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        record = self.service.create(Actor("creator", "intake_officer", "office-a"), "IMM-30001", CREATE_DATA)
        self.record = self.service.act(
            Actor("legal-a", "legal_rep", "office-a"), record["id"], record["version"],
            "submit", {"documents": ["passport", "sponsor_letter"]},
        )
        self.record = self.service.act(
            OFFICER_A(), self.record["id"], self.record["version"],
            "request_evidence", {"evidence_request_day": 115, "allowed_days": 10, "evidence_request": "补充收入证明"},
        )

    def tearDown(self):
        self.temp.cleanup()

    def _initiate(self, actor=None, version=None, to_org="office-b"):
        actor = actor or OFFICER_A()
        version = self.record["version"] if version is None else version
        return self.service.initiate_transfer(actor, self.record["id"], version, to_org)

    def test_initiate_snapshots_evidence_deadline_and_pending_blocks_decisions(self):
        transfer = self._initiate()
        self.assertEqual(transfer["status"], "pending")
        self.assertEqual(transfer["from_org"], "office-a")
        self.assertEqual(transfer["to_org"], "office-b")
        self.assertEqual(transfer["evidence_due_day"], 125)
        # 归属尚未切换
        self.assertEqual(self.service.get_record(OFFICER_A(), self.record["id"])["office"], "office-a")
        # 确认前双方都不能提交或决定
        with self.assertRaises(Conflict):
            self.service.act(OFFICER_A(), self.record["id"], self.record["version"],
                             "decide", {"decision": "granted", "decision_reason": "提前决定"})
        with self.assertRaises(Conflict):
            self.service.act(OFFICER_B(), self.record["id"], self.record["version"],
                             "decide", {"decision": "granted", "decision_reason": "接收方提前决定"})

    def test_confirm_switches_office_once_and_is_idempotent(self):
        transfer = self._initiate()
        confirmed = self.service.confirm_transfer(OFFICER_B(), transfer["id"])
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(confirmed["result"]["office"], "office-b")
        record = self.service.get_record(OFFICER_B(), self.record["id"])
        self.assertEqual(record["office"], "office-b")
        self.assertEqual(record["version"], self.record["version"] + 1)
        # 重复确认返回同一结果（同一 confirmed_by / result，版本不再增加）
        again = self.service.confirm_transfer(OFFICER_B(), transfer["id"])
        self.assertEqual(again["id"], confirmed["id"])
        self.assertEqual(again["status"], "confirmed")
        self.assertEqual(again["result"], confirmed["result"])
        self.assertEqual(again["confirmed_by"], confirmed["confirmed_by"])
        self.assertEqual(self.service.get_record(OFFICER_B(), self.record["id"])["version"], record["version"])
        # 接管后接收方可以推进：先补件回应再决定
        record = self.service.act(LEGAL_B(), record["id"], record["version"],
                                  "respond", {"response_day": 120, "documents": ["income_proof"]})
        self.service.act(OFFICER_B(), record["id"], record["version"],
                         "decide", {"decision": "granted", "decision_reason": "材料充分"})
        timeline = self.service.timeline(OFFICER_A(), record["id"])
        actions = [event["action"] for event in timeline]
        self.assertIn("transfer_initiated", actions)
        self.assertIn("transfer_confirmed", actions)

    def test_case_update_during_pending_voids_transfer_and_prompt_resend(self):
        transfer = self._initiate()
        # 等待期间补件回应（允许的动作）使案件更新
        self.record = self.service.act(
            LEGAL_A(), self.record["id"], self.record["version"],
            "respond", {"response_day": 120, "documents": ["income_proof"]},
        )
        stale = self.service.get_transfer(OFFICER_A(), transfer["id"])
        self.assertEqual(stale["status"], "voided")
        # 确认旧单被拒绝并提示重发
        with self.assertRaises(Conflict) as ctx:
            self.service.confirm_transfer(OFFICER_B(), transfer["id"])
        self.assertIn("作废", str(ctx.exception))
        # 可以重新发起，且新单携带最新修订号
        fresh = self._initiate(version=self.record["version"])
        self.assertEqual(fresh["status"], "pending")
        self.assertEqual(fresh["record_version"], self.record["version"])
        self.service.confirm_transfer(OFFICER_B(), fresh["id"])

    def test_stale_version_initiate_rejected(self):
        with self.assertRaises(Conflict):
            self._initiate(version=self.record["version"] - 1)

    def test_two_offices_only_first_initiate_accepted(self):
        first = self._initiate()
        # 归属办事处的另一经办人同时再发一单，只接受先到的一单
        with self.assertRaises(Conflict):
            self.service.initiate_transfer(Actor("off-a2", "case_officer", "office-a"),
                                           self.record["id"], self.record["version"], "office-c")
        transfers = self.service.list_transfers(OFFICER_A(), self.record["id"])
        pending = [t for t in transfers if t["status"] == "pending"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["id"], first["id"])

    def test_failed_confirm_keeps_pending_for_recovery(self):
        transfer = self._initiate()
        real_confirm = self.service.repository.confirm_transfer
        calls = {"n": 0}

        def flaky(transfer_id, actor_id):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("simulated disk write failure")
            return real_confirm(transfer_id, actor_id)

        self.service.repository.confirm_transfer = flaky
        with self.assertRaises(OSError):
            self.service.confirm_transfer(OFFICER_B(), transfer["id"])
        self.service.repository.confirm_transfer = real_confirm
        # 写入失败后原单仍是 pending，可以恢复确认
        self.assertEqual(self.service.get_transfer(OFFICER_B(), transfer["id"])["status"], "pending")
        self.assertEqual(self.service.get_record(OFFICER_A(), self.record["id"])["office"], "office-a")
        recovered = self.service.confirm_transfer(OFFICER_B(), transfer["id"])
        self.assertEqual(recovered["status"], "confirmed")

    def test_taken_over_case_not_double_counted(self):
        transfer = self._initiate()
        self.service.confirm_transfer(OFFICER_B(), transfer["id"])
        stats = {row["office"]: row["total"] for row in self.service.office_stats(SUP_A())}
        self.assertEqual(stats.get("office-a", 0), 0)
        self.assertEqual(stats.get("office-b", 0), 1)
        self.assertEqual(sum(stats.values()), 1)

    def test_wrong_org_and_missing_org_rejected(self):
        # 非归属办事处不能发起
        with self.assertRaises(PermissionDenied):
            self.service.initiate_transfer(OFFICER_B(), self.record["id"], self.record["version"], "office-c")
        # 缺少办事处归属
        with self.assertRaises(ValidationError):
            self.service.initiate_transfer(Actor("off-x", "case_officer", ""), self.record["id"], self.record["version"], "office-b")
        # 不能转给本办事处
        with self.assertRaises(ValidationError):
            self._initiate(to_org="office-a")
        # 无转办角色
        with self.assertRaises(PermissionDenied):
            self.service.initiate_transfer(Actor("leg", "legal_rep", "office-a"), self.record["id"], self.record["version"], "office-b")

    def test_only_receiver_can_confirm(self):
        transfer = self._initiate()
        with self.assertRaises(PermissionDenied):
            self.service.confirm_transfer(OFFICER_A(), transfer["id"])

    def test_backfill_office_on_first_transfer_with_legacy_data(self):
        # 模拟旧数据：直接写入无 office 的记录和旧审计事件
        repo = self.service.repository
        import json
        from src.repository import _now
        now = _now()
        with repo._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO records(reference,state,version,office,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                ("IMM-LEGACY", "submitted", 3, "", json.dumps({"evidence_due_day": 130}, ensure_ascii=False), "old", "old", now, now),
            )
            legacy_id = int(cursor.lastrowid)
            conn.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (legacy_id, "created", "old", 1, "{}", now),
            )
        # 首次转办前 office 为空
        self.assertEqual(repo.get(legacy_id)["office"], "")
        # 用 office-a 身份回填并发起
        transfer = self.service.initiate_transfer(SUP_A(), legacy_id, 3, "office-b")
        self.assertEqual(transfer["from_org"], "office-a")
        self.assertEqual(repo.get(legacy_id)["office"], "office-a")
        # 回填不改变修订号
        self.assertEqual(repo.get(legacy_id)["version"], 3)
        # 原有审计记录照常可查
        timeline = self.service.timeline(SUP_A(), legacy_id)
        self.assertEqual(timeline[0]["action"], "created")
        self.assertTrue(any(e["action"] == "transfer_initiated" for e in timeline))


if __name__ == "__main__":
    unittest.main()
