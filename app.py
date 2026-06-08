import logging
import os
import threading
import time
from datetime import datetime, timezone, timedelta

from flask import Flask, jsonify, render_template

import database
import scraper

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

app = Flask(__name__)

HEADERS = scraper.HEADERS
KST = timezone(timedelta(hours=9))


# ── 실시간 API ──────────────────────────────────────────────────────────────

@app.route("/api/keywords")
def api_keywords():
    try:
        data = scraper.fetch_ranking(scraper.SEED_KEYWORD_ID)
        return jsonify({
            "keywords": data.get("keywords", []),
            "rankingBaseDateTime": scraper.current_slot_ms(),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/emoticons/<int:keyword_id>")
def api_emoticons(keyword_id: int):
    try:
        data = scraper.fetch_ranking(keyword_id)
        kw_name = next(
            (k["keyword"] for k in data.get("keywords", []) if k["keywordId"] == keyword_id),
            "",
        )
        return jsonify({
            "emots": data.get("emots", []),
            "keyword": kw_name,
            "rankingBaseDateTime": scraper.current_slot_ms(),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── 히스토리 API ────────────────────────────────────────────────────────────

@app.route("/api/snapshots")
def api_snapshots():
    try:
        return jsonify(database.get_snapshots(limit=200))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/snapshots/<int:snapshot_id>/emoticons/<int:keyword_id>")
def api_snapshot_emoticons(snapshot_id: int, keyword_id: int):
    try:
        emots = database.get_snapshot_emoticons(snapshot_id, keyword_id)
        return jsonify({"emots": emots})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/hourly-trends")
def api_hourly_trends():
    try:
        return jsonify(database.get_hourly_trends())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/capture", methods=["POST"])
def api_capture():
    """수동 수집 트리거"""
    try:
        result = scraper.capture_and_save()
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── 페이지 ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


# ── 백그라운드 스케줄러 ─────────────────────────────────────────────────────

def _scheduler():
    """서버 시작 시 즉시 1회 수집, 이후 매 정각마다 수집"""
    time.sleep(2)  # Flask 완전 시작 대기
    while True:
        try:
            result = scraper.capture_and_save()
            log.info("스케줄 수집 결과: %s", result)
        except Exception as e:
            log.error("스케줄 수집 오류: %s", e)

        # 다음 정각 +5분까지 대기
        now = time.time()
        next_hour = (now // 3600 + 1) * 3600 + 300
        sleep_secs = next_hour - now
        log.info("다음 수집까지 %.0f초 (다음 정각 +5분)", sleep_secs)
        time.sleep(sleep_secs)


def start_scheduler():
    t = threading.Thread(target=_scheduler, daemon=True, name="scheduler")
    t.start()
    log.info("백그라운드 스케줄러 시작 (1시간 간격)")


# ── 앱 초기화 ───────────────────────────────────────────────────────────────

database.init_db()
start_scheduler()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
