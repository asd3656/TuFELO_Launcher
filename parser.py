"""
SC:BW / Remastered .rep 파일 파서.
외부 CLI 도구 screp (https://github.com/icza/screp) 의 JSON 출력을 분석합니다.

screp JSON 최상위 구조:
  Header   → 게임 메타데이터 (방 제목, 맵, 플레이어 목록, 시작 시각)
  Commands → 플레이어 명령 시퀀스 (이 파서에서는 사용 안 함)
  Computed → screp이 계산한 파생 데이터 (승자 팀, 퇴장 기록)

배포 경로 탐색 순서:
  1. bin/screp.exe    ← 배포 표준 위치
  2. lib/screp.exe    ← 구버전 호환
  3. screp.exe        ← 프로젝트 루트
  4. PATH 상의 screp  ← 개발 환경
"""

import hashlib
import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

_BASE = Path(__file__).parent
KST = timezone(timedelta(hours=9))  # Korea Standard Time (UTC+9)

# SC:BW 색상/제어 코드: \x01~\x1e 범위 바이트를 색상 변경에 사용
_SC_COLOR_RE = re.compile(r"[\x00-\x1e]")
_MAP_VERSION_RE = re.compile(r"\s+\d+\.\d+(\.\d+)*\s*$")

# 탐색 우선순위 — 앞에 있는 경로가 먼저 사용됨
_SCREP_CANDIDATES: list[Path] = [
    _BASE / "bin" / "screp.exe",
    _BASE / "lib" / "screp.exe",
    _BASE / "screp.exe",
]


# ---------------------------------------------------------------------------
# screp 실행 파일 탐색
# ---------------------------------------------------------------------------

def _find_screp() -> str | None:
    for path in _SCREP_CANDIDATES:
        if path.exists():
            return str(path)
    return shutil.which("screp")  # PATH 폴백


# ---------------------------------------------------------------------------
# 공개 API
# ---------------------------------------------------------------------------

def parse_replay(rep_path: str) -> dict:
    """
    .rep 파일을 파싱해 게임 정보 딕셔너리를 반환합니다.

    반환값 형식:
    {
        "title":            str,        # 방 제목 (Header.Title) — #tuf 필터 기준
        "map_name":         str,        # 맵 파일명 (Header.Map)
        "players": [
            {
                "id":   int,            # 슬롯 번호 (0~7)
                "name": str,            # 닉네임
                "race": str,            # "T" / "Z" / "P" / "?"
                "team": int,            # 팀 번호 (1v1에서 1 또는 2)
            },
            ...  # 인간 플레이어만 포함 (Computer/Open/Closed 제외)
        ],
        "winner_name":      str | None, # 승자 닉네임 (판정 불가시 None)
        "winner_team":      int | None, # 승자 팀 번호
        "duration_seconds": int,        # 게임 길이 (초)
        "played_at":        str,        # ISO 8601 시작 시각 (UTC 또는 로컬)
        "replay_hash":      str,        # SHA-256 중복 방지 지문
    }

    예외:
        FileNotFoundError  — screp 실행 파일 없음
        RuntimeError       — screp 실행 오류 또는 JSON 파싱 실패
    """
    screp_path = _find_screp()
    if screp_path is None:
        raise FileNotFoundError(
            "screp 실행 파일을 찾을 수 없습니다.\n"
            "bin/screp.exe 위치에 배치하세요.\n"
            "다운로드: https://github.com/icza/screp/releases"
        )

    raw_json = _run_screp(screp_path, rep_path)
    data = _parse_json(raw_json, rep_path)
    return _extract_fields(data)


def extract_match_type(parsed: dict) -> str | None:
    """
    방 제목에서 $태그를 찾아 경기 유형 문자열을 반환합니다.
    예) "$친선" → "친선", "$내전" → "내전"
    $ 태그가 없으면 None (이 리플레이는 수집 대상 아님).
    """
    title = parsed.get("title", "")
    m = re.search(r'\$(\S+)', title)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# screp 실행
# ---------------------------------------------------------------------------

def _run_screp(screp_path: str, rep_path: str) -> str:
    """
    screp -json <rep_path> 를 실행하고 stdout(JSON 문자열)을 반환합니다.
    screp은 성공 시 JSON을 stdout에, 오류 메시지를 stderr에 출력합니다.
    """
    try:
        result = subprocess.run(
            [screp_path, rep_path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"screp 실행 시간 초과 (15초): {rep_path}")
    except OSError as e:
        raise RuntimeError(f"screp 실행 실패 — 파일 권한 또는 경로 오류: {e}")

    if result.returncode != 0:
        err = (result.stderr or result.stdout or "알 수 없는 오류").strip()
        raise RuntimeError(
            f"screp 파싱 오류 (종료 코드 {result.returncode}): {err}"
        )

    return result.stdout


def _parse_json(raw: str, rep_path: str) -> dict:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"screp JSON 파싱 실패 ({rep_path}): {e}\n"
            f"출력 앞 200자: {raw[:200]!r}"
        )


# ---------------------------------------------------------------------------
# 필드 추출
# ---------------------------------------------------------------------------

def _extract_fields(data: dict) -> dict:
    """
    screp JSON 최상위 dict를 받아 필요한 필드만 추출합니다.

    screp Header 구조:
      Title      → 호스트가 설정한 방 이름 (게임 로비 제목)
      Map        → 맵 파일명
      Frames     → 총 게임 프레임 (BW는 24fps 기준)
      StartTime  → RFC3339 형식 시작 시각
      Players[]  → 슬롯별 플레이어 정보
        ID, Name
        Race: { ShortName: "T"/"Z"/"P", Name: "Terran"/"Zerg"/"Protoss" }
        Type: { Name: "Human" / "Computer" / "Open" / "Closed" }
        Team: 팀 번호

    screp Computed 구조:
      WinnerTeam     → 승리 팀 번호 (0이면 미결정)
      LeaveActions[] → 플레이어 퇴장 기록
        Frame          → 퇴장 프레임
        Player.Name    → 퇴장한 플레이어 닉네임
        Reason.Name    → "Quit" / "Disconnect" / "Drop" 등
    """
    header   = data.get("Header",   {})
    computed = data.get("Computed", {})

    title:    str = _strip_sc_codes(header.get("Title",   ""))
    map_name: str = _MAP_VERSION_RE.sub("", _strip_sc_codes(header.get("Map", ""))).strip()

    # BW는 24fps 고정. Frames=0인 손상 리플레이 방어
    frames:   int = max(0, header.get("Frames", 0))
    duration: int = frames // 24

    players = _extract_players(header.get("Players", []))

    start_time: str = header.get("StartTime", "")
    played_at = _to_kst(start_time)  # UTC → KST 변환

    winner_name, winner_team = _detect_winner(computed, players)

    return {
        "title":            title,
        "map_name":         map_name,
        "players":          players,
        "winner_name":      winner_name,
        "winner_team":      winner_team,
        "duration_seconds": duration,
        "played_at":        played_at,
        "replay_hash":      _compute_hash(players, map_name, played_at),
    }


def _extract_players(raw_players: list) -> list[dict]:
    """
    실제 게임 참여 플레이어만 추출합니다.
      - Type == "Human"  : Computer/Open/Closed 슬롯 제외
      - Team in (1, 2)   : 옵저버(team=0) 및 중립 슬롯 제외
    """
    result = []
    for p in raw_players:
        if p.get("Type", {}).get("Name") != "Human":
            continue
        if p.get("Team", 0) not in (1, 2):
            continue

        race_info = p.get("Race", {})
        # ShortName에도 색상 코드가 섞일 수 있으므로 반드시 전처리 후 사용
        short = _strip_sc_codes(race_info.get("ShortName", ""))
        race = short if short in ("T", "Z", "P", "R") else _race_code(race_info.get("Name", ""))

        result.append({
            "id":   p.get("ID",   0),
            "name": _strip_sc_codes(p.get("Name", "")),
            "race": race,
            "team": p.get("Team", 0),
        })
    return result


# ---------------------------------------------------------------------------
# 승자 판정
# ---------------------------------------------------------------------------

def _detect_winner(
    computed: dict,
    players: list[dict],
) -> tuple[str | None, int | None]:
    """
    승자를 판정합니다. 판정 불가 시 (None, None)을 반환합니다.

    판정 우선순위:
      1순위 — Computed.WinnerTeam
             screp이 직접 계산한 승리 팀 번호. 0이면 미결정.
             LeaveActions 기반이지만 이미 검증된 값이므로 가장 신뢰도 높음.

      2순위 — Computed.LeaveActions 프레임 순서
             가장 낮은 프레임(= 가장 먼저 나간 플레이어)을 패자로 처리.
             1v1에서 상대방이 승자가 됨.
             LeaveActions가 비어 있거나 패자를 특정할 수 없으면 넘어감.

      3순위 — 판정 불가 (None, None)
             상태 표시 시 "Unknown"으로 표현.
    """
    # 1순위: WinnerTeam
    winner_team: int = computed.get("WinnerTeam", 0)
    if winner_team > 0:
        winners = [p for p in players if p["team"] == winner_team]
        if winners:
            return winners[0]["name"], winner_team

    # 2순위: LeaveActions
    leave_actions: list[dict] = computed.get("LeaveActions", [])
    if leave_actions and len(players) >= 2:
        # Frame 오름차순 정렬 → index 0 = 가장 먼저 나간 플레이어 = 패자
        sorted_leaves = sorted(leave_actions, key=lambda x: x.get("Frame", 0))
        loser_name: str = sorted_leaves[0].get("Player", {}).get("Name", "")

        if loser_name:
            survivors = [p for p in players if p["name"] != loser_name]
            if survivors:
                winner = survivors[0]
                return winner["name"], winner["team"] or None

    # 3순위: 판정 불가
    return None, None


# ---------------------------------------------------------------------------
# 해시 / 유틸리티
# ---------------------------------------------------------------------------

def _compute_hash(
    players: list[dict],
    map_name: str,
    played_at: str,
) -> str:
    """
    리플레이 고유 지문을 SHA-256으로 생성합니다.

    동일 게임을 두 플레이어가 각자 업로드해도 같은 해시가 나와야 하므로:
      - 플레이어 이름을 사전순 정렬 → player1/player2 순서에 무관
      - played_at을 분(MM) 단위까지만 사용 → 초 단위 파일 저장 시각 오차 흡수
      - duration 제외 → 두 클라이언트의 Frames 값이 네트워크 딜레이로 1~2초 다를 수 있음
    """
    sorted_names = sorted(p["name"] for p in players)
    raw = "|".join([
        *sorted_names,
        map_name,
        played_at[:16],  # "YYYY-MM-DDTHH:MM"
    ])
    return hashlib.sha256(raw.encode()).hexdigest()


def _to_kst(start_time: str) -> str:
    """
    screp의 StartTime(RFC3339)을 KST(UTC+9)로 변환합니다.

    screp은 리플레이 파일 내부의 시각을 그대로 읽는데,
    스타크래프트 리마스터는 UTC 기준으로 기록하는 경우가 많습니다.
    UI 및 DB 저장 시 한국 시각으로 통일하기 위해 KST로 변환합니다.

    처리 사례:
      "2026-05-10T14:30:00Z"        → "2026-05-10T23:30:00+09:00"
      "2026-05-10T14:30:00+00:00"   → "2026-05-10T23:30:00+09:00"
      "2026-05-10T23:30:00+09:00"   → "2026-05-10T23:30:00+09:00" (변환 없음)
      ""  또는 파싱 불가             → 현재 KST 시각
    """
    if not start_time:
        return datetime.now(KST).isoformat()

    # Python 3.10 이하는 fromisoformat()이 'Z'를 처리하지 못하므로 교체
    normalized = start_time.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            # 타임존 정보가 전혀 없으면 UTC로 간주
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(KST).isoformat()
    except ValueError:
        return datetime.now(KST).isoformat()


def _strip_sc_codes(text: str) -> str:
    """
    스타크래프트 색상/제어 코드(\x00~\x1e)를 제거합니다.
    맵 이름, 방 제목, 닉네임, 종족명 모두에 적용합니다.
    """
    return _SC_COLOR_RE.sub("", text).strip()


def _race_code(race_name: str) -> str:
    """Race.Name → 단일 문자 코드 변환 (ShortName이 없는 구버전 screp 대비)."""
    clean = _strip_sc_codes(race_name)
    return {"Terran": "T", "Zerg": "Z", "Protoss": "P", "Random": "R"}.get(
        clean,
        clean[0].upper() if clean else "?",
    )
