WITH frames AS (
    SELECT c.ts, ROW_NUMBER() OVER () AS seq
    FROM callstack c
    JOIN thread t ON c.callid = t.id
    CROSS JOIN trace_range r
    WHERE t.tid = (
        SELECT tid FROM thread WHERE name = 'render_service' LIMIT 1
    )
      AND c.name LIKE 'H:RSMainThread::DoComposition%'
      AND c.ts BETWEEN r.start_ts AND r.end_ts
)
SELECT ROUND(
    (COUNT(*) - 1) * 1e9 / NULLIF(
        (SELECT ts FROM frames ORDER BY seq DESC LIMIT 1) -
        (SELECT ts FROM frames ORDER BY seq LIMIT 1), 0
    ), 1
) AS fps
FROM frames;
