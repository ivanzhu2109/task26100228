"""百万级流式对账子进程: 由 test_vaultpay.TestMillionScale 启动。

- 事件与账单均由生成器流式产生, 不构造完整列表;
- 账单金额由本脚本独立推导(自带手续费公式), 不调用 vaultpay.project;
- 注入 6 条已知异常, 校验 reconcile 全部查出且无误报;
- 末尾打印 JSON: 规模、异常对比、进程峰值内存(MiB)。
"""

import json
import os
import random
import resource
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import vaultpay

PAYMENTS = 340_000          # >= 30 万支付
EVENTS_PER_PAYMENT = 3      # 共 1,020,000 条事件
WINDOW = 30_000             # 有界乱序窗口


def fee_of(cumulative):
    """测试侧独立实现的手续费公式(与 vaultpay 无关)。"""
    if cumulative <= 0:
        return 0
    return (cumulative * 29 + 500) // 1000 + 30


def payment_spec(i):
    """第 i 笔支付的确定性参数。"""
    merchant = "m%04d" % (i % 500)
    payment = "p%d" % i
    cap1 = 100 + (i % 900)
    cap2 = 200 + (i % 700)
    refund = 50 + (i % 100)
    return merchant, payment, cap1, cap2, refund


def payment_events(i):
    merchant, payment, cap1, cap2, refund = payment_spec(i)
    base = i * 10
    return [
        {"merchant": merchant, "payment": payment, "event": "e%d_1" % i,
         "seq": 1, "day": "d%08d" % (base + 1), "kind": "capture",
         "cents": cap1},
        {"merchant": merchant, "payment": payment, "event": "e%d_2" % i,
         "seq": 2, "day": "d%08d" % (base + 2), "kind": "capture",
         "cents": cap2},
        {"merchant": merchant, "payment": payment, "event": "e%d_3" % i,
         "seq": 3, "day": "d%08d" % (base + 3), "kind": "refund",
         "cents": refund},
    ]


def expected_rows(i):
    """独立推导第 i 笔支付的三条应有清算明细。"""
    merchant, _, cap1, cap2, refund = payment_spec(i)
    ev = payment_events(i)
    fee1 = fee_of(cap1)
    fee2 = fee_of(cap1 + cap2) - fee1
    return [
        {"merchant": merchant, "day": ev[0]["day"], "event": ev[0]["event"],
         "gross": cap1, "fee": fee1, "net": cap1 - fee1},
        {"merchant": merchant, "day": ev[1]["day"], "event": ev[1]["event"],
         "gross": cap2, "fee": fee2, "net": cap2 - fee2},
        {"merchant": merchant, "day": ev[2]["day"], "event": ev[2]["event"],
         "gross": -refund, "fee": 0, "net": -refund},
    ]


def gen_events():
    """乱序(有界窗口内洗牌)的事件流。"""
    rng = random.Random(1234)
    window = []
    for i in range(PAYMENTS):
        window.extend(payment_events(i))
        if len(window) >= WINDOW:
            rng.shuffle(window)
            yield from window
            window = []
    rng.shuffle(window)
    yield from window


SKIP = 1000          # 该支付的 seq1 明细从账单中删除
DUPLICATE = 2000     # 该支付的 seq2 明细在账单中出现两次
WRONG_GROSS = 3000   # 该支付的退款行 gross 少 1 分
TRIPLE = 4000        # 该支付的 seq1 明细出现三次, 其中两条 fee 多 1 分


def gen_ledger():
    """与事件流顺序不同的账单流, 注入已知异常。"""
    window = []
    for i in range(PAYMENTS):
        rows = expected_rows(i)
        if i == SKIP:
            rows = rows[1:]                       # 缺失交易(缺实际侧)
        elif i == DUPLICATE:
            rows.append(dict(rows[1]))            # 重复条目, extra=1
        elif i == WRONG_GROSS:
            rows[2]["gross"] -= 1                 # 金额不符 1 行
            rows[2]["net"] -= 1
        elif i == TRIPLE:
            bad1 = dict(rows[0]); bad1["fee"] += 1; bad1["net"] -= 1
            bad2 = dict(rows[0]); bad2["fee"] += 1; bad2["net"] -= 1
            rows.extend([bad1, bad2])             # extra=2 且 2 行金额不符
        window.extend(rows)
        if len(window) >= WINDOW:
            yield from reversed(window)           # 与事件流不同的顺序
            window = []
    yield from reversed(window)
    # 账单中的未知键
    yield {"merchant": "m0000", "day": "d99999999", "event": "ghost",
           "gross": 1, "fee": 0, "net": 1}


def peak_rss_mib():
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return rss / (1024 * 1024)
    return rss / 1024


def main():
    anomalies = []
    vaultpay.reconcile(gen_events(), gen_ledger(), anomalies.append)

    def key_of(a):
        return json.dumps(a, sort_keys=True)

    expected_anomalies = []
    skip_merchant = payment_spec(SKIP)[0]
    expected_anomalies.append({
        "type": "missing", "merchant": skip_merchant,
        "day": payment_events(SKIP)[0]["day"],
        "event": "e%d_1" % SKIP, "side": "ledger"})
    dup_merchant = payment_spec(DUPLICATE)[0]
    expected_anomalies.append({
        "type": "duplicate", "merchant": dup_merchant,
        "day": payment_events(DUPLICATE)[1]["day"],
        "event": "e%d_2" % DUPLICATE, "extra": 1})
    wg_merchant = payment_spec(WRONG_GROSS)[0]
    expected_anomalies.append({
        "type": "mismatch", "merchant": wg_merchant,
        "day": payment_events(WRONG_GROSS)[2]["day"],
        "event": "e%d_3" % WRONG_GROSS, "errors": 1})
    tri_merchant = payment_spec(TRIPLE)[0]
    expected_anomalies.append({
        "type": "duplicate", "merchant": tri_merchant,
        "day": payment_events(TRIPLE)[0]["day"],
        "event": "e%d_1" % TRIPLE, "extra": 2})
    expected_anomalies.append({
        "type": "mismatch", "merchant": tri_merchant,
        "day": payment_events(TRIPLE)[0]["day"],
        "event": "e%d_1" % TRIPLE, "errors": 2})
    expected_anomalies.append({
        "type": "missing", "merchant": "m0000", "day": "d99999999",
        "event": "ghost", "side": "expected"})

    report = {
        "event_count": PAYMENTS * EVENTS_PER_PAYMENT,
        "ledger_base_count": PAYMENTS * EVENTS_PER_PAYMENT,
        "payment_count": PAYMENTS,
        "anomalies": sorted(key_of(a) for a in anomalies),
        "expected_anomalies": sorted(key_of(a) for a in expected_anomalies),
        "peak_rss_mib": round(peak_rss_mib(), 1),
    }
    print(json.dumps(report))


if __name__ == "__main__":
    main()
