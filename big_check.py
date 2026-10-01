"""百万级流式自检（独立进程运行，pytest 通过子进程调用）。

- 334,000 个支付 x 3 事件 = 1,002,000 条事件；账单约 100 万行。
- 事件与账单都以流式生成（乘法置换打乱顺序），不物化完整列表。
- 预期金额由本文件独立的参考实现计算，不调用 vaultpay.project。
- 结束时打印 peak_rss_mib=<值> 供测试断言。
"""

import os
import resource
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vaultpay

N_PAY = 334_000
SEQ_PER = 3
N_EV = N_PAY * SEQ_PER  # 1,002,000
MULT_EV = 7919      # 与 N_EV 互素，用于流式打乱事件顺序
MULT_LED = 104729   # 与 N_EV 互素，用于流式打乱账单顺序

P_MISS = 10007   # i % P_MISS == 0: 账单缺 seq=2 的捕获行
P_DUP = 10009    # i % P_DUP  == 0: seq=1 的捕获行重复一次
P_BAD = 10011    # i % P_BAD  == 0: seq=3 的退款键混入 gross/fee 各+1 的行


def fee_total(cum):
    if cum <= 0:
        return 0
    return (2 * 29 * cum + 1000) // 2000 + 30


def spec(i):
    """第 i 个支付的确定性参数。"""
    c1 = 100 + i % 900
    c2 = 50 + i % 450
    r = 1 + i % c1
    return c1, c2, r


def expected_lines(i):
    """参考实现：直接由支付参数算出三行应有明细。"""
    merchant = "m%04d" % (i % 1000)
    c1, c2, r = spec(i)
    f1 = fee_total(c1)
    f2 = fee_total(c1 + c2) - f1
    day = lambda s: "2026-03-%02d" % (i % 28 + s)
    return (
        (merchant, day(1), "e%d_1" % i, c1, f1, c1 - f1),
        (merchant, day(2), "e%d_2" % i, c2, f2, c2 - f2),
        (merchant, day(3), "e%d_3" % i, -r, 0, -r),
    )


def gen_events():
    for j in range(N_EV):
        i, s = divmod((j * MULT_EV) % N_EV, SEQ_PER)
        merchant = "m%04d" % (i % 1000)
        c1, c2, r = spec(i)
        day = "2026-03-%02d" % (i % 28 + s + 1)
        if s == 0:
            kind, cents = "capture", c1
        elif s == 1:
            kind, cents = "capture", c2
        else:
            kind, cents = "refund", r
        yield dict(merchant=merchant, payment="p%d" % i,
                   event="e%d_%d" % (i, s + 1), seq=s + 1, day=day,
                   kind=kind, cents=cents)


def gen_ledger():
    for j in range(N_EV):
        i, s = divmod((j * MULT_LED) % N_EV, SEQ_PER)
        line = expected_lines(i)[s]
        row = dict(zip(("merchant", "day", "event", "gross", "fee", "net"), line))
        if s == 1 and i % P_MISS == 0:
            continue  # 缺失：跳过该行
        yield row
        if s == 0 and i % P_DUP == 0:
            yield dict(row)  # 重复：同样的行再来一条
        if s == 2 and i % P_BAD == 0:
            bad = dict(row)
            bad["gross"] += 1
            bad["fee"] += 1  # net 不变但 gross/fee 错
            yield bad


def expected_anomalies():
    for i in range(N_PAY):
        if i % P_MISS == 0:
            line = expected_lines(i)[1]
            yield ("missing", line[0], line[1], line[2], "ledger", None, None)
        if i % P_DUP == 0:
            line = expected_lines(i)[0]
            yield ("duplicate", line[0], line[1], line[2], None, 1, None)
        if i % P_BAD == 0:
            line = expected_lines(i)[2]
            yield ("duplicate", line[0], line[1], line[2], None, 1, None)
            yield ("mismatch", line[0], line[1], line[2], None, None, 1)


def check_project():
    count = 0
    sum_gross = sum_fee = sum_net = 0

    def emit(line):
        nonlocal count, sum_gross, sum_fee, sum_net
        count += 1
        sum_gross += line.gross
        sum_fee += line.fee
        sum_net += line.net

    vaultpay.project(gen_events(), emit)

    exp_count = N_EV
    exp_gross = exp_fee = 0
    for i in range(N_PAY):
        c1, c2, r = spec(i)
        exp_gross += c1 + c2 - r
        exp_fee += fee_total(c1 + c2)
    assert count == exp_count, (count, exp_count)
    assert sum_gross == exp_gross, (sum_gross, exp_gross)
    assert sum_fee == exp_fee, (sum_fee, exp_fee)
    assert sum_net == exp_gross - exp_fee, (sum_net, exp_gross - exp_fee)
    print("project ok: %d lines, fee sum %d" % (count, sum_fee))


def check_reconcile():
    got = set()

    def emit(a):
        got.add((a.kind, a.merchant, a.day, a.event, a.side, a.extra, a.wrong))

    vaultpay.reconcile(gen_events(), gen_ledger(), emit)
    want = set(expected_anomalies())
    assert got == want, "anomaly diff: only_got=%d only_want=%d" % (
        len(got - want), len(want - got))
    print("reconcile ok: %d anomalies, all expected" % len(got))


def peak_rss_mib():
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return rss / (1024 * 1024)
    return rss / 1024


if __name__ == "__main__":
    check_project()
    check_reconcile()
    print("peak_rss_mib=%.1f" % peak_rss_mib())
