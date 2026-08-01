import threading
import time
from typing import Callable

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class DuplicateMatchError(Exception):
    """같은 replay_hash가 이미 시트에 존재하는 경우."""


# Google Apps Script Web App 배포 URL — 경기 결과 전송 대상
APPS_SCRIPT_URL: str = "https://script.google.com/macros/s/AKfycbxBp22Aq9g_5S_r80gh4fMopwAVekjnxOWH_0ERlnTlcGiNWZ-SBok8IsjzRkJQjFFo/exec"

# Supabase — 클랜원 목록 및 설정 조회용 (SDK 없이 REST API 직접 호출)
_SUPABASE_URL: str = "https://ypwcyorlzwyegjtwcqzw.supabase.co"
_SUPABASE_KEY: str = "sb_publishable_kz4wSAI5TQ85_-SQ1zYAdg_okLdNC6w"


def _supabase_headers() -> dict:
    return {
        "apikey": _SUPABASE_KEY,
        "Authorization": f"Bearer {_SUPABASE_KEY}",
    }


# ---------------------------------------------------------------------------
# settings (id = 1 단일 행)
# ---------------------------------------------------------------------------

def fetch_settings() -> dict:
    """
    settings 테이블의 id=1 행에서 앱 전역 설정을 가져옵니다.

    반환값:
    {
        "current_version": str | None,
        "screp_url":    str | None,
        "notice":          str | None,
        "is_maintenance":  bool
    }
    """
    defaults = {
        "current_version": None,
        "screp_url": None,
        "notice": None,
        "is_maintenance": False,
    }
    try:
        response = requests.get(
            f"{_SUPABASE_URL}/rest/v1/settings",
            headers=_supabase_headers(),
            params={
                "select": "current_version,screp_url,notice,is_maintenance",
                "id": "eq.1",
                "limit": "1",
            },
            timeout=10,
            verify=False,
        )
        response.raise_for_status()
        data = response.json()
        if data:
            defaults.update(data[0])
    except Exception:
        pass
    return defaults


# ---------------------------------------------------------------------------
# members
# ---------------------------------------------------------------------------

def fetch_all_members() -> list[dict]:
    """
    is_active=True인 모든 클랜원을 이름순으로 반환합니다.

    반환값: [{"id": uuid_str, "name": str, "tier": str | None, "race": str | None}, ...]
    race 값: "T" (테란), "Z" (저그), "P" (프로토스), None (미설정)
    """
    try:
        response = requests.get(
            f"{_SUPABASE_URL}/rest/v1/members",
            headers=_supabase_headers(),
            params={
                "select": "id,name,tier,race",
                "is_active": "eq.true",
                "order": "name",
            },
            timeout=10,
            verify=False,
        )
        response.raise_for_status()
        return response.json() or []
    except Exception:
        return []


# ---------------------------------------------------------------------------
# 런처 사용 통계 — members.last_launcher_used_at 갱신 (fire-and-forget)
# ---------------------------------------------------------------------------

def _mark_launcher_used_async(name: str) -> None:
    try:
        requests.post(
            f"{_SUPABASE_URL}/rest/v1/rpc/mark_launcher_used",
            headers=_supabase_headers(),
            json={"member_name": name},
            timeout=5,
            verify=False,
        )
    except Exception:
        pass


def _mark_launcher_used(name: str) -> None:
    """전적 전송 시 런처 사용자의 마지막 사용 시각을 백그라운드로 기록합니다.

    실패해도 실제 전적 전송 로직에는 영향을 주지 않으며, 응답도 기다리지 않습니다.
    """
    threading.Thread(target=_mark_launcher_used_async, args=(name,), daemon=True).start()


# ---------------------------------------------------------------------------
# matches — Google Apps Script로 전송
# ---------------------------------------------------------------------------

_SEND_MAX_RETRIES = 2   # 최초 시도 포함 총 3회
_SEND_RETRY_DELAY = 3   # 초


def _post_match_row(payload: dict) -> dict:
    """Apps Script에 1회 POST하고 JSON 응답을 반환합니다. 실패 시 예외를 그대로 던집니다."""
    resp = requests.post(APPS_SCRIPT_URL, json=payload, timeout=15, allow_redirects=False, verify=False)
    location = resp.headers.get("Location")
    if location:
        resp = requests.get(location, timeout=15, verify=False)

    resp.raise_for_status()
    try:
        return resp.json()
    except Exception:
        body = resp.text[:300] if resp.text else "(빈 응답)"
        raise RuntimeError(f"Apps Script 응답이 JSON이 아닙니다. 상태코드={resp.status_code}, 내용={body}")


def send_match(
    *,
    tier_p1: str = "",
    name_p1: str,
    tier_p2: str = "",
    name_p2: str,
    player1_won: bool,
    map: str,
    match_type: str,
    played_at: str = "",
    replay_hash: str = "",
    mark_usage: bool = True,
    on_retry: Callable[[str], None] | None = None,
) -> None:
    """
    경기 결과를 Google Apps Script Web App에 POST합니다.

    시트 열 순서:
      A: tier_p1, B: name_p1, C: 승/패, D: 패/승,
      E: name_p2, F: tier_p2, G: map, H: match_type,
      K: played_at, L: replay_hash (중복 방지 지문)
    name_p1 은 항상 런처 사용자, player1_won 으로 승패 결정.

    mark_usage: name_p1을 런처 실사용자로 보고 last_launcher_used_at을 갱신할지 여부.
    옵저버 모드처럼 name_p1이 리플레이 속 플레이어일 뿐 실제 런처 사용자가 아닐 수 있는
    호출부에서는 False로 넘겨야 합니다.

    네트워크 타임아웃/연결 오류/비정상 응답(Apps Script 과부하 등)은 일시적일 수 있으므로
    최대 _SEND_MAX_RETRIES회까지 재시도합니다. 중복/업무 오류(duplicate, error)는
    재시도해도 결과가 같으므로 즉시 예외를 던집니다.

    on_retry: 재시도가 발생할 때마다(대기 직전) 사람이 읽을 메시지 문자열로 호출됩니다.
    호출부에서 로그 패널에 남기는 용도로 사용합니다.
    """
    if not APPS_SCRIPT_URL:
        raise RuntimeError("APPS_SCRIPT_URL이 설정되지 않았습니다.")

    result1, result2 = ("승", "패") if player1_won else ("패", "승")
    row = [tier_p1, name_p1, result1, result2, name_p2, tier_p2, map, match_type, "", "", played_at, replay_hash]
    payload = {"rows": [row]}

    for attempt in range(_SEND_MAX_RETRIES + 1):
        try:
            result = _post_match_row(payload)
            break
        except (requests.exceptions.RequestException, RuntimeError) as e:
            if attempt >= _SEND_MAX_RETRIES:
                raise
            if on_retry:
                on_retry(
                    f"[재시도 {attempt + 1}/{_SEND_MAX_RETRIES}] 응답 지연/오류로 "
                    f"{_SEND_RETRY_DELAY}초 후 재시도합니다: {e}"
                )
            time.sleep(_SEND_RETRY_DELAY)

    if mark_usage:
        _mark_launcher_used(name_p1)
    if result.get("status") == "duplicate":
        raise DuplicateMatchError("이미 등록된 경기입니다. (다른 클랜원이 먼저 업로드)")
    if result.get("status") == "error":
        raise RuntimeError(result.get("message", "Apps Script 오류"))
