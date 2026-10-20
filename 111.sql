SELECT
    p.pid,
    COUNT(*) AS frame_count,
    ROUND(
        (COUNT(*) - 1) * 1e9 / NULLIF(MAX(f.ts) - MIN(f.ts), 0),
        1
    ) AS fps
FROM main.frame_slice f
JOIN main.process p ON f.ipid = p.ipid
WHERE p.name LIKE '%render_service'
  AND f.type = 0
  AND f.flag <> 2
GROUP BY p.ipid, p.pid;
