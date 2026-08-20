import hmac
import logging
import os
import threading
import time
from datetime import datetime, timezone, timedelta

from flask import Flask, Response, jsonify, render_template, request

import database
import scraper
import tts

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


# ── 나만의 TTS ──────────────────────────────────────────────────────────────
# 2인 개인용. TTS_USERS 환경변수가 없으면 기능 전체를 닫는다(fail closed).
#   TTS_USERS="승준:패스코드1,동료:패스코드2"

TTS_MAX_CHARS = int(os.environ.get("TTS_MAX_CHARS", "1000"))
TTS_DEMO_MAX_CHARS = 200   # 미리듣기는 짧게 — 호출 낭비를 막는다


def _tts_users() -> dict:
    """패스코드 → 소유자 이름 매핑.

    형식은 "이름:패스코드"를 쉼표로 이어 붙인 것.
    콜론 없이 패스코드만 적으면 둘이 공유하는 패스코드로 보고 소유자를 "공용"으로 둔다.
    패스코드에 콜론이 들어가도 첫 콜론에서만 쪼개므로 그대로 살아남는다.
    """
    raw = os.environ.get("TTS_USERS", "").strip()
    users = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if ":" in pair:
            name, _, code = pair.partition(":")
            name, code = name.strip(), code.strip()
        else:
            name, code = "공용", pair
        if name and code:
            users[code] = name
    return users


def _tts_owner():
    """X-TTS-Passcode 헤더를 검증해 소유자 이름을 반환. 실패 시 None."""
    users = _tts_users()
    if not users:
        return None
    sent = request.headers.get("X-TTS-Passcode", "")
    if not sent:
        return None
    for code, name in users.items():
        if hmac.compare_digest(sent, code):
            return name
    return None


def _tts_guard():
    """(소유자, 오류응답) 튜플. 오류응답이 None이 아니면 그걸 그대로 반환한다."""
    if not _tts_users():
        return None, (jsonify({"error": "서버에 TTS_USERS가 설정되지 않았거나 형식이 잘못돼 기능이 닫혀 있습니다."
                             " 형식: 패스코드 또는 이름:패스코드(쉼표로 여러 개)."}), 503)
    owner = _tts_owner()
    if owner is None:
        return None, (jsonify({"error": "패스코드가 올바르지 않습니다."}), 401)
    return owner, None


@app.route("/api/tts/login", methods=["POST"])
def api_tts_login():
    owner, err = _tts_guard()
    if err:
        return err
    providers = tts.available()
    return jsonify({
        "owner":     owner,
        "providers": providers,
        "app_max":   TTS_MAX_CHARS,
        "sample": {
            "min_sec":   tts.SAMPLE_MIN_SEC,
            "max_sec":   tts.SAMPLE_MAX_SEC,
            "max_bytes": tts.SAMPLE_MAX_BYTES,
        },
    })


@app.route("/api/tts/voices")
def api_tts_voices():
    owner, err = _tts_guard()
    if err:
        return err
    try:
        return jsonify(database.get_tts_voices())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/tts/voices", methods=["POST"])
def api_tts_create_voice():
    """샘플 오디오를 제공자로 넘겨 음성을 복제하고 참조를 저장한다."""
    owner, err = _tts_guard()
    if err:
        return err

    provider = (request.form.get("provider") or "").strip()
    label    = (request.form.get("label") or "").strip()
    upload   = request.files.get("file")

    if provider not in tts.PROVIDERS:
        return jsonify({"error": "제공자를 선택해 주세요."}), 400
    if not label:
        return jsonify({"error": "음성 이름을 입력해 주세요."}), 400
    if upload is None or not upload.filename:
        return jsonify({"error": "샘플 오디오 파일이 없습니다."}), 400

    ext = os.path.splitext(upload.filename)[1].lower()
    if ext not in (".wav", ".mp3"):
        return jsonify({"error": "wav 또는 mp3만 사용할 수 있습니다."}), 400

    blob = upload.read()
    if not blob:
        return jsonify({"error": "빈 파일입니다."}), 400
    if len(blob) > tts.SAMPLE_MAX_BYTES:
        return jsonify({"error": "샘플이 너무 큽니다(최대 25MB)."}), 400

    mimetype = "audio/wav" if ext == ".wav" else "audio/mpeg"
    try:
        voice_id = tts.clone(provider, label, "sample" + ext, blob, mimetype)
    except tts.TTSError as e:
        return jsonify({"error": str(e)}), 502

    try:
        row_id = database.add_tts_voice(
            provider, voice_id, label, owner, datetime.now(KST).isoformat()
        )
    except Exception as e:
        # 원격에는 만들어졌는데 DB 저장이 실패한 경우 — voice_id를 로그로 남겨 수동 복구 여지를 둔다
        log.error("음성 저장 실패 (provider=%s voice_id=%s): %s", provider, voice_id, e)
        return jsonify({"error": "음성은 생성됐지만 저장에 실패했습니다: " + str(e)}), 500

    return jsonify({"id": row_id, "provider": provider,
                    "voice_id": voice_id, "label": label, "owner": owner})


@app.route("/api/tts/voices/<int:row_id>", methods=["DELETE"])
def api_tts_delete_voice(row_id: int):
    owner, err = _tts_guard()
    if err:
        return err

    try:
        row = database.delete_tts_voice(row_id)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    if row is None:
        return jsonify({"error": "이미 삭제된 음성입니다."}), 404

    # DB에서는 지웠으니, 원격 삭제가 실패해도 경고만 남기고 성공으로 처리한다
    remote_error = None
    try:
        tts.delete(row["provider"], row["voice_id"])
    except tts.TTSError as e:
        remote_error = str(e)
        log.warning("원격 음성 삭제 실패 (수동 정리 필요): %s", remote_error)

    return jsonify({"deleted": row_id, "remote_error": remote_error})


@app.route("/api/tts/speak", methods=["POST"])
def api_tts_speak():
    """등록된 음성으로 텍스트를 합성해 mp3를 그대로 스트리밍한다."""
    owner, err = _tts_guard()
    if err:
        return err

    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    if not text:
        return jsonify({"error": "읽을 텍스트를 입력해 주세요."}), 400

    try:
        row_id = int(body.get("id"))
    except (TypeError, ValueError):
        return jsonify({"error": "음성을 선택해 주세요."}), 400

    try:
        row = database.get_tts_voice(row_id)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    if row is None:
        return jsonify({"error": "등록되지 않은 음성입니다."}), 404

    # 앱 상한과 제공자 하드 리밋 중 더 낮은 쪽을 적용
    limit = min(TTS_MAX_CHARS, tts.max_chars(row["provider"]))
    if len(text) > limit:
        return jsonify({
            "error": "글자 수 상한을 넘었습니다 ({}자 / 최대 {}자).".format(len(text), limit)
        }), 400

    opts = {
        "speed":      body.get("speed"),
        "pitch":      body.get("pitch"),
        "emotion":    body.get("emotion"),
        "intensity":  body.get("intensity"),
        "stability":  body.get("stability"),
        "similarity": body.get("similarity"),
    }

    try:
        audio = tts.speak(row["provider"], row["voice_id"], text, opts)
    except tts.TTSError as e:
        return jsonify({"error": str(e)}), 502

    log.info("합성 완료: owner=%s provider=%s chars=%d bytes=%d",
             owner, row["provider"], len(text), len(audio))
    return Response(audio, mimetype="audio/mpeg", headers={
        "Cache-Control": "no-store",
        "Content-Length": str(len(audio)),
    })


# ── 프리셋 목소리 탐색 ──────────────────────────────────────────────────────
# 설명 → 추천 → 미리듣기 → 목록에 담기. 추천은 Typecast 네이티브 기능이다.

@app.route("/api/tts/recommend")
def api_tts_recommend():
    owner, err = _tts_guard()
    if err:
        return err

    query = (request.args.get("q") or "").strip()
    if not query:
        return jsonify({"error": "찾고 싶은 목소리를 설명해 주세요."}), 400

    try:
        count = int(request.args.get("count", 5))
    except (TypeError, ValueError):
        count = 5

    try:
        return jsonify(tts.recommend_voices(query, count))
    except tts.TTSError as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/tts/demo", methods=["POST"])
def api_tts_demo():
    """목록에 담기 전에 프리셋 목소리를 미리 들어본다."""
    owner, err = _tts_guard()
    if err:
        return err

    body     = request.get_json(silent=True) or {}
    voice_id = (body.get("voice_id") or "").strip()
    text     = (body.get("text") or "").strip()

    if not voice_id.startswith(("tc_", "uc_")):
        return jsonify({"error": "올바른 Typecast 목소리 ID가 아닙니다."}), 400
    if not text:
        return jsonify({"error": "미리듣기 문장을 입력해 주세요."}), 400
    if len(text) > TTS_DEMO_MAX_CHARS:
        return jsonify({
            "error": "미리듣기 문장은 {}자까지입니다 (현재 {}자).".format(
                TTS_DEMO_MAX_CHARS, len(text))
        }), 400

    opts = {
        "speed":     body.get("speed"),
        "emotion":   body.get("emotion"),
        "intensity": body.get("intensity"),
    }
    try:
        audio = tts.speak("typecast", voice_id, text, opts)
    except tts.TTSError as e:
        return jsonify({"error": str(e)}), 502

    log.info("미리듣기: owner=%s voice_id=%s chars=%d", owner, voice_id, len(text))
    return Response(audio, mimetype="audio/mpeg", headers={
        "Cache-Control": "no-store",
        "Content-Length": str(len(audio)),
    })


@app.route("/api/tts/voices/preset", methods=["POST"])
def api_tts_save_preset():
    """추천으로 찾은 프리셋 목소리를 목록에 담는다(복제가 아니라 참조만 저장)."""
    owner, err = _tts_guard()
    if err:
        return err

    body     = request.get_json(silent=True) or {}
    voice_id = (body.get("voice_id") or "").strip()
    label    = (body.get("label") or "").strip()

    if not voice_id.startswith("tc_"):
        return jsonify({"error": "프리셋 목소리 ID(tc_)가 아닙니다."}), 400
    if not label:
        return jsonify({"error": "이름을 입력해 주세요."}), 400

    try:
        row_id = database.add_tts_voice(
            "typecast", voice_id, label[:60], owner, datetime.now(KST).isoformat()
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"id": row_id, "provider": "typecast", "voice_id": voice_id,
                    "label": label[:60], "owner": owner, "preset": True})


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
