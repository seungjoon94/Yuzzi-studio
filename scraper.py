"""카카오 이모티콘 트렌딩 키워드 수집기"""
import time
import logging
from datetime import datetime, timezone, timedelta

import requests

import database

log = logging.getLogger(__name__)

KAKAO_API = "https://e.kakao.com/api/keyword/rankings"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "Chrome/148.0.0.0 Safari/537.36",
    "Referer": "https://e.kakao.com/",
    "Accept": "application/json, text/plain, */*",
}
SEED_KEYWORD_ID = 156
KST = timezone(timedelta(hours=9))


def current_slot_ms() -> int:
    return (int(time.time() * 1000) // 600_000) * 600_000


def fetch_ranking(keyword_id: int) -> dict:
    resp = requests.get(
        KAKAO_API,
        params={"keywordId": keyword_id},
        headers=HEADERS,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def capture_and_save() -> dict:
    """현재 트렌딩 데이터를 수집해 DB에 저장. 결과 요약 반환."""
    slot_ms = current_slot_ms()

    # 이미 이 슬롯이 저장돼 있으면 스킵
    if database.snapshot_exists(slot_ms):
        log.info("슬롯 %s 이미 저장됨, 스킵", slot_ms)
        return {"skipped": True, "ranking_base_dt": slot_ms}

    log.info("수집 시작: slot=%s", slot_ms)

    # 1. 키워드 목록 가져오기 (rankingBaseDateTime 미지정 → 카카오 최신 데이터 반환)
    first = fetch_ranking(SEED_KEYWORD_ID)
    keywords: list = first.get("keywords", [])
    ranking_base_dt: int = slot_ms

    # 2. 키워드별 이모티콘 수집
    keyword_emots: dict[int, list] = {}

    # 첫 번째 호출 결과 재사용
    keyword_emots[SEED_KEYWORD_ID] = first.get("emots", [])

    for kw in keywords:
        kid = kw["keywordId"]
        if kid in keyword_emots:
            continue
        try:
            data = fetch_ranking(kid)
            keyword_emots[kid] = data.get("emots", [])
            time.sleep(0.3)
        except Exception as e:
            log.warning("키워드 %s(%s) 이모티콘 수집 실패: %s", kw["keyword"], kid, e)
            keyword_emots[kid] = []

    captured_at = datetime.now(KST).isoformat()
    snapshot_id = database.save_snapshot(captured_at, ranking_base_dt, keywords, keyword_emots)

    total_emots = sum(len(v) for v in keyword_emots.values())
    log.info("저장 완료: snapshot_id=%s, 키워드=%d, 이모티콘=%d",
             snapshot_id, len(keywords), total_emots)

    return {
        "skipped": False,
        "snapshot_id": snapshot_id,
        "ranking_base_dt": ranking_base_dt,
        "keyword_count": len(keywords),
        "emot_count": total_emots,
    }
