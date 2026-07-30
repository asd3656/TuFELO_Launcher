"""
런처 Apps Script 전송 테스트 스크립트
사용법: python test_send.py
"""
import sys
import time
import urllib3
sys.path.insert(0, ".")
import database

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── 테스트 데이터 (여기서 수정) ───────────────────────────────────────────────
TEST_P1        = "tyr"          # P1 DB 닉네임
TEST_P1_RACE   = "Z"         # P1 리플레이 종족 ("T" / "Z" / "P" / "R" / "?")
TEST_P2        = "beombu"          # P2 DB 닉네임
TEST_P2_RACE   = "P"         # P2 리플레이 종족
TEST_P1_WON    = True        # P1 승리 여부
TEST_MAP       = "테스트"
TEST_TYPE      = "시즌3(승패)"   # 시트 탭 이름과 일치해야 함
TEST_PLAYED    = "2026-07-11 20:00:00"
TEST_HASH      = "debug_hash_12345"   # 중복 시 이 값 바꿔서 재시도
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

# 3단계: database.send_match() 호출 (실제 런처와 동일한 경로 — 중복체크/에러처리/사용자 기록 포함)
print("\n[3단계] database.send_match() 호출")
fmt_tier = lambda m: f"{m['tier']}티어" if m and m.get("tier") else ""

try:
    database.send_match(
        tier_p1=fmt_tier(member1),
        name_p1=TEST_P1,
        tier_p2=fmt_tier(member2),
        name_p2=TEST_P2,
        player1_won=TEST_P1_WON,
        map=TEST_MAP,
        match_type=TEST_TYPE,
        played_at=TEST_PLAYED,
        replay_hash=TEST_HASH,
    )
    print("✅ 전송 성공 — 시트 확인하세요")
except database.DuplicateMatchError:
    print("⚠️  중복: 이미 등록된 hash (TEST_HASH를 바꿔서 재시도하세요)")
except Exception as e:
    print(f"❌ 전송 실패: {e}")

# _mark_launcher_used()는 daemon 백그라운드 스레드라, 스크립트가 바로 끝나면
# Supabase 요청이 채 끝나기 전에 함께 종료될 수 있음 → 잠깐 대기
print("\n[4단계] 런처 사용자 기록(Supabase) 반영 대기 중...")
time.sleep(2)
print("완료")
