import random
import subprocess
import sys
from pathlib import Path

import vaultpay
from vaultpay import Anomaly, Line


def ev(merchant, payment, event, seq, day, kind, cents):
    return dict(merchant=merchant, payment=payment, event=event,
                seq=seq, day=day, kind=kind, cents=cents)


def run_project(events):
    out = []
    vaultpay.project(iter(events), out.append)
    return out


def run_reconcile(events, ledger):
    out = []
    vaultpay.reconcile(iter(events), iter(ledger), out.append)
    return out


def base_events():
    return [
        ev("m1", "p1", "e1", 1, "d1", "capture", 50),
        ev("m1", "p1", "e2", 2, "d2", "capture", 50),
        ev("m1", "p1", "e3", 3, "d3", "capture", 400),
        ev("m1", "p1", "e4", 4, "d4", "refund", 500),
    ]


def test_capture_refund_fees():
    lines = run_project(base_events())
    assert lines == [
        Line("m1", "d1", "e1", 50, 31, 19),
        Line("m1", "d2", "e2", 50, 2, 48),
        Line("m1", "d3", "e3", 400, 12, 388),
        Line("m1", "d4", "e4", -500, 0, -500),
    ]


def test_shuffled_input_same_result():
    expected = run_project(base_events())
    for seed in range(20):
        shuffled = base_events()
        random.Random(seed).shuffle(shuffled)
        assert run_project(shuffled) == expected


def test_payment_id_reused_across_merchants():
    events = [
        ev("m1", "p1", "e1", 1, "d1", "capture", 50),
        ev("m2", "p1", "e3", 1, "d1", "capture", 50),
        ev("m1", "p1", "e2", 2, "d1", "capture", 50),
        ev("m2", "p1", "e4", 2, "d1", "capture", 50),
    ]
    lines = run_project(events)
    # 每个商户各自的 p1 独立累计：各自第二笔捕获手续费都是 2 分
    assert lines == [
        Line("m1", "d1", "e1", 50, 31, 19),
        Line("m1", "d1", "e2", 50, 2, 48),
        Line("m2", "d1", "e3", 50, 31, 19),
        Line("m2", "d1", "e4", 50, 2, 48),
    ]


def test_clean_ledger_no_anomalies():
    events = base_events()
    ledger = [l._asdict() for l in run_project(events)]
    assert run_reconcile(events, ledger) == []


def test_missing_and_duplicate_with_equal_day_net():
    # m1 的两个支付在同一天各有一笔 net=19 的明细：
    # 删掉 e1、复制 e2 后日净额仍为 38，但必须同时查出缺失与重复。
    events = [
        ev("m1", "p1", "e1", 1, "d1", "capture", 50),
        ev("m1", "p2", "e2", 1, "d1", "capture", 50),
    ]
    ledger = [
        dict(merchant="m1", day="d1", event="e2", gross=50, fee=31, net=19),
        dict(merchant="m1", day="d1", event="e2", gross=50, fee=31, net=19),
    ]
    anomalies = run_reconcile(events, ledger)
    assert anomalies == [
        Anomaly("missing", "m1", "d1", "e1", side="ledger"),
        Anomaly("duplicate", "m1", "d1", "e2", extra=1),
    ]


def test_mismatch_row_with_correct_net():
    events = [ev("m1", "p1", "e1", 1, "d1", "capture", 50)]
    ledger = [
        dict(merchant="m1", day="d1", event="e1", gross=50, fee=31, net=19),
        # net 正确但 gross 与 fee 同时多一分：仍是金额错误行
        dict(merchant="m1", day="d1", event="e1", gross=51, fee=32, net=19),
    ]
    anomalies = run_reconcile(events, ledger)
    assert anomalies == [
        Anomaly("duplicate", "m1", "d1", "e1", extra=1),
        Anomaly("mismatch", "m1", "d1", "e1", wrong=1),
    ]


def test_unknown_ledger_key_and_duplicate():
    events = [ev("m1", "p1", "e1", 1, "d1", "capture", 50)]
    ledger = [
        dict(merchant="m1", day="d1", event="e1", gross=50, fee=31, net=19),
        dict(merchant="m1", day="d9", event="zz", gross=1, fee=0, net=1),
        dict(merchant="m1", day="d9", event="zz", gross=1, fee=0, net=1),
    ]
    anomalies = run_reconcile(events, ledger)
    assert anomalies == [
        Anomaly("missing", "m1", "d9", "zz", side="expected"),
        Anomaly("duplicate", "m1", "d9", "zz", extra=1),
    ]


def test_million_scale_streaming_and_memory():
    """百万级无序流式输入：完整结果校验 + 独立进程内存峰值 < 128 MiB。"""
    script = Path(__file__).with_name("big_check.py")
    proc = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True, text=True, timeout=1800,
    )
    print(proc.stdout)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    peak_mib = float(proc.stdout.strip().splitlines()[-1].split("=")[1])
    assert peak_mib < 128, f"peak RSS {peak_mib:.1f} MiB exceeds 128 MiB"
