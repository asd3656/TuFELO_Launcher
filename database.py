import requests


class DuplicateMatchError(Exception):
    """같은 replay_hash가 이미 시트에 존재하는 경우."""


# Google Apps Script Web App 배포 URL — 경기 결과 전송 대상
APPS_SCRIPT_URL: str = "https://script.google.com/macros/s/AKfycbwfIsE4WOWatCQHUqfofa7NEjJoU5xk05z00Sjonm2XiGP0FiUcs3bdqJ_gYaMurXM-/exec"

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
        "screp_url":       str | None,
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
    tier_w: str,
    name_w: str,
    tier_l: str,
    name_l: str,
    map: str,
    match_type: str,
    played_at: str = "",
    replay_hash: str = "",
) -> None:
    """
    경기 결과를 Google Apps Script Web App에 POST합니다.

    시트 열 순서:
      A: tier_w, B: name_w, C: "승", D: "패",
      E: name_l, F: tier_l, G: map, H: match_type,
      K: played_at, L: replay_hash (중복 방지 지문)
    """
    if not APPS_SCRIPT_URL:
        raise RuntimeError("APPS_SCRIPT_URL이 설정되지 않았습니다.")

    row = [tier_w, name_w, "승", "패", name_l, tier_l, map, match_type, "", "", played_at, replay_hash]
    payload = {"rows": [row]}

    resp = requests.post(APPS_SCRIPT_URL, json=payload, timeout=15, allow_redirects=False)
    location = resp.headers.get("Location")
    if location:
        resp = requests.get(location, timeout=15)

    resp.raise_for_status()
    result = resp.json()
    if result.get("status") == "duplicate":
        raise DuplicateMatchError("이미 등록된 경기입니다. (다른 클랜원이 먼저 업로드)")
    if result.get("status") == "error":
        raise RuntimeError(result.get("message", "Apps Script 오류"))
