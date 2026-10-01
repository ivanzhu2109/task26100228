# VaultPay 离线清算对账组件

只处理已完成的捕获（capture）与退款（refund）。输入为无序、只能消费一次的
迭代器；中间结果落盘（SQLite 临时文件），全程精确整数运算，常驻内存有界
（百万级输入实测峰值约 55 MiB）。

## 调用

```python
import vaultpay

# 1) 生成应有清算明细
vaultpay.project(events, emit)     # emit(vaultpay.Line) 逐条回调

# 2) 与实际清算明细核对
vaultpay.reconcile(events, ledger, emit)  # emit(vaultpay.Anomaly) 逐条回调
```

- `events`：迭代器，每项为 dict，含 `merchant, payment, event, seq, day,
  kind, cents`（`kind` 为 `"capture"`/`"refund"`，`cents` 为正整数分）。
- `ledger`：迭代器，每项为 dict，含 `merchant, day, event, gross, fee, net`
  （整数分）。
- `emit`：回调函数，每条输出调用一次。两个操作都不会修改输入。

## 输出

- `Line(merchant, day, event, gross, fee, net)`：应有清算明细。
  捕获 `gross=金额`、`fee=本次捕获前后累计手续费 F 的差`、`net=gross-fee`；
  退款 `gross=-金额`、`fee=0`、`net=gross`。
  `F(0)=0`；`C>0` 时 `F(C)=round_half_up(C*2.9%)+30`（分，恰好半分向上）。
- `Anomaly(kind, merchant, day, event, side, extra, wrong)`：按
  `(merchant, day, event)` 键核对，每个键每类最多一条：
  - `kind="missing"`：缺失交易，`side="ledger"` 表示实际账单缺行，
    `side="expected"` 表示账单出现未知键；
  - `kind="duplicate"`：重复条目，`extra` 为额外行数（n-1）；
  - `kind="mismatch"`：金额不符，`wrong` 为 gross/fee/net 任一不同的行数，
    可与 `duplicate` 同时出现。

## 测试

```bash
python3 -m pytest test_vaultpay.py -q          # 全部（含百万级流式用例）
python3 -m pytest test_vaultpay.py -q -k "not million"  # 仅快速用例
python3 big_check.py                            # 单独跑百万级自检并打印内存峰值
```
