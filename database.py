import os
import psycopg2
import psycopg2.extras
from urllib.parse import urlparse

DATABASE_URL = os.environ.get("DATABASE_URL", "")


def get_conn():
    url = urlparse(DATABASE_URL)
    return psycopg2.connect(
        host=url.hostname,
        port=url.port or 5432,
        dbname=url.path.lstrip("/"),
        user=url.username,
        password=url.password,
        sslmode="require",
    )


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            id               SERIAL PRIMARY KEY,
            captured_at      TEXT   NOT NULL,
            ranking_base_dt  BIGINT NOT NULL UNIQUE
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS snapshot_keywords (
            id           SERIAL  PRIMARY KEY,
            snapshot_id  INTEGER NOT NULL REFERENCES snapshots(id),
            rank         INTEGER NOT NULL,
            keyword_id   INTEGER NOT NULL,
            keyword_name TEXT    NOT NULL
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sk_snapshot ON snapshot_keywords(snapshot_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sk_keyword  ON snapshot_keywords(keyword_id)")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS keyword_emoticons (
            keyword_id  INTEGER NOT NULL,
            slug        TEXT    NOT NULL,
            title       TEXT    NOT NULL,
            image_url   TEXT    NOT NULL,
            emot_idx    INTEGER,
            PRIMARY KEY (keyword_id, slug)
        )
    """)
    conn.commit()
    cur.close()
    conn.close()


def snapshot_exists(ranking_base_dt: int) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM snapshots WHERE ranking_base_dt = %s", (ranking_base_dt,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row is not None


def save_snapshot(captured_at: str, ranking_base_dt: int,
                  keywords: list, keyword_emots: dict) -> int | None:
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO snapshots (captured_at, ranking_base_dt)
               VALUES (%s, %s)
               ON CONFLICT (ranking_base_dt) DO NOTHING
               RETURNING id""",
            (captured_at, ranking_base_dt),
        )
        row = cur.fetchone()
        if row is None:
            conn.close()
            return None
        snapshot_id = row[0]

        psycopg2.extras.execute_batch(
            cur,
            """INSERT INTO snapshot_keywords (snapshot_id, rank, keyword_id, keyword_name)
               VALUES (%s, %s, %s, %s)""",
            [(snapshot_id, i + 1, k["keywordId"], k["keyword"]) for i, k in enumerate(keywords)],
        )

        for keyword_id, emots in keyword_emots.items():
            psycopg2.extras.execute_batch(
                cur,
                """INSERT INTO keyword_emoticons (keyword_id, slug, title, image_url, emot_idx)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (keyword_id, slug) DO UPDATE SET
                       title=EXCLUDED.title,
                       image_url=EXCLUDED.image_url,
                       emot_idx=EXCLUDED.emot_idx""",
                [(keyword_id, e["slug"], e["title"], e["imageUrl"], e.get("emotIdx"))
                 for e in emots],
            )

        conn.commit()
        cur.close()
        return snapshot_id
    finally:
        conn.close()


def get_snapshots(limit: int = 200) -> list[dict]:
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """SELECT id, captured_at, ranking_base_dt
           FROM snapshots ORDER BY ranking_base_dt DESC LIMIT %s""",
        (limit,),
    )
    rows = cur.fetchall()

    result = []
    for r in rows:
        cur2 = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur2.execute(
            """SELECT keyword_id, keyword_name, rank
               FROM snapshot_keywords WHERE snapshot_id = %s ORDER BY rank""",
            (r["id"],),
        )
        kws = cur2.fetchall()
        cur2.close()
        result.append({
            "id": r["id"],
            "captured_at": r["captured_at"],
            "ranking_base_dt": r["ranking_base_dt"],
            "keywords": [
                {"keywordId": k["keyword_id"], "keyword": k["keyword_name"], "rank": k["rank"]}
                for k in kws
            ],
        })

    cur.close()
    conn.close()
    return result


def get_hourly_trends() -> dict:
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("""
        SELECT COUNT(*)             AS cnt,
               MIN(ranking_base_dt) AS min_dt,
               MAX(ranking_base_dt) AS max_dt
        FROM snapshots
    """)
    meta = cur.fetchone()

    cur.execute("""
        SELECT EXTRACT(HOUR FROM to_timestamp(ranking_base_dt / 1000.0)
                       AT TIME ZONE 'Asia/Seoul')::INTEGER AS hour,
               COUNT(*) AS snap_count
        FROM snapshots
        GROUP BY hour
    """)
    snap_by_hour = {r["hour"]: r["snap_count"] for r in cur.fetchall()}

    cur.execute("""
        SELECT
            EXTRACT(HOUR FROM to_timestamp(s.ranking_base_dt / 1000.0)
                    AT TIME ZONE 'Asia/Seoul')::INTEGER AS hour,
            sk.keyword_id,
            sk.keyword_name,
            ROUND(AVG(sk.rank)::NUMERIC, 1) AS avg_rank
        FROM snapshot_keywords sk
        JOIN snapshots s ON sk.snapshot_id = s.id
        GROUP BY hour, sk.keyword_id, sk.keyword_name
        ORDER BY hour, avg_rank
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()

    hour_kws: dict[int, list] = {h: [] for h in range(24)}
    for r in rows:
        hour_kws[r["hour"]].append({
            "id":       r["keyword_id"],
            "name":     r["keyword_name"],
            "avg_rank": float(r["avg_rank"]),
        })

    return {
        "snapshot_count": meta["cnt"],
        "min_dt":         meta["min_dt"],
        "max_dt":         meta["max_dt"],
        "hours": [
            {
                "hour":       h,
                "snap_count": snap_by_hour.get(h, 0),
                "keywords":   hour_kws[h],
            }
            for h in range(24)
        ],
    }


def get_snapshot_emoticons(snapshot_id: int, keyword_id: int) -> list[dict]:
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT keyword_name FROM snapshot_keywords WHERE snapshot_id=%s AND keyword_id=%s",
        (snapshot_id, keyword_id),
    )
    if not cur.fetchone():
        cur.close()
        conn.close()
        return []

    cur.execute(
        """SELECT slug, title, image_url, emot_idx
           FROM keyword_emoticons WHERE keyword_id = %s ORDER BY emot_idx""",
        (keyword_id,),
    )
    emots = [dict(e) for e in cur.fetchall()]
    cur.close()
    conn.close()
    return emots
