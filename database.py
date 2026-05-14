import requests


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
        "download_url":    str | None,
        "notice":          str | None,
        "is_maintenance":  bool
    }
    """
    defaults = {
        "current_version": None,
        "download_url": None,
        "notice": None,
        "is_maintenance": False,
    }
    try:
        response = requests.get(
            f"{_SUPABASE_URL}/rest/v1/settings",
            headers=_supabase_headers(),
            params={
                "select": "current_version,download_url,notice,is_maintenance",
                "id": "eq.1",
                "limit": "1",
            },
            timeout=10,
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

    반환값: [{"id": uuid_str, "name": str, "tier": str | None}, ...]
    """
    try:
        response = requests.get(
            f"{_SUPABASE_URL}/rest/v1/members",
            headers=_supabase_headers(),
            params={
                "select": "id,name,tier",
                "is_active": "eq.true",
                "order": "name",
            },
            timeout=10,
        )
        response.raise_for_status()
        return response.json() or []
    except Exception:
        return []


# ---------------------------------------------------------------------------
# matches — Google Apps Script로 전송
# ---------------------------------------------------------------------------

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
) -> None:
    """
    경기 결과를 Google Apps Script Web App에 POST합니다.

    시트 열 순서:
      A: tier_p1, B: name_p1, C: 승/패, D: 패/승,
      E: name_p2, F: tier_p2, G: map, H: match_type,
      K: played_at, L: replay_hash (중복 방지 지문)
    name_p1 은 항상 런처 사용자, player1_won 으로 승패 결정.
    """
    if not APPS_SCRIPT_URL:
        raise RuntimeError("APPS_SCRIPT_URL이 설정되지 않았습니다.")

    result1, result2 = ("승", "패") if player1_won else ("패", "승")
    row = [tier_p1, name_p1, result1, result2, name_p2, tier_p2, map, match_type, "", "", played_at, replay_hash]
    payload = {"rows": [row]}

    resp = requests.post(APPS_SCRIPT_URL, json=payload, timeout=15, allow_redirects=False)
    location = resp.headers.get("Location")
    if location:
        resp = requests.get(location, timeout=15)

    resp.raise_for_status()
    try:
        result = resp.json()
    except Exception:
        return  # HTTP 200 OK + non-JSON 응답 → 시트 기록 성공으로 간주
    if result.get("status") == "duplicate":
        raise DuplicateMatchError("이미 등록된 경기입니다. (다른 클랜원이 먼저 업로드)")
    if result.get("status") == "error":
        raise RuntimeError(result.get("message", "Apps Script 오류"))
