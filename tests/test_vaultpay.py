"""vaultpay 自动化测试。运行: python3 -m unittest discover -s tests -v"""

import copy
import json
import os
import random
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import vaultpay

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_events():
    """m1/p1: 跨日捕获 50、50、400, 再退款 500; m2 复用相同 payment id。"""
    return [
        {"merchant": "m1", "payment": "p1", "event": "e1", "seq": 1,
         "day": "2026-10-01", "kind": "capture", "cents": 50},
        {"merchant": "m1", "payment": "p1", "event": "e2", "seq": 2,
         "day": "2026-10-01", "kind": "capture", "cents": 50},
        {"merchant": "m1", "payment": "p1", "event": "e3", "seq": 3,
         "day": "2026-10-02", "kind": "capture", "cents": 400},
        {"merchant": "m1", "payment": "p1", "event": "e4", "seq": 4,
         "day": "2026-10-03", "kind": "refund", "cents": 500},
        {"merchant": "m2", "payment": "p1", "event": "e5", "seq": 1,
         "day": "2026-10-01", "kind": "capture", "cents": 50},
    ]


EXPECTED_ROWS = {
    ("m1", "2026-10-01", "e1"): (50, 31, 19),
    ("m1", "2026-10-01", "e2"): (50, 2, 48),
    ("m1", "2026-10-02", "e3"): (400, 12, 388),
    ("m1", "2026-10-03", "e4"): (-500, 0, -500),
    ("m2", "2026-10-01", "e5"): (50, 31, 19),
}


def projected(events):
    out = []
    vaultpay.project(iter(events), out.append)
    return out


def as_keyed(rows):
    return {(r["merchant"], r["day"], r["event"]):
            (r["gross"], r["fee"], r["net"]) for r in rows}


class TestProject(unittest.TestCase):
    def test_fee_schedule_across_days_and_refund(self):
        keyed = as_keyed(projected(make_events()))
        self.assertEqual(keyed, EXPECTED_ROWS)
        # 捕获手续费依次为 31、2、12
        fees = [keyed[("m1", "2026-10-01", "e1")][1],
                keyed[("m1", "2026-10-01", "e2")][1],
                keyed[("m1", "2026-10-02", "e3")][1]]
        self.assertEqual(fees, [31, 2, 12])

    def test_shuffled_input_same_result(self):
        events = make_events()
        baseline = as_keyed(projected(events))
        for seed in range(10):
            shuffled = list(events)
            random.Random(seed).shuffle(shuffled)
            self.assertEqual(as_keyed(projected(shuffled)), baseline)

    def test_payment_id_reuse_across_merchants(self):
        keyed = as_keyed(projected(make_events()))
        # m2 的 p1 与 m1 的 p1 互不串账, 各自从 F(0) 起算
        self.assertEqual(keyed[("m2", "2026-10-01", "e5")], (50, 31, 19))

    def test_capture_split_invariance(self):
        # 同一支付无论按 50+50+400 还是 200+300 拆分, 最终累计手续费一致
        def total_fee(captures):
            events = [
                {"merchant": "m", "payment": "p", "event": f"e{i}", "seq": i,
                 "day": f"2026-10-0{i}", "kind": "capture", "cents": c}
                for i, c in enumerate(captures, start=1)
            ]
            return sum(r["fee"] for r in projected(events))
        self.assertEqual(total_fee([50, 50, 400]), total_fee([200, 300]))
        self.assertEqual(total_fee([50, 50, 400]), total_fee([500]))


def reconciled(events, ledger):
    out = []
    vaultpay.reconcile(iter(events), iter(ledger), out.append)
    return out


def two_payment_events():
    """两笔支付各捕获 100 分且同日清算, 两行应有明细金额完全相同。"""
    return [
        {"merchant": "m", "payment": "pa", "event": "ea", "seq": 1,
         "day": "d1", "kind": "capture", "cents": 100},
        {"merchant": "m", "payment": "pb", "event": "eb", "seq": 1,
         "day": "d1", "kind": "capture", "cents": 100},
    ]


class TestReconcile(unittest.TestCase):
    def test_clean_ledger_no_anomalies(self):
        events = make_events()
        ledger = projected(events)
        self.assertEqual(reconciled(events, ledger), [])

    def test_missing_and_duplicate_with_equal_day_net(self):
        events = two_payment_events()
        ledger = projected(events)
        row_a = next(r for r in ledger if r["event"] == "ea")
        row_b = next(r for r in ledger if r["event"] == "eb")
        self.assertEqual((row_a["gross"], row_a["fee"], row_a["net"]),
                         (row_b["gross"], row_b["fee"], row_b["net"]))
        # 删掉 ea 的明细, 复制等额的 eb 明细: 日净额不变
        mutated = [dict(r) for r in ledger if r["event"] != "ea"]
        mutated.append(dict(row_b))
        anomalies = reconciled(events, mutated)
        by_type = {}
        for a in anomalies:
            by_type.setdefault(a["type"], []).append(a)
        self.assertEqual(len(by_type["missing"]), 1)
        self.assertEqual(by_type["missing"][0]["event"], "ea")
        self.assertEqual(by_type["missing"][0]["side"], "ledger")
        self.assertEqual(len(by_type["duplicate"]), 1)
        self.assertEqual(by_type["duplicate"][0]["event"], "eb")
        self.assertEqual(by_type["duplicate"][0]["extra"], 1)
        self.assertNotIn("mismatch", by_type)

    def test_mismatch_row_with_correct_net(self):
        events = two_payment_events()
        ledger = projected(events)
        # 同键混入净额正确但 gross 与 fee 同时加一分的行
        extra = dict(next(r for r in ledger if r["event"] == "ea"))
        extra["gross"] += 1
        extra["fee"] += 1
        mutated = ledger + [extra]
        anomalies = reconciled(events, mutated)
        by_type = {}
        for a in anomalies:
            by_type.setdefault(a["type"], []).append(a)
        self.assertEqual(len(by_type["mismatch"]), 1)
        self.assertEqual(by_type["mismatch"][0]["event"], "ea")
        self.assertEqual(by_type["mismatch"][0]["errors"], 1)
        # 该键实际行数为 2, 同时报告重复条目
        self.assertEqual(len(by_type["duplicate"]), 1)
        self.assertEqual(by_type["duplicate"][0]["event"], "ea")
        self.assertEqual(by_type["duplicate"][0]["extra"], 1)

    def test_unknown_key_in_ledger(self):
        events = two_payment_events()
        ledger = projected(events) + [
            {"merchant": "m", "day": "d1", "event": "ghost",
             "gross": 1, "fee": 1, "net": 0}
        ]
        anomalies = reconciled(events, ledger)
        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0]["type"], "missing")
        self.assertEqual(anomalies[0]["side"], "expected")
        self.assertEqual(anomalies[0]["event"], "ghost")

    def test_inputs_not_modified(self):
        events = make_events()
        ledger = projected(events) + [
            {"merchant": "m1", "day": "2026-10-01", "event": "e1",
             "gross": 0, "fee": 0, "net": 0}
        ]
        events_snapshot = copy.deepcopy(events)
        ledger_snapshot = copy.deepcopy(ledger)
        reconciled(iter(events), iter(ledger))
        self.assertEqual(events, events_snapshot)
        self.assertEqual(ledger, ledger_snapshot)


@unittest.skipUnless(os.environ.get("VAULTPAY_SLOW", "1") == "1",
                     "slow test disabled")
class TestMillionScale(unittest.TestCase):
    def test_million_scale_streaming(self):
        child = os.path.join(REPO_ROOT, "tests", "million_child.py")
        proc = subprocess.run(
            [sys.executable, child],
            capture_output=True, text=True, timeout=1800,
        )
        self.assertEqual(proc.returncode, 0,
                         f"child failed:\n{proc.stdout}\n{proc.stderr}")
        report = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(report["event_count"], report["ledger_base_count"])
        self.assertGreaterEqual(report["event_count"], 1_000_000)
        self.assertGreaterEqual(report["payment_count"], 300_000)
        # 注入的 6 条异常全部被查出, 且没有误报
        self.assertEqual(report["anomalies"], report["expected_anomalies"])
        self.assertLess(report["peak_rss_mib"], 128,
                        f"peak RSS {report['peak_rss_mib']} MiB")


if __name__ == "__main__":
    unittest.main()
