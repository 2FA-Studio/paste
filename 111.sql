WITH target_ipid AS (
    SELECT ipid
    FROM main.frame_slice
    WHERE ipid IS NOT NULL
    GROUP BY ipid
    ORDER BY COUNT(*) DESC, ipid
    LIMIT 1
),
stats AS (
    SELECT
        COUNT(*) AS x,
        MAX(ts) - MIN(ts) AS y
    FROM main.frame_slice
    WHERE ipid = (SELECT ipid FROM target_ipid)
      AND type = 0
      AND flag != 2
)
SELECT
    (SELECT ipid FROM target_ipid) AS ipid,
    x,
    y,
    x * 1.0e9 / NULLIF(y, 0) AS result
FROM stats;
