"""VaultPay 离线清算对账组件。

只处理已完成的捕获(capture)与退款(refund)，输入为只能消费一次的无序迭代器，
中间结果落盘(SQLite 临时文件)，全程精确整数运算，内存占用有界。
"""

import os
import sqlite3
import tempfile
from collections import namedtuple

# 应有/实际清算明细行
Line = namedtuple("Line", ["merchant", "day", "event", "gross", "fee", "net"])

# 对账异常：
#   kind: "missing"(缺失交易) / "duplicate"(重复条目) / "mismatch"(金额不符)
#   side: 仅 missing 使用，"ledger" 表示实际账单缺失，"expected" 表示应有侧缺失
#   extra: 仅 duplicate 使用，额外行数(n-1)
#   wrong: 仅 mismatch 使用，金额错误行数
Anomaly = namedtuple("Anomaly", ["kind", "merchant", "day", "event", "side", "extra", "wrong"])
Anomaly.__new__.__defaults__ = (None, None, None)

_CHUNK = 5000


def _fee_total(cum_cents):
    """累计捕获额为 cum_cents 时的累计手续费 F(C)，精确整数运算。

    F(0)=0；C>0 时 F(C) = round_half_up(C * 2.9%) + 30。
    round_half_up(x)=floor(x+0.5)，整数形式：(2*29*C + 1000) // 2000。
    """
    if cum_cents <= 0:
        return 0
    return (2 * 29 * cum_cents + 1000) // 2000 + 30


def _open_db():
    """创建基于临时文件的 SQLite 连接，内存占用有界。"""
    fd, path = tempfile.mkstemp(prefix="vaultpay_", suffix=".db")
    os.close(fd)
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode = OFF")
    db.execute("PRAGMA synchronous = OFF")
    db.execute("PRAGMA cache_size = -8000")  # 8 MiB 页缓存上限
    return db, path


def _close_db(db, path):
    db.close()
    try:
        os.unlink(path)
    except OSError:
        pass


def _insert_rows(db, sql, rows):
    """把可迭代行分块写入，避免在内存中物化全部输入。"""
    buf = []
    for row in rows:
        buf.append(row)
        if len(buf) >= _CHUNK:
            db.executemany(sql, buf)
            buf.clear()
    if buf:
        db.executemany(sql, buf)
    db.commit()


def _project_lines(events):
    """内部生成器：按 (merchant, payment, seq) 排序后流式产出应有明细。"""
    db, path = _open_db()
    try:
        db.execute(
            "CREATE TABLE ev (merchant, payment, seq, day, event, kind, cents)"
        )
        _insert_rows(
            db,
            "INSERT INTO ev VALUES (?,?,?,?,?,?,?)",
            (
                (e["merchant"], e["payment"], e["seq"], e["day"],
                 e["event"], e["kind"], e["cents"])
                for e in events
            ),
        )
        cur = db.execute(
            "SELECT merchant, payment, day, event, kind, cents "
            "FROM ev ORDER BY merchant, payment, seq"
        )
        cur_key = None
        cum = 0
        for merchant, payment, day, event, kind, cents in cur:
            key = (merchant, payment)
            if key != cur_key:
                cur_key = key
                cum = 0
            if kind == "capture":
                before = _fee_total(cum)
                cum += cents
                fee = _fee_total(cum) - before
                yield Line(merchant, day, event, cents, fee, cents - fee)
            else:  # refund
                yield Line(merchant, day, event, -cents, 0, -cents)
    finally:
        _close_db(db, path)


def project(events, emit):
    """生成应有的清算明细，逐条调用 emit(Line)。"""
    for line in _project_lines(events):
        emit(line)


def reconcile(events, ledger, emit):
    """将业务事件与实际清算明细核对，逐条调用 emit(Anomaly)。不修改任一输入。"""
    for anomaly in _reconcile_anomalies(events, ledger):
        emit(anomaly)


def _reconcile_anomalies(events, ledger):
    db, path = _open_db()
    try:
        db.execute("CREATE TABLE exp (merchant, day, event, gross, fee, net)")
        _insert_rows(
            db,
            "INSERT INTO exp VALUES (?,?,?,?,?,?)",
            ((l.merchant, l.day, l.event, l.gross, l.fee, l.net)
             for l in _project_lines(events)),
        )
        db.execute("CREATE TABLE led (merchant, day, event, gross, fee, net)")
        _insert_rows(
            db,
            "INSERT INTO led VALUES (?,?,?,?,?,?)",
            (
                (r["merchant"], r["day"], r["event"],
                 r["gross"], r["fee"], r["net"])
                for r in ledger
            ),
        )
        db.execute("CREATE INDEX idx_exp ON exp (merchant, day, event)")
        db.execute("CREATE INDEX idx_led ON led (merchant, day, event)")

        join = "l.merchant = e.merchant AND l.day = e.day AND l.event = e.event"
        # 应有侧每个键唯一：覆盖 仅应有侧存在(缺失) / 重复条目 / 金额不符
        cur = db.execute(
            "SELECT e.merchant, e.day, e.event, COUNT(l.rowid), "
            "SUM(CASE WHEN l.rowid IS NOT NULL AND (l.gross IS NOT e.gross "
            "OR l.fee IS NOT e.fee OR l.net IS NOT e.net) THEN 1 ELSE 0 END) "
            f"FROM exp e LEFT JOIN led l ON {join} "
            "GROUP BY e.merchant, e.day, e.event "
            "ORDER BY e.merchant, e.day, e.event"
        )
        for merchant, day, event, n, wrong in cur:
            if n == 0:
                yield Anomaly("missing", merchant, day, event, side="ledger")
            else:
                if n > 1:
                    yield Anomaly("duplicate", merchant, day, event, extra=n - 1)
                if wrong:
                    yield Anomaly("mismatch", merchant, day, event, wrong=wrong)
        cur.close()

        # 仅实际账单存在的未知键：缺失(应有侧) + 可能的重复
        cur = db.execute(
            "SELECT l.merchant, l.day, l.event, COUNT(*) "
            f"FROM led l LEFT JOIN exp e ON {join} "
            "WHERE e.event IS NULL "
            "GROUP BY l.merchant, l.day, l.event "
            "ORDER BY l.merchant, l.day, l.event"
        )
        for merchant, day, event, n in cur:
            yield Anomaly("missing", merchant, day, event, side="expected")
            if n > 1:
                yield Anomaly("duplicate", merchant, day, event, extra=n - 1)
        cur.close()
    finally:
        _close_db(db, path)
