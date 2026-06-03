"""
런처 Apps Script 전송 테스트 스크립트
사용법: python test_send.py
"""
import sys
import json
import urllib3
sys.path.insert(0, ".")
import database

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── 테스트 데이터 (여기서 수정) ───────────────────────────────────────────────
TEST_P1        = ""          # P1 DB 닉네임
TEST_P1_RACE   = "T"         # P1 리플레이 종족 ("T" / "Z" / "P" / "R" / "?")
TEST_P2        = ""          # P2 DB 닉네임
TEST_P2_RACE   = "Z"         # P2 리플레이 종족
TEST_P1_WON    = True        # P1 승리 여부
TEST_MAP       = "테스트"
TEST_TYPE      = "시즌3(승패)"   # 시트 탭 이름과 일치해야 함
TEST_PLAYED    = "2026-06-02 15:00:00"
TEST_HASH      = "debug_hash_0011"   # 중복 시 이 값 바꿔서 재시도
# ─────────────────────────────────────────────────────────────────────────────

RACE_NAMES = {"T": "테란", "Z": "저그", "P": "프로토스", "R": "랜덤"}


def find_member(members: list[dict], name: str) -> dict | None:
    for m in members:
        if m["name"].lower() == name.lower():
            return m
    return None


def check_race(member: dict, replay_race: str) -> bool:
    """종족 불일치 또는 랜덤이면 True 반환 (전송 차단)."""
    db_race = (member.get("race") or "").strip().upper()
    if not db_race or replay_race in ("?", ""):
        print(f"  ↳ {member['name']}: DB 종족 미설정 또는 리플레이 미상 → 검증 건너뜀")
        return False
    if replay_race == "R":
        print(f"  ❌ {member['name']}: 종족이 랜덤입니다.")
        return True
    if db_race != replay_race:
        db_name = RACE_NAMES.get(db_race, db_race)
        replay_name = RACE_NAMES.get(replay_race, replay_race)
        print(f"  ❌ {member['name']}: 종족 불일치 (DB={db_name} / 리플레이={replay_name})")
        return True
    db_name = RACE_NAMES.get(db_race, db_race)
    print(f"  ✅ {member['name']}: 종족 일치 ({db_name})")
    return False


if not TEST_P1 or not TEST_P2:
    print("❌ TEST_P1, TEST_P2 닉네임을 입력해 주세요.")
    sys.exit(1)

print("=" * 60)

# 1단계: 클랜원 DB 조회
print("[1단계] 클랜원 DB 조회")
members = database.fetch_all_members()
if not members:
    print("  ❌ 클랜원 목록 조회 실패 또는 비어있음")
    sys.exit(1)
print(f"  클랜원 {len(members)}명 로드 완료")

member1 = find_member(members, TEST_P1)
member2 = find_member(members, TEST_P2)
print(f"  P1 '{TEST_P1}': {'발견 (race=' + str(member1.get('race')) + ')' if member1 else '⚠️  DB에 없음'}")
print(f"  P2 '{TEST_P2}': {'발견 (race=' + str(member2.get('race')) + ')' if member2 else '⚠️  DB에 없음'}")

# 2단계: 종족 검증
print("\n[2단계] 종족 검증")
blocked = False
if member1 and check_race(member1, TEST_P1_RACE):
    blocked = True
if member2 and check_race(member2, TEST_P2_RACE):
    blocked = True

if blocked:
    print("→ 전송 차단됨 (종족 불일치)")
    sys.exit(0)
print("  → 검증 통과")

# 3단계: Apps Script 전송
print("\n[3단계] Apps Script 전송")
fmt_tier = lambda m: f"{m['tier']}티어" if m and m.get("tier") else ""
result1, result2 = ("승", "패") if TEST_P1_WON else ("패", "승")
row = [fmt_tier(member1), TEST_P1, result1, result2, TEST_P2, fmt_tier(member2),
       TEST_MAP, TEST_TYPE, "", "", TEST_PLAYED, TEST_HASH]
payload = {"rows": [row]}
print(f"  전송 데이터: {json.dumps(payload, ensure_ascii=False)}")

import requests
try:
    resp = requests.post(database.APPS_SCRIPT_URL, json=payload, timeout=15, allow_redirects=False, verify=False)
    print(f"  [1차] 상태코드: {resp.status_code}")
    location = resp.headers.get("Location")
    if location:
        print(f"  리다이렉트 → {location[:80]}...")
        resp = requests.get(location, timeout=15, verify=False)
        print(f"  [2차] 상태코드: {resp.status_code}")
    else:
        print("  리다이렉트 없음 (직접 응답)")

    print("\n" + "=" * 60)
    try:
        result = resp.json()
        print(f"JSON 파싱 성공: {result}")
        if result.get("status") == "ok":
            print("✅ 전송 성공 — 시트 확인하세요")
        elif result.get("status") == "duplicate":
            print("⚠️  중복: 이미 등록된 hash")
        elif result.get("status") == "error":
            print(f"❌ 오류: {result.get('message')}")
        else:
            print(f"❓ 알 수 없는 상태: {result}")
    except Exception as e:
        print(f"❌ JSON 파싱 실패: {e}\n   원본 응답: {resp.text[:300]}")

except Exception as e:
    print(f"❌ 요청 실패: {e}")
