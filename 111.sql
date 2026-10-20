import sqlite3

with sqlite3.connect(r"D:\trace.db") as db:
    ts = [r[0] for r in db.execute("""
        SELECT c.ts - r.start_ts
        FROM callstack c
        LEFT JOIN thread t ON c.callid = t.id
        LEFT JOIN process p ON t.ipid = p.id
        CROSS JOIN trace_range r
        WHERE t.tid = ?
          AND c.name LIKE 'H:RSMainThread::DoComposition%'
          AND c.ts BETWEEN r.start_ts AND r.end_ts
    """, (1234,))]

print((len(ts) - 1) * 1e9 / (ts[-1] - ts[0]) if len(ts) > 1 and ts[-1] != ts[0] else None)
