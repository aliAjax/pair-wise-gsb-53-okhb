import json
import tempfile
import threading
import unittest
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app import build_service
from src.http_api import create_server


CREATE_DATA = {'applicant_id': 'A-300', 'case_type': 'work', 'received_day': 100, 'deadline_days': 30, 'response_day': 110, 'representation_active': True, 'required_documents': ['passport']}


class TransferHttpTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        service = build_service(str(Path(self.temp.name) / "http.db"))
        self.server = create_server("127.0.0.1", 0, service, Path("static"))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        status, created = self._request("POST", "/api/records",
                                        {"reference": "IMM-HTTP-1", "data": CREATE_DATA},
                                        ("off-a", "intake_officer", "office-a"))
        self.assertEqual(status, 201)
        self.record_id = created["id"]
        self.version = created["version"]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def _request(self, method, path, payload=None, identity=("off-a", "case_officer", "office-a")):
        url = "http://127.0.0.1:%s%s" % (self.port, path)
        data = json.dumps(payload or {}).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("X-User-Id", identity[0])
        req.add_header("X-Role", identity[1])
        req.add_header("X-Org", identity[2])
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_two_phase_transfer_over_http(self):
        status, transfer = self._request("POST", "/api/records/%s/transfers" % self.record_id,
                                         {"expected_version": self.version, "data": {"to_org": "office-b"}})
        self.assertEqual(status, 201)
        self.assertEqual(transfer["status"], "pending")
        transfer_id = transfer["id"]
        # 等待期间决定被冻结
        status, err = self._request("POST", "/api/records/%s/actions/decide" % self.record_id,
                                    {"expected_version": self.version, "data": {"decision": "granted", "decision_reason": "x"}},
                                    ("off-b", "case_officer", "office-b"))
        self.assertEqual(status, 409)
        # 非接收方不能确认
        status, err = self._request("POST", "/api/transfers/%s/confirm" % transfer_id, {},
                                    ("off-a", "case_officer", "office-a"))
        self.assertEqual(status, 403)
        # 接收方确认
        status, confirmed = self._request("POST", "/api/transfers/%s/confirm" % transfer_id, {},
                                          ("off-b", "case_officer", "office-b"))
        self.assertEqual(status, 200)
        self.assertEqual(confirmed["status"], "confirmed")
        # 重复确认返回同一结果
        status, again = self._request("POST", "/api/transfers/%s/confirm" % transfer_id, {},
                                      ("off-b", "case_officer", "office-b"))
        self.assertEqual(status, 200)
        self.assertEqual(again["result"], confirmed["result"])
        # 归属切换
        status, record = self._request("GET", "/api/records/%s" % self.record_id)
        self.assertEqual(record["office"], "office-b")
        # 办事处计数不重复
        status, stats = self._request("GET", "/api/office-stats")
        counts = {row["office"]: row["total"] for row in stats["items"]}
        self.assertEqual(counts.get("office-a", 0), 0)
        self.assertEqual(counts["office-b"], 1)

    def test_concurrent_initiates_only_one_pending(self):
        barrier = threading.Barrier(2)

        def initiate(target):
            barrier.wait()
            return self._request("POST", "/api/records/%s/transfers" % self.record_id,
                                 {"expected_version": self.version, "data": {"to_org": target}},
                                 ("off-a", "case_officer", "office-a"))

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(initiate, ["office-b", "office-c"]))
        statuses = sorted(status for status, _ in results)
        self.assertEqual(statuses, [201, 409])
        status, items = self._request("GET", "/api/records/%s/transfers" % self.record_id)
        pending = [t for t in items["items"] if t["status"] == "pending"]
        self.assertEqual(len(pending), 1)


if __name__ == "__main__":
    unittest.main()
