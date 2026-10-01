# VaultPay 离线清算对账组件

只处理已完成的捕获(capture)与退款(refund)的离线清算与对账, 实现于 `vaultpay.py`。
全程精确整数运算; 输入为一次性无序迭代器, 中间结果落盘(SQLite 临时库),
常驻内存与输入规模无关(百万级实测峰值约 58 MiB)。

## 接口

```python
import vaultpay

# 生成应有的清算明细, 逐条回调 emit
vaultpay.project(events, emit)

# 将业务事件与实际清算明细核对, 逐条回调 emit 输出异常
vaultpay.reconcile(events, ledger, emit)
```

- `events`: 一次性迭代器, 每项含 `merchant, payment, event, seq, day, kind, cents`
  (`kind` 为 `"capture"` 或 `"refund"`)。
- `ledger`: 一次性迭代器, 每项含 `merchant, day, event, gross, fee, net`(整数分)。
- `project` 输出行: `{"merchant", "day", "event", "gross", "fee", "net"}`。
- `reconcile` 输出异常(均带 `merchant, day, event` 键):
  - `{"type": "missing", "side": "ledger"|"expected", ...}` 仅一侧存在;
  - `{"type": "duplicate", "extra": n-1, ...}` 实际行数 n > 1;
  - `{"type": "mismatch", "errors": k, ...}` 双方都有该键且 k 行金额不符。

## 示例

```python
events = iter([
    {"merchant": "m1", "payment": "p1", "event": "e1", "seq": 1,
     "day": "2026-10-01", "kind": "capture", "cents": 50},
    {"merchant": "m1", "payment": "p1", "event": "e2", "seq": 2,
     "day": "2026-10-01", "kind": "capture", "cents": 50},
    {"merchant": "m1", "payment": "p1", "event": "e3", "seq": 3,
     "day": "2026-10-02", "kind": "capture", "cents": 400},
    {"merchant": "m1", "payment": "p1", "event": "e4", "seq": 4,
     "day": "2026-10-03", "kind": "refund", "cents": 500},
])
vaultpay.project(events, print)
# {'merchant': 'm1', 'day': '2026-10-01', 'event': 'e1', 'gross': 50, 'fee': 31, 'net': 19}
# {'merchant': 'm1', 'day': '2026-10-01', 'event': 'e2', 'gross': 50, 'fee': 2, 'net': 48}
# {'merchant': 'm1', 'day': '2026-10-02', 'event': 'e3', 'gross': 400, 'fee': 12, 'net': 388}
# {'merchant': 'm1', 'day': '2026-10-03', 'event': 'e4', 'gross': -500, 'fee': 0, 'net': -500}
```

## 运行测试

```bash
python3 -m unittest discover -s tests -v   # 含百万级流式测试(约 10 秒)
VAULTPAY_SLOW=0 python3 -m unittest discover -s tests -v   # 只跑快速测试
```
