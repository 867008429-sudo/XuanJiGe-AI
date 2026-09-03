#!/usr/bin/env python3
"""
Spike 3：SQLite WAL 并发写 + v4.1 配额原子扣减设计验证

验证方案 2.1 的核心 SQL 在并发下的正确性：
  UPDATE chat_followups SET used = used + 1
  WHERE hid=? AND account=? AND used < 5
受影响行数为 0 即额度不足 —— 一步完成防双花。

PASS 条件：
  1. WAL 模式开启成功，busy_timeout 生效
  2. 10 个并发 greenlet 抢 5 个额度 → 恰好 5 次成功，used 最终 == 5（无超卖）
  3. 退款路径（used-1）工作正常
  4. 对照组：无 busy_timeout 时并发写会报 database is locked（证明该参数必要）
"""
import sqlite3
import subprocess
import sys
import time

from gevent import monkey
monkey.patch_all()

import gevent  # noqa: E402

DB = "/tmp/spike3_wal.db"


def fresh_db(wal=True, busy_timeout=None):
    import os
    if os.path.exists(DB):
        os.remove(DB)
    conn = sqlite3.connect(DB)
    if wal:
        conn.execute("PRAGMA journal_mode=WAL")
    if busy_timeout is not None:
        conn.execute(f"PRAGMA busy_timeout={busy_timeout}")
    conn.execute("CREATE TABLE chat_followups (hid TEXT, account TEXT, used INT)")
    conn.execute("INSERT INTO chat_followups VALUES ('h1', 'a1', 0)")
    conn.commit()
    return conn


def try_consume(db_path, wal=True, busy_timeout=None):
    """每个 greenlet 独立连接（连接不可跨协程共享）"""
    conn = sqlite3.connect(db_path)
    if wal:
        conn.execute("PRAGMA journal_mode=WAL")
    if busy_timeout is not None:
        conn.execute(f"PRAGMA busy_timeout={busy_timeout}")
    cur = conn.execute(
        "UPDATE chat_followups SET used = used + 1 WHERE hid='h1' AND account='a1' AND used < 5"
    )
    ok = cur.rowcount == 1
    conn.commit()
    conn.close()
    return ok


def main():
    results = []
    ok = lambda name, cond: results.append((name, bool(cond)))

    # ① WAL + busy_timeout 基本面
    conn = fresh_db(busy_timeout=5000)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    ok(f"journal_mode = {mode}", mode == "wal")

    # ② 并发扣减：10 greenlet 抢 5 额度
    jobs = [gevent.spawn(try_consume, DB, True, 5000) for _ in range(10)]
    gevent.joinall(jobs, timeout=15)
    wins = sum(1 for j in jobs if j.successful() and j.value)
    used = sqlite3.connect(DB).execute("SELECT used FROM chat_followups").fetchone()[0]
    ok(f"10 并发抢 5 额度：{wins} 成功、used={used}（应恰为 5，无超卖无少卖）",
       wins == 5 and used == 5)

    # ③ 失败退款（AI 调用抛错 → used-1）
    conn2 = sqlite3.connect(DB)
    conn2.execute("PRAGMA busy_timeout=5000")
    conn2.execute("UPDATE chat_followups SET used = used - 1 WHERE hid='h1' AND used > 0")
    conn2.commit()
    used2 = conn2.execute("SELECT used FROM chat_followups").fetchone()[0]
    ok(f"退款后 used={used2}（4）", used2 == 4)

    # ④ 对照组：确定性锁竞争（复刻 gunicorn 2 worker 场景）
    #    spike 发现：gevent 单 OS 线程内 greenlet 因 sqlite C 调用不协程切换而天然串行化，
    #    真正的锁竞争来自多 worker 进程 —— 用"持锁事务 + 第二进程抢写"做确定性复现
    import os
    holder = (
        "import sqlite3, time, sys\n"
        "conn = sqlite3.connect(sys.argv[1], timeout=10)\n"
        "conn.execute('BEGIN IMMEDIATE')\n"  # 独占写锁
        "conn.execute(\"INSERT INTO chat_followups VALUES ('hold','a',1)\")\n"
        "time.sleep(0.8)\n"  # 持锁 0.8s，模拟慢事务
        "conn.commit()\n"
    )
    contender = (
        "import sqlite3, sys\n"
        "conn = sqlite3.connect(sys.argv[1], timeout=0.01)\n"  # 无有效 busy_timeout
        "try:\n"
        "    conn.execute(\"INSERT INTO chat_followups VALUES ('try','a',1)\")\n"
        "    conn.commit()\n"
        "    print('OK')\n"
        "except sqlite3.OperationalError:\n"
        "    print('LOCKED')\n"
    )
    os.remove(DB)
    conn3 = fresh_db(busy_timeout=None)
    ph = subprocess.Popen([sys.executable, "-c", holder, DB])
    time.sleep(0.2)  # 等持锁方就位
    pc = subprocess.run([sys.executable, "-c", contender, DB],
                        capture_output=True, text=True, timeout=15)
    ph.wait(timeout=10)
    ok("无 busy_timeout 的并发进程报 database is locked（该参数必要）",
       "LOCKED" in pc.stdout)
    # 正向对照：有 busy_timeout 时同一场景能等锁成功
    contender_ok = contender.replace("timeout=0.01", "timeout=5000")
    ph2 = subprocess.Popen([sys.executable, "-c", holder, DB])
    time.sleep(0.2)
    pc2 = subprocess.run([sys.executable, "-c", contender_ok, DB],
                         capture_output=True, text=True, timeout=15)
    ph2.wait(timeout=10)
    ok("busy_timeout=5000 时同场景等待后写成功", "OK" in pc2.stdout)

    print("\n" + "=" * 56)
    print("Spike 3 结果：SQLite WAL 并发 + 配额原子扣减")
    print("=" * 56)
    passed = sum(1 for _, c in results if c)
    for name, cond in results:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    print(f"\n  {passed}/{len(results)} 项通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
