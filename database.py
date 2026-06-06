import os
import sqlite3
from pathlib import Path

# Railway 볼륨 마운트 경로는 DB_PATH 환경변수로 지정, 로컬은 프로젝트 폴더
DB_PATH = Path(os.environ.get("DB_PATH", str(Path(__file__).parent / "kakao_trending.db")))


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS snapshots (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            captured_at      TEXT    NOT NULL,
            ranking_base_dt  INTEGER NOT NULL UNIQUE
        );

        CREATE TABLE IF NOT EXISTS snapshot_keywords (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id  INTEGER NOT NULL REFERENCES snapshots(id),
            rank         INTEGER NOT NULL,
            keyword_id   INTEGER NOT NULL,
            keyword_name TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_sk_snapshot ON snapshot_keywords(snapshot_id);
        CREATE INDEX IF NOT EXISTS idx_sk_keyword  ON snapshot_keywords(keyword_id);

        -- 이모티콘은 keyword_id 기준으로 한 번만 저장 (upsert)
        CREATE TABLE IF NOT EXISTS keyword_emoticons (
            keyword_id   INTEGER NOT NULL,
            slug         TEXT    NOT NULL,
            title        TEXT    NOT NULL,
            image_url    TEXT    NOT NULL,
            emot_idx     INTEGER,
            PRIMARY KEY (keyword_id, slug)
        );
    """)
    conn.commit()
    conn.close()


def snapshot_exists(ranking_base_dt: int) -> bool:
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM snapshots WHERE ranking_base_dt = ?", (ranking_base_dt,)
    ).fetchone()
    conn.close()
    return row is not None


def save_snapshot(captured_at: str, ranking_base_dt: int,
                  keywords: list, keyword_emots: dict) -> int | None:
    """스냅샷 저장. 이미 존재하면 None 반환."""
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO snapshots (captured_at, ranking_base_dt) VALUES (?,?)",
            (captured_at, ranking_base_dt),
        )
        conn.commit()
        if cur.lastrowid == 0:
            conn.close()
            return None  # 이미 존재

        snapshot_id = cur.lastrowid

        # 키워드 저장
        conn.executemany(
            "INSERT INTO snapshot_keywords (snapshot_id, rank, keyword_id, keyword_name) VALUES (?,?,?,?)",
            [(snapshot_id, i + 1, k["keywordId"], k["keyword"]) for i, k in enumerate(keywords)],
        )

        # 이모티콘 upsert (keyword별, 중복 없이)
        for keyword_id, emots in keyword_emots.items():
            conn.executemany(
                """INSERT OR REPLACE INTO keyword_emoticons
                   (keyword_id, slug, title, image_url, emot_idx)
                   VALUES (?,?,?,?,?)""",
                [(keyword_id, e["slug"], e["title"], e["imageUrl"], e.get("emotIdx"))
                 for e in emots],
            )

        conn.commit()
        return snapshot_id
    finally:
        conn.close()


def get_snapshots(limit: int = 100) -> list[dict]:
    """스냅샷 목록 + 각 스냅샷의 키워드 요약"""
    conn = get_conn()
    rows = conn.execute(
        """SELECT id, captured_at, ranking_base_dt
           FROM snapshots ORDER BY ranking_base_dt DESC LIMIT ?""",
        (limit,),
    ).fetchall()

    result = []
    for r in rows:
        kws = conn.execute(
            """SELECT keyword_id, keyword_name, rank
               FROM snapshot_keywords WHERE snapshot_id = ? ORDER BY rank""",
            (r["id"],),
        ).fetchall()
        result.append({
            "id": r["id"],
            "captured_at": r["captured_at"],
            "ranking_base_dt": r["ranking_base_dt"],
            "keywords": [{"keywordId": k["keyword_id"], "keyword": k["keyword_name"], "rank": k["rank"]} for k in kws],
        })

    conn.close()
    return result


def get_hourly_trends() -> dict:
    """시간대별(0~23시) 키워드 랭킹 집계 — 행=시간, 열=순위별 키워드"""
    conn = get_conn()

    meta = conn.execute("""
        SELECT COUNT(*)             AS cnt,
               MIN(ranking_base_dt) AS min_dt,
               MAX(ranking_base_dt) AS max_dt
        FROM snapshots
    """).fetchone()

    # 시간대별 스냅샷 수
    snap_rows = conn.execute("""
        SELECT CAST(strftime('%H', datetime(ranking_base_dt/1000, 'unixepoch', '+9 hours'))
                    AS INTEGER) AS hour,
               COUNT(*) AS snap_count
        FROM snapshots
        GROUP BY hour
    """).fetchall()
    snap_by_hour = {r["hour"]: r["snap_count"] for r in snap_rows}

    # 시간대 × 키워드 평균 순위
    rows = conn.execute("""
        SELECT
            CAST(strftime('%H', datetime(s.ranking_base_dt/1000, 'unixepoch', '+9 hours'))
                 AS INTEGER)       AS hour,
            sk.keyword_id,
            sk.keyword_name,
            ROUND(AVG(sk.rank), 1) AS avg_rank
        FROM snapshot_keywords sk
        JOIN snapshots s ON sk.snapshot_id = s.id
        GROUP BY hour, sk.keyword_id
        ORDER BY hour, avg_rank
    """).fetchall()
    conn.close()

    # 시간대별로 키워드 목록 구성
    hour_kws: dict[int, list] = {h: [] for h in range(24)}
    for r in rows:
        hour_kws[r["hour"]].append({
            "id":       r["keyword_id"],
            "name":     r["keyword_name"],
            "avg_rank": r["avg_rank"],
        })

    return {
        "snapshot_count": meta["cnt"],
        "min_dt":         meta["min_dt"],
        "max_dt":         meta["max_dt"],
        "hours": [
            {
                "hour":       h,
                "snap_count": snap_by_hour.get(h, 0),
                "keywords":   hour_kws[h],   # avg_rank 오름차순 정렬
            }
            for h in range(24)
        ],
    }


def get_snapshot_emoticons(snapshot_id: int, keyword_id: int) -> list[dict]:
    """특정 스냅샷 시점의 키워드 이모티콘 (keyword_id 기준 조회)"""
    conn = get_conn()
    # 해당 snapshot에 이 keyword_id가 실제로 있었는지 확인
    kw = conn.execute(
        "SELECT keyword_name FROM snapshot_keywords WHERE snapshot_id=? AND keyword_id=?",
        (snapshot_id, keyword_id),
    ).fetchone()
    if not kw:
        conn.close()
        return []

    emots = conn.execute(
        """SELECT slug, title, image_url, emot_idx
           FROM keyword_emoticons WHERE keyword_id = ? ORDER BY emot_idx""",
        (keyword_id,),
    ).fetchall()
    conn.close()
    return [dict(e) for e in emots]
