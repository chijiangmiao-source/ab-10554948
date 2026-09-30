"""核心分析引擎单元测试。"""

import unittest

from app import core
from app.core import analyze, parse_payload, PayloadShapeError


def txn(tid, start, commit, steps):
    return {"id": tid, "start": start, "commit": commit, "steps": steps}


def rd(key, value=None, writer=None):
    s = {"op": "read", "key": key}
    if value is not None:
        s["observed_value"] = value
    if writer is not None:
        s["writer"] = writer
    return s


def wr(key, value):
    return {"op": "write", "key": key, "value": value}


STALE = {
    "audit_id": "stale-1",
    "initial": {"safety_interlock": 1},
    "transactions": [
        txn("T1", 10, 20, [rd("safety_interlock", 1, core.INITIAL), wr("safety_interlock", 0)]),
        # T2 在 T1 提交之后开始，却声称读到初始版本 -> 过期读
        txn("T2", 30, 40, [rd("safety_interlock", 1, core.INITIAL)]),
    ],
}

WRITE_SKEW = {
    "audit_id": "skew-1",
    "initial": {"oncall_a": 1, "oncall_b": 1},
    "transactions": [
        txn("Alice", 10, 30, [rd("oncall_b", 1, core.INITIAL), wr("oncall_a", 0)]),
        txn("Bob", 11, 31, [rd("oncall_a", 1, core.INITIAL), wr("oncall_b", 0)]),
    ],
}

SERIAL = {
    "audit_id": "serial-1",
    "initial": {"x_tilt_deg": 0},
    "transactions": [
        txn("T2", 30, 40, [rd("x_tilt_deg", 2, "T1"), wr("x_tilt_deg", 3)]),
        txn("T1", 10, 20, [rd("x_tilt_deg", 0, core.INITIAL), wr("x_tilt_deg", 2)]),
        txn("T3", 50, 60, [rd("x_tilt_deg", 3, "T2")]),
    ],
}


class ParseTests(unittest.TestCase):
    def test_missing_audit_id(self):
        with self.assertRaises(PayloadShapeError):
            parse_payload({"initial": {}, "transactions": []})

    def test_too_many_transactions(self):
        payload = {
            "audit_id": "big",
            "initial": {},
            "transactions": [txn(f"T{i:02d}", 0, 1, []) for i in range(25)],
        }
        with self.assertRaises(PayloadShapeError):
            parse_payload(payload)

    def test_24_transactions_allowed(self):
        payload = {
            "audit_id": "big",
            "initial": {},
            "transactions": [txn(f"T{i:02d}", i, i + 0.5, []) for i in range(24)],
        }
        _, _, txns = parse_payload(payload)
        self.assertEqual(len(txns), 24)

    def test_duplicate_txn_id(self):
        with self.assertRaises(PayloadShapeError):
            parse_payload({
                "audit_id": "dup",
                "initial": {},
                "transactions": [txn("T1", 0, 1, []), txn("T1", 2, 3, [])],
            })

    def test_read_must_declare_observation(self):
        with self.assertRaises(PayloadShapeError):
            parse_payload({
                "audit_id": "x", "initial": {"k": 1},
                "transactions": [txn("T1", 0, 1, [{"op": "read", "key": "k"}])],
            })

    def test_write_requires_value(self):
        with self.assertRaises(PayloadShapeError):
            parse_payload({
                "audit_id": "x", "initial": {},
                "transactions": [txn("T1", 0, 1, [{"op": "write", "key": "k"}])],
            })

    def test_start_equals_commit_is_semantic_error_not_shape_error(self):
        # 结构合法但时间矛盾：应产生冻结的 invalid 结论而非 400
        r = analyze({
            "audit_id": "t", "initial": {},
            "transactions": [txn("T1", 5, 5, [])],
        })
        self.assertEqual(r["status"], "invalid")
        self.assertTrue(any(e["code"] == "INVALID_TIME_RANGE" for e in r["errors"]))


class StaleReadTests(unittest.TestCase):
    def test_status_invalid_and_stale_code(self):
        r = analyze(STALE)
        self.assertEqual(r["status"], "invalid")
        codes = [(e["code"], e["txn_id"], e["step_no"]) for e in r["errors"]]
        self.assertIn(("STALE_READ", "T2", 1), codes)

    def test_per_read_report_points_to_expected_version(self):
        r = analyze(STALE)
        t2_read = next(x for x in r["reads"] if x["txn_id"] == "T2")
        self.assertFalse(t2_read["ok"])
        self.assertEqual(t2_read["resolved"]["writer"], "T1")
        self.assertEqual(t2_read["resolved"]["value"], 0)
        self.assertIn("最新已提交版本", t2_read["basis"])

    def test_valid_read_in_same_payload_passes(self):
        r = analyze(STALE)
        t1_read = next(x for x in r["reads"] if x["txn_id"] == "T1")
        self.assertTrue(t1_read["ok"])

    def test_invalid_history_has_no_graph(self):
        r = analyze(STALE)
        self.assertIsNone(r["graph"])
        self.assertIsNone(r["cycle"])
        self.assertIsNone(r["serial_order"])

    def test_unknown_writer_rejected(self):
        payload = {
            "audit_id": "u", "initial": {"k": 1},
            "transactions": [txn("T1", 0, 1, [rd("k", writer="Ghost")])],
        }
        r = analyze(payload)
        self.assertEqual(r["status"], "invalid")
        self.assertTrue(any(e["code"] == "UNKNOWN_WRITER" for e in r["errors"]))

    def test_read_uninitialized_key(self):
        payload = {
            "audit_id": "u", "initial": {},
            "transactions": [txn("T1", 1, 2, [rd("k", writer=core.INITIAL)])],
        }
        r = analyze(payload)
        self.assertTrue(any(e["code"] == "READ_UNINITIALIZED_KEY" for e in r["errors"]))


class WriteSkewTests(unittest.TestCase):
    def test_two_node_cycle(self):
        r = analyze(WRITE_SKEW)
        self.assertEqual(r["status"], "cycle")
        self.assertEqual(r["cycle"]["length"], 2)
        self.assertEqual(r["cycle"]["nodes"], ["Alice", "Bob"])

    def test_cycle_edges_are_both_antidependencies_with_keys_and_steps(self):
        r = analyze(WRITE_SKEW)
        edges = r["cycle"]["edges"]
        self.assertEqual(len(edges), 2)
        self.assertEqual({(e["type"], e["from"], e["to"], e["key"]) for e in edges}, {
            (core.RW, "Alice", "Bob", "oncall_b"),
            (core.RW, "Bob", "Alice", "oncall_a"),
        })
        for e in edges:
            self.assertTrue(e["basis"])
            self.assertIn("键", e["basis"])
            self.assertTrue(e["reader_steps"])
            self.assertTrue(e["writer_steps"])

    def test_graph_lists_rw_edges(self):
        r = analyze(WRITE_SKEW)
        kinds = {e["type"] for e in r["graph"]["edges"]}
        self.assertEqual(kinds, {core.RW})

    def test_shortest_cycle_wins_and_lexicographic_tiebreak(self):
        # 两个互不相连的写偏差对：最短环长度都为 2，应取规范化后字典序最小的 (Alice,Bob)
        payload = {
            "audit_id": "two-skews",
            "initial": {"a1": 1, "a2": 1, "z1": 1, "z2": 1},
            "transactions": [
                txn("Alice", 10, 30, [rd("a2", 1), wr("a1", 0)]),
                txn("Bob", 11, 31, [rd("a1", 1), wr("a2", 0)]),
                txn("Zed", 12, 32, [rd("z2", 1), wr("z1", 0)]),
                txn("Yan", 13, 33, [rd("z1", 1), wr("z2", 0)]),
            ],
        }
        r = analyze(payload)
        self.assertEqual(r["status"], "cycle")
        self.assertEqual(r["cycle"]["nodes"], ["Alice", "Bob"])

    def test_rotation_canonicalization(self):
        # 用标识 Z、A 制造 2-环，闭环必须从最小标识 A 起报
        payload = {
            "audit_id": "rot", "initial": {"ka": 1, "kz": 1},
            "transactions": [
                txn("Z", 10, 30, [rd("ka", 1), wr("kz", 0)]),
                txn("A", 11, 31, [rd("kz", 1), wr("ka", 0)]),
            ],
        }
        r = analyze(payload)
        self.assertEqual(r["cycle"]["nodes"], ["A", "Z"])


class SerializableTests(unittest.TestCase):
    def test_status_and_stable_order(self):
        r = analyze(SERIAL)
        self.assertEqual(r["status"], "serializable")
        self.assertEqual(r["serial_order"], ["T1", "T2", "T3"])

    def test_serial_replay_matches_all_reads(self):
        r = analyze(SERIAL)
        rep = r["serial_replay"]
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["final_state"], {"x_tilt_deg": 3})
        vals = {(x["txn_id"], x["key"]): x["value"] for x in rep["reads"]}
        self.assertEqual(vals, {("T1", "x_tilt_deg"): 0, ("T2", "x_tilt_deg"): 2,
                                ("T3", "x_tilt_deg"): 3})

    def test_wr_and_ww_edges_present(self):
        edges = {(e["type"], e["from"], e["to"], e["key"])
                 for e in analyze(SERIAL)["graph"]["edges"]}
        self.assertIn((core.WR, "T1", "T2", "x_tilt_deg"), edges)
        self.assertIn((core.WW, "T1", "T2", "x_tilt_deg"), edges)
        self.assertIn((core.WR, "T2", "T3", "x_tilt_deg"), edges)

    def test_tiebreak_topological_order_uses_id(self):
        # T2、T3 都只依赖 T1，拓扑并列时按标识裁决
        payload = {
            "audit_id": "tie", "initial": {"k": 0},
            "transactions": [
                txn("T1", 0, 2, [rd("k", 0, core.INITIAL), wr("k", 1)]),
                txn("T3", 3, 5, [rd("k", 1, "T1")]),
                txn("T2", 3, 5, [rd("k", 1, "T1")]),
            ],
        }
        r = analyze(payload)
        self.assertEqual(r["serial_order"], ["T1", "T2", "T3"])


class ReadYourWritesTests(unittest.TestCase):
    def test_read_own_write_resolves_within_txn(self):
        payload = {
            "audit_id": "ryw", "initial": {"k": 1},
            "transactions": [
                txn("T1", 0, 10, [wr("k", 9), rd("k", 9, "T1")]),
                txn("T2", 11, 12, [rd("k", 9, "T1")]),
            ],
        }
        r = analyze(payload)
        self.assertEqual(r["status"], "serializable")
        own = next(x for x in r["reads"] if x["txn_id"] == "T1")
        self.assertTrue(own["read_your_writes"])
        self.assertTrue(own["ok"])
        # 不应产生 T1 -> T1 自环
        self.assertFalse(any(e["from"] == e["to"] for e in r["graph"]["edges"]))


class LongerCycleTests(unittest.TestCase):
    def test_three_cycle_detected(self):
        # 经典轮转：每个事务读前一键的初值、写下一键，形成 rw 3-环
        payload = {
            "audit_id": "ring3", "initial": {"a": 1, "b": 1, "c": 1},
            "transactions": [
                txn("T1", 10, 40, [rd("a", 1, core.INITIAL), wr("b", 0)]),
                txn("T2", 11, 41, [rd("b", 1, core.INITIAL), wr("c", 0)]),
                txn("T3", 12, 42, [rd("c", 1, core.INITIAL), wr("a", 0)]),
            ],
        }
        r = analyze(payload)
        self.assertEqual(r["status"], "cycle")
        self.assertEqual(r["cycle"]["length"], 3)
        # T1 读 a 而 T3 写后继 a：T1->T3；T3 读 c 而 T2 写后继 c：T3->T2；T2 读 b 而 T1 写后继 b：T2->T1
        self.assertEqual(r["cycle"]["nodes"], ["T1", "T3", "T2"])


if __name__ == "__main__":
    unittest.main()
