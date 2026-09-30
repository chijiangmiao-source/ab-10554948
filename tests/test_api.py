"""冻结存储与 HTTP 真实接口测试（标准库）。"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from app import core, server
from app.store import FrozenStore


def make_server():
    tmpdir = tempfile.mkdtemp()
    server.DB_PATH = os.path.join(tmpdir, "audits.db")
    server._store = FrozenStore(server.DB_PATH)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def http(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


PAYLOAD = {
    "audit_id": "http-1",
    "initial": {"k": 1},
    "transactions": [
        {"id": "T1", "start": 0, "commit": 2,
         "steps": [{"op": "read", "key": "k", "observed_value": 1, "writer": "__initial__"}]},
    ],
}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = FrozenStore(os.path.join(self.tmp, "sub", "audits.db"))

    def tearDown(self):
        self.store.close()

    def test_create_replay_conflict(self):
        c1 = core.analyze(PAYLOAD)
        outcome, stored = self.store.submit("http-1", PAYLOAD, c1)
        self.assertEqual(outcome, "created")
        # 同载荷重传：回放原结论
        outcome2, stored2 = self.store.submit("http-1", PAYLOAD, c1)
        self.assertEqual(outcome2, "replayed")
        self.assertEqual(stored2["summary"], stored["summary"])
        # 改换载荷：拒绝
        changed = json.loads(json.dumps(PAYLOAD))
        changed["initial"]["k"] = 2
        outcome3, stored3 = self.store.submit("http-1", changed, core.analyze(changed))
        self.assertEqual(outcome3, "conflict")
        self.assertIsNone(stored3)
        # 已冻结记录未被覆盖
        self.assertEqual(self.store.get("http-1")["per_key_versions"]["k"][0]["value"], 1)

    def test_field_order_does_not_change_hash(self):
        c = core.analyze(PAYLOAD)
        self.store.submit("http-1", PAYLOAD, c)
        reordered = {"transactions": PAYLOAD["transactions"],
                     "initial": PAYLOAD["initial"], "audit_id": PAYLOAD["audit_id"]}
        outcome, _ = self.store.submit("http-1", reordered, c)
        self.assertEqual(outcome, "replayed")


class HttpApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd, cls.base = make_server()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def test_healthz(self):
        status, body = http("GET", self.base + "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_page_served(self):
        with urllib.request.urlopen(self.base + "/") as r:
            self.assertEqual(r.status, 200)
            self.assertIn("text/html", r.headers["Content-Type"])
            self.assertIn("MVCC", r.read().decode())

    def test_submit_replay_conflict_and_fetch(self):
        status, body = http("POST", self.base + "/api/audits", PAYLOAD)
        self.assertEqual(status, 200)
        self.assertEqual(body["outcome"], "created")
        self.assertEqual(body["conclusion"]["status"], "serializable")

        status, body = http("POST", self.base + "/api/audits", PAYLOAD)
        self.assertEqual(status, 200)
        self.assertEqual(body["outcome"], "replayed")

        changed = json.loads(json.dumps(PAYLOAD))
        changed["initial"]["k"] = 999
        status, body = http("POST", self.base + "/api/audits", changed)
        self.assertEqual(status, 409)
        self.assertIn("拒绝覆盖", body["error"])

        status, body = http("GET", self.base + "/api/audits/http-1")
        self.assertEqual(status, 200)
        self.assertEqual(body["conclusion"]["per_key_versions"]["k"][0]["value"], 1)

    def test_shape_error_returns_400_and_is_not_frozen(self):
        bad = {"audit_id": "", "initial": {}, "transactions": []}
        status, body = http("POST", self.base + "/api/audits", bad)
        self.assertEqual(status, 400)
        # 结构错误不应产生任何冻结记录
        status, _ = http("GET", self.base + "/api/audits/definitely-missing-id")
        self.assertEqual(status, 404)

    def test_stale_read_freeze_is_a_normal_conclusion(self):
        # 语义错误（过期读）是可冻结的有效结论，而非接口错误
        payload = {
            "audit_id": "stale-http",
            "initial": {"k": 1},
            "transactions": [
                {"id": "T1", "start": 0, "commit": 2,
                 "steps": [{"op": "write", "key": "k", "value": 0}]},
                {"id": "T2", "start": 3, "commit": 4,
                 "steps": [{"op": "read", "key": "k", "observed_value": 1, "writer": "__initial__"}]},
            ],
        }
        status, body = http("POST", self.base + "/api/audits", payload)
        self.assertEqual(status, 200)
        self.assertEqual(body["conclusion"]["status"], "invalid")
        self.assertTrue(any(e["code"] == "STALE_READ" for e in body["conclusion"]["errors"]))


if __name__ == "__main__":
    unittest.main()
