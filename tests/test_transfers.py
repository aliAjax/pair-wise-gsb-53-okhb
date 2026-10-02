"""两阶段跨办事处转办：完整流程、冻结期限与归属切换。"""
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


CREATE_DATA = {'applicant_id': 'A-900', 'case_type': 'family', 'received_day': 100, 'deadline_days': 30,
               'response_day': 110, 'representation_active': True,
               'required_documents': ['passport', 'sponsor_letter']}

A = Actor("alice", "case_officer", "office-north")
B = Actor("bob", "case_officer", "office-south")
INTAKE = Actor("molly", "intake_officer", "office-north")


class TransferFlowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def _case(self, reference="IMM-30001", actor=INTAKE):
        return self.service.create(actor, reference, CREATE_DATA)

    def test_two_phase_handoff_snapshot_and_switch(self):
        record = self._case()
        # 先产生一条补件期限，确保快照冻结的是原补件期限。
        record = self.service.act(Actor("alice", "legal_rep", "office-north"), record["id"], record["version"],
                                  "submit", {'documents': ['passport', 'sponsor_letter']})
        record = self.service.act(A, record["id"], record["version"], "request_evidence",
                                  {'evidence_request_day': 115, 'allowed_days': 10, 'evidence_request': '补充收入证明'})
        case_version = record["version"]

        transfer = self.service.initiate_transfer(A, record["id"], case_version, "office-south")
        self.assertEqual(transfer["status"], "pending")
        self.assertEqual(transfer["from_office"], "office-north")
        self.assertEqual(transfer["to_office"], "office-south")
        self.assertEqual(transfer["expected_version"], case_version)
        # 原补件期限随单冻结。
        self.assertEqual(transfer["deadline_snapshot"]["evidence_due_day"], 125)
        self.assertEqual(transfer["deadline_snapshot"]["deadline_day"], 130)

        # 确认前归属仍在转出方。
        self.assertEqual(self.service.get_record(A, record["id"])["owning_office"], "office-north")

        confirmed = self.service.confirm_transfer(B, transfer["id"])
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(confirmed["confirmed_by"], "bob")

        after = self.service.get_record(A, record["id"])
        self.assertEqual(after["owning_office"], "office-south")
        # 归属切换不改变案件内容修订号。
        self.assertEqual(after["version"], case_version)

        timeline = self.service.timeline(A, record["id"])
        actions = [event["action"] for event in timeline]
        self.assertIn("transfer_initiated", actions)
        self.assertIn("transfer_confirmed", actions)

    def test_neither_side_can_submit_or_decide_while_pending(self):
        record = self._case()
        transfer = self.service.initiate_transfer(A, record["id"], record["version"], "office-south")

        # 转出方不能提交。
        with self.assertRaises(Conflict):
            self.service.act(Actor("alice", "legal_rep", "office-north"), record["id"], record["version"],
                             "submit", {'documents': ['passport', 'sponsor_letter']})
        # 接收方也不能决定。
        with self.assertRaises(Conflict):
            self.service.act(Actor("bob", "case_officer", "office-south"), record["id"], record["version"],
                             "decide", {'decision': 'granted', 'decision_reason': 'x'})

        # 确认后恢复正常：归属在接收方，接收方可以继续推进。
        self.service.confirm_transfer(B, transfer["id"])
        record = self.service.get_record(B, record["id"])
        record = self.service.act(Actor("bob", "legal_rep", "office-south"), record["id"], record["version"],
                                  "submit", {'documents': ['passport', 'sponsor_letter']})
        self.assertEqual(record["state"], "submitted")

    def test_repeat_confirm_is_idempotent(self):
        record = self._case()
        transfer = self.service.initiate_transfer(A, record["id"], record["version"], "office-south")
        first = self.service.confirm_transfer(B, transfer["id"])
        second = self.service.confirm_transfer(B, transfer["id"])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["status"], second["status"])
        self.assertEqual(first["confirmed_at"], second["confirmed_at"])
        self.assertEqual(first["confirmed_by"], second["confirmed_by"])
        # 归属只切换一次。
        self.assertEqual(self.service.get_record(A, record["id"])["owning_office"], "office-south")

        actions = [e["action"] for e in self.service.timeline(A, record["id"])]
        self.assertEqual(actions.count("transfer_confirmed"), 1)

    def test_update_during_waiting_voids_old_transfer_and_prompts_resend(self):
        record = self._case()
        # 转出方先提交案件（等待期前合法），此时进入 submitted。
        record = self.service.act(Actor("alice", "legal_rep", "office-north"), record["id"], record["version"],
                                  "submit", {'documents': ['passport', 'sponsor_letter']})
        transfer = self.service.initiate_transfer(A, record["id"], record["version"], "office-south")

        # 等待期间允许的非冻结动作（此处由转出方发补件）会使旧单作废。
        updated = self.service.act(A, record["id"], record["version"], "request_evidence",
                                   {'evidence_request_day': 115, 'allowed_days': 10,
                                    'evidence_request': '补充收入证明'})
        self.assertEqual(updated["version"], transfer["expected_version"] + 1)

        stale = self.service.get_transfer(B, transfer["id"])
        self.assertEqual(stale["status"], "voided")

        # 接收方再确认 -> 明确提示重发。
        with self.assertRaises(Conflict) as ctx:
            self.service.confirm_transfer(B, transfer["id"])
        self.assertIn("重新发起", str(ctx.exception))

        # 新单按最新修订号发起后可正常接管。
        fresh = self.service.initiate_transfer(A, updated["id"], updated["version"], "office-south")
        self.service.confirm_transfer(B, fresh["id"])
        self.assertEqual(self.service.get_record(A, record["id"])["owning_office"], "office-south")

        actions = [e["action"] for e in self.service.timeline(A, record["id"])]
        self.assertIn("transfer_voided", actions)
        # 作废的旧单仍在单据列表与审计中可查。
        listing = self.service.list_transfers(A, record["id"])
        self.assertEqual([t["status"] for t in listing], ["voided", "confirmed"])

    def test_concurrent_initiate_only_first_wins(self):
        record = self._case()
        barrier = threading.Barrier(2)
        outcomes = []

        # 两个转出操作员同属归属办事处，同时把同一案转到不同办事处：
        # 服务层归属校验都通过，由唯一部分索引决定先到者。
        carol = Actor("carol", "case_officer", "office-north")

        def race(actor, to_office):
            try:
                barrier.wait(timeout=5)
                t = self.service.initiate_transfer(actor, record["id"], record["version"], to_office)
                outcomes.append(("ok", t["to_office"]))
            except Conflict as exc:
                outcomes.append(("conflict", str(exc)))
            except sqlite3.Error as exc:
                outcomes.append(("db_error", str(exc)))

        t1 = threading.Thread(target=race, args=(A, "office-south"))
        t2 = threading.Thread(target=race, args=(carol, "office-west"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(len(outcomes), 2)
        self.assertEqual(sorted(status for status, _ in outcomes), ["conflict", "ok"])
        pending = self.service.list_transfers(A, record["id"])
        self.assertEqual(len([t for t in pending if t["status"] == "pending"]), 1)

    def test_failed_write_does_not_lose_pending_transfer(self):
        # 模拟确认阶段在事务内写入审计时失败：with 块回滚整笔事务，单据仍是 pending，可恢复确认。
        record = self._case()
        transfer = self.service.initiate_transfer(A, record["id"], record["version"], "office-south")
        repository = self.service.repository

        class ConfirmWriteError(RuntimeError):
            pass

        state = {"calls": 0}
        original_insert_audit = repository._insert_audit

        def failing_insert(connection, *args, **kwargs):
            state["calls"] += 1
            raise ConfirmWriteError("写入失败")

        repository._insert_audit = failing_insert
        try:
            with self.assertRaises(ConfirmWriteError):
                self.service.confirm_transfer(B, transfer["id"])
        finally:
            repository._insert_audit = original_insert_audit

        # 原单仍是 pending，归属未被半切换，可恢复确认（且不会重复计数/重复切换）。
        self.assertEqual(self.service.get_transfer(B, transfer["id"])["status"], "pending")
        self.assertEqual(self.service.get_record(A, record["id"])["owning_office"], "office-north")
        recovered = self.service.confirm_transfer(B, transfer["id"])
        self.assertEqual(recovered["status"], "confirmed")
        self.assertEqual(self.service.get_record(A, record["id"])["owning_office"], "office-south")
        # 恢复路径恰好执行一次确认审计。
        self.assertEqual(state["calls"], 1)

    def test_failed_initiate_can_still_initiate_and_confirm(self):
        # 发起阶段在插入转办单前失败：不留残影，原案可重新发起并确认。
        record = self._case()
        repository = self.service.repository
        original = repository.create_transfer

        class InitiateWriteError(RuntimeError):
            pass

        state = {"calls": 0}

        def flaky_create(*args, **kwargs):
            state["calls"] += 1
            if state["calls"] == 1:
                raise InitiateWriteError("写入失败")
            return original(*args, **kwargs)

        repository.create_transfer = flaky_create
        try:
            with self.assertRaises(InitiateWriteError):
                self.service.initiate_transfer(A, record["id"], record["version"], "office-south")
        finally:
            repository.create_transfer = original

        self.assertIsNone(repository.get_pending_transfer(record["id"]))
        transfer = self.service.initiate_transfer(A, record["id"], record["version"], "office-south")
        self.service.confirm_transfer(B, transfer["id"])
        self.assertEqual(self.service.get_record(A, record["id"])["owning_office"], "office-south")

    def test_taken_over_case_counted_once_under_new_office(self):
        first = self._case("IMM-30001")
        second_data = dict(CREATE_DATA)
        second_data["applicant_id"] = "A-901"
        second = self.service.create(INTAKE, "IMM-30002", second_data)
        t1 = self.service.initiate_transfer(A, first["id"], first["version"], "office-south")
        t2 = self.service.initiate_transfer(A, second["id"], second["version"], "office-south")
        self.service.confirm_transfer(B, t1["id"])
        self.service.confirm_transfer(B, t2["id"])

        stats = self.service.stats(A)
        # 总数不因接管而翻倍。
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["by_office"]["office-south"], 2)
        self.assertNotIn("office-north", stats["by_office"])
        # 两案都只在一个办事处计数。
        self.assertEqual(sum(stats["by_office"].values()), 2)

    def test_legacy_case_backfilled_before_first_transfer(self):
        # 直接用旧结构插入一条没有归属的旧数据。
        import json
        connection = sqlite3.connect(str(Path(self.temp.name) / "test.db"))
        payload = self.service.rules.prepare_create(CREATE_DATA)
        connection.execute(
            "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            ("IMM-LEGACY", "draft", 1, json.dumps(payload, ensure_ascii=False),
             "legacy", "legacy", "2026-09-01T00:00:00+00:00", "2026-09-01T00:00:00+00:00"),
        )
        record_id = connection.execute("SELECT id FROM records WHERE reference='IMM-LEGACY'").fetchone()[0]
        connection.execute(
            "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
            (record_id, "created", "legacy", 1, '{"state": "draft"}', "2026-09-01T00:00:00+00:00"),
        )
        connection.commit()
        connection.close()

        legacy = self.service.get_record(A, record_id)
        self.assertIsNone(legacy["owning_office"])

        # 首次转办：按发起方办事处回填，再发起。
        transfer = self.service.initiate_transfer(A, record_id, 1, "office-south")
        backfilled = self.service.get_record(A, record_id)
        self.assertEqual(backfilled["owning_office"], "office-north")
        # 回填不改变修订号，旧单仍按原修订号等待确认。
        self.assertEqual(backfilled["version"], 1)

        self.service.confirm_transfer(B, transfer["id"])
        self.assertEqual(self.service.get_record(A, record_id)["owning_office"], "office-south")

        # 原有审计记录照常可查，回填另起一条事件。
        timeline = self.service.timeline(A, record_id)
        actions = [event["action"] for event in timeline]
        self.assertEqual(actions[0], "created")
        self.assertIn("office_backfilled", actions)
        self.assertIn("transfer_initiated", actions)
        self.assertIn("transfer_confirmed", actions)

    def test_transfer_permissions_and_guards(self):
        record = self._case()

        # 缺少办事处身份不能发起/确认。
        with self.assertRaises(PermissionDenied):
            self.service.initiate_transfer(Actor("x", "case_officer", ""), record["id"], record["version"],
                                           "office-south")
        # 非转办角色被拒绝。
        with self.assertRaises(PermissionDenied):
            self.service.initiate_transfer(Actor("x", "intake_officer", "office-north"), record["id"],
                                           record["version"], "office-south")
        # 非归属办事处不能替别人发起。
        with self.assertRaises(PermissionDenied):
            self.service.initiate_transfer(B, record["id"], record["version"], "office-west")
        # 不能转给自己。
        with self.assertRaises(ValidationError):
            self.service.initiate_transfer(A, record["id"], record["version"], "office-north")
        # 修订号不匹配不能发起。
        with self.assertRaises(Conflict):
            self.service.initiate_transfer(A, record["id"], record["version"] + 9, "office-south")

        transfer = self.service.initiate_transfer(A, record["id"], record["version"], "office-south")
        # 非接收办事处不能确认。
        with self.assertRaises(PermissionDenied):
            self.service.confirm_transfer(Actor("dan", "case_officer", "office-east"), transfer["id"])
        # 正常确认。
        self.service.confirm_transfer(B, transfer["id"])
        # 转出方失去归属后不能再对该案发起转办。
        with self.assertRaises(PermissionDenied):
            self.service.initiate_transfer(A, record["id"], record["version"], "office-east")


if __name__ == "__main__":
    unittest.main()
