"""VaultPay 离线清算对账组件。

只处理已完成的捕获(capture)与退款(refund), 不实现授权、卡库或定时调度。
输入为一次性、无序的事件/账单迭代器, 输出通过 emit 回调逐条给出。

设计要点:
- 全程精确整数运算, 不使用二进制浮点计算金额。
- 事件与账单先流式写入 SQLite 临时库(落盘), 再借助 SQL 排序/连接完成计算,
  因此常驻内存与输入规模无关, 也不会把完整输入或全部支付状态留在内存。
- 每个支付只按 (merchant, payment, seq) 排序后扫描一次, 不重扫历史。
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterable, Iterator

Event = Dict[str, Any]
LedgerRow = Dict[str, Any]
ClearingRow = Dict[str, Any]
Anomaly = Dict[str, Any]

_INSERT_CHUNK = 8192


def fee_total(cumulative_cents: int) -> int:
    """累计捕获额为 cumulative_cents 时的累计手续费 F(C)。

    F(0) = 0; C > 0 时为 C * 2.9% 按分四舍五入(恰好半分向上)再加 30 分。
    纯整数实现: round_half_up(C*29/1000) = floor((C*29 + 500) / 1000)。
    """
    if cumulative_cents <= 0:
        return 0
    return (cumulative_cents * 29 + 500) // 1000 + 30


@contextmanager
def _temp_db() -> Iterator[sqlite3.Connection]:
    """创建落盘的临时 SQLite 库, 用毕删除。内存占用通过 pragma 限制。"""
    fd, path = tempfile.mkstemp(prefix="vaultpay_", suffix=".sqlite3")
    os.close(fd)
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA temp_store=FILE")
        conn.execute("PRAGMA cache_size=-4000")  # 约 4 MiB 页缓存
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        yield conn
        conn.commit()
    finally:
        conn.close()
        os.unlink(path)


def _chunked(iterable: Iterable[tuple], size: int) -> Iterator[list]:
    chunk: list = []
    for item in iterable:
        chunk.append(item)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def _load_events(conn: sqlite3.Connection, events: Iterable[Event]) -> None:
    """把事件流式写入临时库, 不修改输入对象。"""
    conn.execute(
        "CREATE TABLE events ("
        " merchant TEXT NOT NULL, payment TEXT NOT NULL, seq INTEGER NOT NULL,"
        " day TEXT NOT NULL, kind TEXT NOT NULL, cents INTEGER NOT NULL,"
        " event TEXT NOT NULL)"
    )
    rows = (
        (e["merchant"], e["payment"], e["seq"], e["day"], e["kind"],
         e["cents"], e["event"])
        for e in events
    )
    for chunk in _chunked(rows, _INSERT_CHUNK):
        conn.executemany("INSERT INTO events VALUES (?,?,?,?,?,?,?)", chunk)


def _iter_projected(conn: sqlite3.Connection) -> Iterator[ClearingRow]:
    """按 (merchant, payment, seq) 顺序扫描事件, 逐支付累计并产出应有明细。"""
    cursor = conn.execute(
        "SELECT merchant, payment, seq, day, kind, cents, event"
        " FROM events ORDER BY merchant, payment, seq"
    )
    cur_payment_key = None
    cumulative = 0
    for merchant, payment, _seq, day, kind, cents, event in cursor:
        payment_key = (merchant, payment)
        if payment_key != cur_payment_key:
            cur_payment_key = payment_key
            cumulative = 0
        if kind == "capture":
            before = fee_total(cumulative)
            cumulative += cents
            fee = fee_total(cumulative) - before
            gross = cents
        else:  # refund: 不降低累计捕获额, 不退手续费
            fee = 0
            gross = -cents
        yield {
            "merchant": merchant,
            "day": day,
            "event": event,
            "gross": gross,
            "fee": fee,
            "net": gross - fee,
        }


def project(events: Iterable[Event], emit: Callable[[ClearingRow], None]) -> None:
    """根据业务事件生成应有的清算明细, 逐条调用 emit。

    输出行的字段: merchant, day, event, gross, fee, net (金额均为整数分)。
    """
    with _temp_db() as conn:
        _load_events(conn, events)
        for row in _iter_projected(conn):
            emit(row)


def _load_ledger(conn: sqlite3.Connection, ledger: Iterable[LedgerRow]) -> None:
    conn.execute(
        "CREATE TABLE ledger ("
        " merchant TEXT NOT NULL, day TEXT NOT NULL, event TEXT NOT NULL,"
        " gross INTEGER NOT NULL, fee INTEGER NOT NULL, net INTEGER NOT NULL)"
    )
    rows = (
        (r["merchant"], r["day"], r["event"], r["gross"], r["fee"], r["net"])
        for r in ledger
    )
    for chunk in _chunked(rows, _INSERT_CHUNK):
        conn.executemany("INSERT INTO ledger VALUES (?,?,?,?,?,?)", chunk)


def reconcile(
    events: Iterable[Event],
    ledger: Iterable[LedgerRow],
    emit: Callable[[Anomaly], None],
) -> None:
    """将业务事件与实际清算明细核对, 逐条输出异常。不修改任一输入。

    异常类型(type 字段):
    - "missing":   仅一侧存在, side 字段标明缺失侧("ledger" 或 "expected")。
    - "duplicate": 实际行数 n > 1, extra 字段为额外行数 n-1。
    - "mismatch":  双方都有该键, errors 字段为金额不符的实际行数。
    每条异常都带 merchant, day, event 键。
    """
    with _temp_db() as conn:
        _load_events(conn, events)
        conn.execute(
            "CREATE TABLE expected ("
            " merchant TEXT NOT NULL, day TEXT NOT NULL, event TEXT NOT NULL,"
            " gross INTEGER NOT NULL, fee INTEGER NOT NULL, net INTEGER NOT NULL)"
        )
        projected = (
            (r["merchant"], r["day"], r["event"], r["gross"], r["fee"], r["net"])
            for r in _iter_projected(conn)
        )
        for chunk in _chunked(projected, _INSERT_CHUNK):
            conn.executemany("INSERT INTO expected VALUES (?,?,?,?,?,?)", chunk)
        conn.execute("DROP TABLE events")

        _load_ledger(conn, ledger)
        conn.execute(
            "CREATE INDEX idx_ledger_key ON ledger (merchant, day, event)")
        conn.execute(
            "CREATE INDEX idx_expected_key ON expected (merchant, day, event)")

        key = "merchant, day, event"

        # 仅应有侧存在: 账单缺失交易
        for merchant, day, event in conn.execute(
            "SELECT e.merchant, e.day, e.event FROM expected e"
            " LEFT JOIN (SELECT DISTINCT merchant, day, event FROM ledger) l"
            "  ON l.merchant=e.merchant AND l.day=e.day AND l.event=e.event"
            " WHERE l.merchant IS NULL ORDER BY 1, 2, 3"
        ):
            emit({"type": "missing", "merchant": merchant, "day": day,
                  "event": event, "side": "ledger"})

        # 仅实际侧存在: 账单中的未知键
        for merchant, day, event in conn.execute(
            "SELECT l.merchant, l.day, l.event"
            " FROM (SELECT DISTINCT merchant, day, event FROM ledger) l"
            " LEFT JOIN expected e"
            "  ON e.merchant=l.merchant AND e.day=l.day AND e.event=l.event"
            " WHERE e.merchant IS NULL ORDER BY 1, 2, 3"
        ):
            emit({"type": "missing", "merchant": merchant, "day": day,
                  "event": event, "side": "expected"})

        # 实际行数大于一: 重复条目
        for merchant, day, event, extra in conn.execute(
            f"SELECT {key}, COUNT(*) - 1 FROM ledger"
            f" GROUP BY {key} HAVING COUNT(*) > 1 ORDER BY 1, 2, 3"
        ):
            emit({"type": "duplicate", "merchant": merchant, "day": day,
                  "event": event, "extra": extra})

        # 双方都有该键: 逐行比较金额, 统计错误行数
        for merchant, day, event, errors in conn.execute(
            "SELECT l.merchant, l.day, l.event, COUNT(*) FROM ledger l"
            " JOIN expected e"
            "  ON e.merchant=l.merchant AND e.day=l.day AND e.event=l.event"
            " WHERE l.gross<>e.gross OR l.fee<>e.fee OR l.net<>e.net"
            " GROUP BY l.merchant, l.day, l.event ORDER BY 1, 2, 3"
        ):
            emit({"type": "mismatch", "merchant": merchant, "day": day,
                  "event": event, "errors": errors})
