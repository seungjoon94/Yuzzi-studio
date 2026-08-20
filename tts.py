"""나만의 TTS — 음성 복제/합성 제공자 어댑터

ElevenLabs와 Typecast를 같은 인터페이스로 감싼다.
엔드포인트와 요청 필드는 모두 이 파일 상단 상수에 모아뒀으니
제공자 스펙이 바뀌면 여기만 고치면 된다.

  ElevenLabs  POST   /v1/voices/add           (multipart) → voice_id
              POST   /v1/text-to-speech/{id}  (json)      → mp3 bytes
              DELETE /v1/voices/{id}
  Typecast    POST   /v1/voices/clone         (multipart) → uc_ 접두사 voice_id
              POST   /v1/text-to-speech       (json)      → mp3 bytes
              DELETE /v1/voices/{id}
"""
import logging
import os
import time

import requests

log = logging.getLogger(__name__)


class TTSError(Exception):
    """제공자 호출 실패. 사용자에게 그대로 보여줄 메시지를 담는다."""


TIMEOUT_CLONE = 120   # 샘플 업로드 + 클로닝은 오래 걸린다
TIMEOUT_SPEAK = 90
TIMEOUT_MISC  = 30

# ── ElevenLabs ──────────────────────────────────────────────────────────────
ELEVEN_API    = "https://api.elevenlabs.io/v1"
ELEVEN_MODEL  = "eleven_multilingual_v2"   # 한국어(kor) 명시 지원
ELEVEN_FORMAT = "mp3_44100_128"
ELEVEN_MAX    = 10_000                     # multilingual_v2 요청당 글자 수 상한

# ── Typecast ────────────────────────────────────────────────────────────────
TYPECAST_API   = "https://api.typecast.ai"
TYPECAST_MODEL = "ssfm-v30"                # 최신 권장 엔진
TYPECAST_LANG  = "kor"                     # ISO 639-3
TYPECAST_MAX   = 2_000                     # API 하드 리밋
TYPECAST_EMOTIONS = ("normal", "happy", "sad", "angry",
                     "whisper", "toneup", "tonedown")
TYPECAST_QUERY_MAX = 500          # 추천 query 길이 상한
TYPECAST_REC_MAX   = 10           # 추천 개수 상한
VOICE_CACHE_TTL    = 6 * 3600     # 목소리 카탈로그 캐시 수명(초)

# 클로닝 샘플 제약 — 둘 중 더 엄격한 Typecast 기준(5~150초, 25MB, wav/mp3)
SAMPLE_MAX_BYTES = 25 * 1024 * 1024
SAMPLE_MIN_SEC   = 5
SAMPLE_MAX_SEC   = 150


def _clamp(value, lo, hi, default):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    return max(lo, min(hi, v))


def _eleven_key():
    return os.environ.get("ELEVENLABS_API_KEY", "").strip()


def _typecast_key():
    return os.environ.get("TYPECAST_API_KEY", "").strip()


def _fail(label, resp):
    """응답에서 쓸만한 오류 메시지를 뽑아 TTSError로 올린다."""
    detail = ""
    try:
        body = resp.json()
        detail = body.get("detail") or body.get("message") or body.get("error") or ""
        if isinstance(detail, dict):
            detail = detail.get("message") or str(detail)
        elif isinstance(detail, list) and detail:
            detail = str(detail[0])
    except Exception:
        detail = (resp.text or "")[:200]

    hint = {
        401: "API 키가 잘못되었거나 만료됐습니다.",
        403: "현재 요금제에서 허용되지 않는 요청입니다.",
        404: "존재하지 않는 음성입니다. 제공자 쪽에서 이미 삭제됐을 수 있습니다.",
        422: "요청 값이 유효하지 않습니다.",
        429: "호출 한도를 초과했습니다. 잠시 후 다시 시도하세요.",
    }.get(resp.status_code, "")

    msg = "{} 오류 (HTTP {})".format(label, resp.status_code)
    if hint:
        msg += " — " + hint
    if detail:
        msg += " [{}]".format(str(detail)[:200])
    log.warning("제공자 호출 실패: %s", msg)
    raise TTSError(msg)


def _post(label, *args, **kwargs):
    """requests.post를 감싸 네트워크 예외를 TTSError로 통일한다."""
    try:
        return requests.post(*args, **kwargs)
    except requests.Timeout:
        raise TTSError(label + " 응답이 시간 내에 오지 않았습니다. 다시 시도해 주세요.")
    except requests.RequestException as e:
        raise TTSError("{} 연결 실패: {}".format(label, e))


def _http_get(label, url, headers, params=None, timeout=TIMEOUT_MISC):
    """requests.get을 감싸 네트워크 예외를 TTSError로 통일한다."""
    try:
        return requests.get(url, headers=headers, params=params, timeout=timeout)
    except requests.Timeout:
        raise TTSError(label + " 응답이 시간 내에 오지 않았습니다. 다시 시도해 주세요.")
    except requests.RequestException as e:
        raise TTSError("{} 연결 실패: {}".format(label, e))


def _delete(label, url, headers, ok_codes=(200,)):
    """requests.delete를 감싸 네트워크 예외를 TTSError로 통일한다."""
    try:
        resp = requests.delete(url, headers=headers, timeout=TIMEOUT_MISC)
    except requests.RequestException as e:
        raise TTSError("{} 연결 실패: {}".format(label, e))
    if resp.status_code not in ok_codes:
        _fail(label, resp)


# ── ElevenLabs 구현 ─────────────────────────────────────────────────────────

def _eleven_clone(label, filename, blob, mimetype):
    resp = _post(
        "ElevenLabs",
        ELEVEN_API + "/voices/add",
        headers={"xi-api-key": _eleven_key()},
        data={"name": label, "remove_background_noise": "true"},
        files=[("files", (filename, blob, mimetype))],
        timeout=TIMEOUT_CLONE,
    )
    if resp.status_code != 200:
        _fail("ElevenLabs", resp)

    data = resp.json()
    voice_id = data.get("voice_id")
    if not voice_id:
        raise TTSError("ElevenLabs가 voice_id를 반환하지 않았습니다.")
    if data.get("requires_verification"):
        log.warning("ElevenLabs 음성 %s: 추가 본인 인증이 필요한 상태", voice_id)
    return voice_id


def _eleven_speak(voice_id, text, opts):
    resp = _post(
        "ElevenLabs",
        "{}/text-to-speech/{}".format(ELEVEN_API, voice_id),
        params={"output_format": ELEVEN_FORMAT},
        headers={"xi-api-key": _eleven_key(), "Content-Type": "application/json"},
        json={
            "text": text,
            "model_id": ELEVEN_MODEL,
            "voice_settings": {
                "stability":         _clamp(opts.get("stability"),  0.0, 1.0, 0.5),
                "similarity_boost":  _clamp(opts.get("similarity"), 0.0, 1.0, 0.75),
                "use_speaker_boost": True,
                # 문서에 범위 명시가 없어 안전한 구간으로 제한
                "speed":             _clamp(opts.get("speed"), 0.7, 1.2, 1.0),
            },
        },
        timeout=TIMEOUT_SPEAK,
    )
    if resp.status_code != 200:
        _fail("ElevenLabs", resp)
    return resp.content


def _eleven_delete(voice_id):
    _delete(
        "ElevenLabs",
        "{}/voices/{}".format(ELEVEN_API, voice_id),
        {"xi-api-key": _eleven_key()},
    )


# ── Typecast 구현 ───────────────────────────────────────────────────────────

def _typecast_clone(label, filename, blob, mimetype):
    resp = _post(
        "Typecast",
        TYPECAST_API + "/v1/voices/clone",
        headers={"X-API-KEY": _typecast_key()},
        data={"name": label[:30], "model": TYPECAST_MODEL},
        files={"file": (filename, blob, mimetype)},
        timeout=TIMEOUT_CLONE,
    )
    if resp.status_code != 200:
        _fail("Typecast", resp)

    voice_id = resp.json().get("voice_id")
    if not voice_id:
        raise TTSError("Typecast가 voice_id를 반환하지 않았습니다.")
    return voice_id


def _typecast_speak(voice_id, text, opts):
    body = {
        "voice_id": voice_id,
        "text": text,
        "model": TYPECAST_MODEL,
        "language": TYPECAST_LANG,
        "output": {
            "volume":       100,
            "audio_pitch":  int(_clamp(opts.get("pitch"), -12, 12, 0)),
            "audio_tempo":  _clamp(opts.get("speed"), 0.5, 2.0, 1.0),
            "audio_format": "mp3",
        },
    }

    emotion = (opts.get("emotion") or "").strip()
    if emotion and emotion != "normal" and emotion in TYPECAST_EMOTIONS:
        body["prompt"] = {
            "emotion_type":      "preset",
            "emotion_preset":    emotion,
            "emotion_intensity": _clamp(opts.get("intensity"), 0.0, 2.0, 1.0),
        }

    resp = _post(
        "Typecast",
        TYPECAST_API + "/v1/text-to-speech",
        headers={"X-API-KEY": _typecast_key(), "Content-Type": "application/json"},
        json=body,
        timeout=TIMEOUT_SPEAK,
    )
    if resp.status_code != 200:
        _fail("Typecast", resp)
    return resp.content


def _typecast_delete(voice_id):
    _delete(
        "Typecast",
        "{}/v1/voices/{}".format(TYPECAST_API, voice_id),
        {"X-API-KEY": _typecast_key()},
        ok_codes=(200, 204),
    )


# ── Typecast 프리셋 목소리 탐색 ─────────────────────────────────────────────
# 자연어 설명 → 목소리 추천은 Typecast가 자체 엔드포인트로 제공한다(LLM 불필요).

_voice_cache = {"at": 0.0, "data": {}}


def _typecast_require():
    key = _typecast_key()
    if not key:
        raise TTSError("Typecast API 키가 서버에 설정되지 않았습니다.")
    return key


def voice_catalog(force=False):
    """voice_id → 메타데이터 맵. 700여 건이라 한 번 받아 캐시한다."""
    now = time.time()
    if not force and _voice_cache["data"] and now - _voice_cache["at"] < VOICE_CACHE_TTL:
        return _voice_cache["data"]

    resp = _http_get(
        "Typecast",
        TYPECAST_API + "/v2/voices",
        {"X-API-KEY": _typecast_require()},
        params={"model": TYPECAST_MODEL},
        timeout=TIMEOUT_SPEAK,
    )
    if resp.status_code != 200:
        _fail("Typecast", resp)

    data = resp.json()
    if not isinstance(data, list):
        raise TTSError("Typecast 목소리 목록 응답 형식이 예상과 다릅니다.")

    catalog = {v["voice_id"]: v for v in data if isinstance(v, dict) and v.get("voice_id")}
    _voice_cache["data"] = catalog
    _voice_cache["at"] = now
    log.info("Typecast 목소리 카탈로그 갱신: %d건", len(catalog))
    return catalog


def _emotions_for(meta):
    """이 목소리가 현재 엔진(ssfm-v30)에서 지원하는 감정 목록."""
    for m in meta.get("models") or []:
        if m.get("version") == TYPECAST_MODEL:
            return list(m.get("emotions") or [])
    return []


def recommend_voices(query, count=5):
    """자연어 설명으로 프리셋 목소리를 추천받고 메타데이터를 붙여 반환."""
    key = _typecast_require()
    query = (query or "").strip()
    if not query:
        raise TTSError("찾고 싶은 목소리를 설명해 주세요.")

    resp = _http_get(
        "Typecast",
        TYPECAST_API + "/v1/voices/recommendations",
        {"X-API-KEY": key},
        params={
            "query": query[:TYPECAST_QUERY_MAX],
            "count": int(_clamp(count, 1, TYPECAST_REC_MAX, 5)),
        },
        timeout=TIMEOUT_SPEAK,
    )
    if resp.status_code != 200:
        _fail("Typecast", resp)

    data = resp.json()
    if not isinstance(data, list):
        raise TTSError("Typecast 추천 응답 형식이 예상과 다릅니다.")

    # 카탈로그 조회가 실패해도 추천 자체는 살린다
    try:
        catalog = voice_catalog()
    except TTSError as e:
        log.warning("카탈로그 조회 실패, 메타데이터 없이 반환: %s", e)
        catalog = {}

    out = []
    for v in data:
        if not isinstance(v, dict) or not v.get("voice_id"):
            continue
        meta = catalog.get(v["voice_id"]) or {}
        out.append({
            "voice_id":  v["voice_id"],
            "voice_name": v.get("voice_name") or meta.get("voice_name") or "",
            "score":     v.get("score"),
            "gender":    meta.get("gender"),
            "age":       meta.get("age"),
            "use_cases": list(meta.get("use_cases") or [])[:3],
            "emotions":  _emotions_for(meta),
        })

    log.info("프리셋 추천: query=%r → %d건", query[:60], len(out))
    return out


def catalog_facets():
    """필터 선택지를 카탈로그에서 뽑아낸다. 하드코딩보다 정확하다."""
    cat = voice_catalog()
    genders, ages, uses = {}, {}, {}

    for v in cat.values():
        if v.get("voice_type") == "custom":
            continue
        g = v.get("gender")
        if g:
            genders[g] = genders.get(g, 0) + 1
        a = v.get("age")
        if a:
            ages[a] = ages.get(a, 0) + 1
        for u in v.get("use_cases") or []:
            uses[str(u)] = uses.get(str(u), 0) + 1

    def rank(d):
        # 많이 쓰이는 값부터, 같으면 이름순
        return [{"value": k, "count": n}
                for k, n in sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))]

    return {
        "total":    sum(1 for v in cat.values() if v.get("voice_type") != "custom"),
        "gender":   rank(genders),
        "age":      rank(ages),
        "use_case": rank(uses),
    }


def search_voices(name=None, gender=None, age=None, use_case=None, limit=30):
    """캐시된 카탈로그를 조건으로 걸러 반환. Typecast 추가 호출이 없다."""
    cat = voice_catalog()

    needle   = (name or "").strip().lower()
    gender   = ((gender or "").strip().lower() or None)
    age      = ((age or "").strip().lower() or None)
    use_case = ((use_case or "").strip().lower() or None)
    lim      = int(_clamp(limit, 1, 100, 30))

    hits = []
    for v in cat.values():
        # 복제 음성은 이미 내 목록에 있으므로 탐색 대상이 아니다
        if v.get("voice_type") == "custom":
            continue
        if needle and needle not in (v.get("voice_name") or "").lower():
            continue
        if gender and (v.get("gender") or "").lower() != gender:
            continue
        if age and (v.get("age") or "").lower() != age:
            continue
        if use_case:
            cases = [str(u).lower() for u in (v.get("use_cases") or [])]
            if use_case not in cases:
                continue
        hits.append(v)

    hits.sort(key=lambda v: (v.get("voice_name") or "").lower())

    voices = [{
        "voice_id":   v["voice_id"],
        "voice_name": v.get("voice_name") or "",
        "score":      None,              # 조건 검색에는 매칭 점수가 없다
        "gender":     v.get("gender"),
        "age":        v.get("age"),
        "use_cases":  list(v.get("use_cases") or [])[:3],
        "emotions":   _emotions_for(v),
    } for v in hits[:lim]]

    log.info("조건 검색: name=%r gender=%s age=%s use_case=%s → %d/%d건",
             needle, gender, age, use_case, len(voices), len(hits))
    return {"total": len(hits), "shown": len(voices), "voices": voices}


def subscription():
    """현재 요금제·크레딧·한도 조회. 크레딧은 1자 = 1크레딧이다."""
    resp = _http_get(
        "Typecast",
        TYPECAST_API + "/v1/users/me/subscription",
        {"X-API-KEY": _typecast_require()},
    )
    if resp.status_code != 200:
        _fail("Typecast", resp)

    data = resp.json()
    if not isinstance(data, dict):
        raise TTSError("Typecast 구독 응답 형식이 예상과 다릅니다.")

    credits = data.get("credits") or {}
    limits  = data.get("limits") or {}
    total   = credits.get("plan_credits")
    used    = credits.get("used_credits")

    remaining = None
    if isinstance(total, (int, float)) and isinstance(used, (int, float)):
        remaining = max(0, total - used)

    return {
        "plan":              data.get("plan"),
        "plan_credits":      total,
        "used_credits":      used,
        "remaining_credits": remaining,
        "concurrency_limit": limits.get("concurrency_limit"),
        "custom_voice_slot": limits.get("custom_voice_slot"),
    }


# ── 제공자 레지스트리 ───────────────────────────────────────────────────────

PROVIDERS = {
    "elevenlabs": {
        "label":     "ElevenLabs",
        "key":       _eleven_key,
        "clone":     _eleven_clone,
        "speak":     _eleven_speak,
        "delete":    _eleven_delete,
        "max_chars": ELEVEN_MAX,
        "emotions":  (),                    # 감정 프리셋 미지원
    },
    "typecast": {
        "label":     "Typecast",
        "key":       _typecast_key,
        "clone":     _typecast_clone,
        "speak":     _typecast_speak,
        "delete":    _typecast_delete,
        "max_chars": TYPECAST_MAX,
        "emotions":  TYPECAST_EMOTIONS,
    },
}


def _get(provider):
    p = PROVIDERS.get(provider)
    if p is None:
        raise TTSError("알 수 없는 제공자: " + str(provider))
    if not p["key"]():
        raise TTSError(p["label"] + " API 키가 서버에 설정되지 않았습니다.")
    return p


def available():
    """API 키가 설정된 제공자만 반환. UI가 이걸로 선택지를 만든다."""
    return [
        {
            "name":      name,
            "label":     p["label"],
            "max_chars": p["max_chars"],
            "emotions":  list(p["emotions"]),
        }
        for name, p in PROVIDERS.items()
        if p["key"]()
    ]


def max_chars(provider):
    p = PROVIDERS.get(provider)
    return p["max_chars"] if p else 0


def clone(provider, label, filename, blob, mimetype):
    """샘플 오디오로 음성을 복제하고 제공자가 발급한 voice_id를 반환."""
    p = _get(provider)
    log.info("클로닝 요청: provider=%s label=%s bytes=%d", provider, label, len(blob))
    voice_id = p["clone"](label, filename, blob, mimetype)
    log.info("클로닝 완료: provider=%s voice_id=%s", provider, voice_id)
    return voice_id


def speak(provider, voice_id, text, opts=None):
    """텍스트를 mp3 바이트로 합성해 반환."""
    p = _get(provider)
    log.info("합성 요청: provider=%s voice_id=%s chars=%d", provider, voice_id, len(text))
    return p["speak"](voice_id, text, opts or {})


def delete(provider, voice_id):
    """제공자 쪽에서 복제 음성을 삭제한다."""
    _get(provider)["delete"](voice_id)
    log.info("원격 음성 삭제: provider=%s voice_id=%s", provider, voice_id)
