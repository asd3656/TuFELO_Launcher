import json
import os
import queue
import threading
import time
from pathlib import Path
import webbrowser
from tkinter import filedialog, messagebox

import customtkinter as ctk
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

import database
from database import DuplicateMatchError
import parser as rep_parser

# ──────────────────────────────────────────────────────────────────────────────
# 상수 / 컬러 팔레트 (ELO 보드 톤)
# ──────────────────────────────────────────────────────────────────────────────

APP_VERSION = "1.0.0"
CONFIG_PATH = Path(__file__).parent / "config.json"

C_BG       = "#0b0e14"   # 앱 배경
C_CARD     = "#131929"   # 카드 배경
C_SURFACE  = "#1a2235"   # 입력창/내부
C_BORDER   = "#2a3352"   # 테두리
C_ACCENT   = "#4f8ef7"   # 포인트 블루
C_ACCENT_H = "#6ba3ff"   # 블루 호버
C_RED      = "#c0392b"   # 중지 버튼
C_RED_H    = "#e74c3c"   # 중지 호버
C_TEXT     = "#e2e8f0"   # 기본 텍스트
C_SUBTEXT  = "#8892a4"   # 보조 텍스트
C_SUCCESS  = "#22c55e"   # 정상
C_DANGER   = "#ef4444"   # 점검/오류
C_LOG_BG   = "#0d1117"   # 로그 배경
C_BTN_SEC  = "#1e2740"   # 보조 버튼

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


# ──────────────────────────────────────────────────────────────────────────────
# 유틸
# ──────────────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"replay_folder": "", "selected_member_id": "", "selected_member_name": ""}


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4, ensure_ascii=False)


def _find_sc_replay_folder() -> str:
    home = Path(os.path.expanduser("~"))
    docs = home / "Documents"
    candidates = [
        docs / "StarCraft" / "Maps" / "Replays",
        docs / "StarCraft" / "maps" / "replays",
        docs / "Starcraft" / "Maps" / "Replays",
    ]
    for p in candidates:
        if p.is_dir():
            return str(p)
    return str(docs / "StarCraft" / "Maps" / "Replays")


def _is_version_outdated(local: str, server: str) -> bool:
    try:
        return (
            tuple(int(x) for x in local.split("."))
            < tuple(int(x) for x in server.split("."))
        )
    except (ValueError, AttributeError):
        return local != server


def _send_notification(title: str, msg: str) -> None:
    try:
        from plyer import notification
        notification.notify(title=title, message=msg, app_name="TuFlauncher", timeout=5)
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────────────
# Watchdog
# ──────────────────────────────────────────────────────────────────────────────

class ReplayEventHandler(FileSystemEventHandler):
    def __init__(self, callback):
        super().__init__()
        self._callback = callback

    def on_created(self, event):
        if not event.is_directory and event.src_path.lower().endswith(".rep"):
            time.sleep(1.5)
            self._callback(event.src_path)


# ──────────────────────────────────────────────────────────────────────────────
# 메인 앱
# ──────────────────────────────────────────────────────────────────────────────

class App(ctk.CTk):
    def __init__(self):
        super().__init__()

        self._cfg              = load_config()
        self._members: list[dict] = []
        self._maintenance_mode = False
        self._log_queue        = queue.Queue()
        self._stop_event: threading.Event | None = None
        self._watcher_thread: threading.Thread | None = None
        self._active_me: dict | None = None  # 감시 시작 시점에 캡처된 나
        self._log_font_size = 12

        self.title(f"TuFlauncher v{APP_VERSION}")
        self.geometry("820x700")
        self.minsize(720, 580)
        self.configure(fg_color=C_BG)

        self._build_ui()
        self._load_ui_from_config()

        self.after(100, self._flush_log)
        threading.Thread(target=self._init_app, daemon=True).start()

    # ── UI 구성 ────────────────────────────────────────────────────────────────

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # ── 헤더 ───────────────────────────────────────────────────────────────
        hdr = ctk.CTkFrame(self, fg_color=C_CARD, corner_radius=0, height=68)
        hdr.grid(row=0, column=0, sticky="ew")
        hdr.grid_columnconfigure(1, weight=1)
        hdr.grid_propagate(False)

        ctk.CTkLabel(
            hdr, text="TuF Clan ELO board",
            font=ctk.CTkFont(size=18, weight="bold"), text_color=C_TEXT,
        ).grid(row=0, column=0, padx=24, pady=(14, 1), sticky="w")

        ctk.CTkLabel(
            hdr, text=f"런처 v{APP_VERSION}",
            font=ctk.CTkFont(size=11), text_color=C_SUBTEXT,
        ).grid(row=1, column=0, padx=24, pady=(0, 12), sticky="w")

        badge = ctk.CTkFrame(hdr, fg_color="transparent")
        badge.grid(row=0, column=2, rowspan=2, padx=24, sticky="e")

        self._status_dot = ctk.CTkLabel(
            badge, text="●", font=ctk.CTkFont(size=13), text_color=C_SUBTEXT,
        )
        self._status_dot.pack(side="left", padx=(0, 6))

        self._status_label = ctk.CTkLabel(
            badge, text="연결 중...",
            font=ctk.CTkFont(size=12), text_color=C_SUBTEXT,
        )
        self._status_label.pack(side="left")

        # ── 콘텐츠 ─────────────────────────────────────────────────────────────
        content = ctk.CTkFrame(self, fg_color="transparent")
        content.grid(row=1, column=0, sticky="nsew", padx=20, pady=16)
        content.grid_columnconfigure(0, weight=1)
        content.grid_rowconfigure(3, weight=1)

        # ── 멤버 카드 ──────────────────────────────────────────────────────────
        mc = ctk.CTkFrame(content, fg_color=C_CARD, corner_radius=12)
        mc.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        mc.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            mc, text="내 정보",
            font=ctk.CTkFont(size=12, weight="bold"), text_color=C_SUBTEXT,
        ).grid(row=0, column=0, columnspan=3, padx=16, pady=(12, 6), sticky="w")

        ctk.CTkLabel(
            mc, text="활동명", font=ctk.CTkFont(size=13), text_color=C_TEXT,
        ).grid(row=1, column=0, padx=16, pady=(0, 14), sticky="w")

        self.member_combo = ctk.CTkComboBox(
            mc, values=[], width=260,
            fg_color=C_SURFACE, border_color=C_BORDER,
            button_color=C_ACCENT, button_hover_color=C_ACCENT_H,
            dropdown_fg_color=C_CARD, dropdown_hover_color=C_SURFACE,
            text_color=C_TEXT, font=ctk.CTkFont(size=13),
            corner_radius=8,
        )
        self.member_combo.set("")
        self.member_combo.grid(row=1, column=1, padx=(0, 10), pady=(0, 14), sticky="ew")

        ctk.CTkButton(
            mc, text="새로고침", width=80, height=32,
            fg_color=C_BTN_SEC, hover_color=C_SURFACE,
            text_color=C_TEXT, border_color=C_BORDER, border_width=1,
            corner_radius=8,
            command=lambda: threading.Thread(target=self._refresh_members, daemon=True).start(),
        ).grid(row=1, column=2, padx=(0, 16), pady=(0, 14))

        # ── 폴더 카드 ──────────────────────────────────────────────────────────
        fc = ctk.CTkFrame(content, fg_color=C_CARD, corner_radius=12)
        fc.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        fc.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            fc, text="리플레이 폴더",
            font=ctk.CTkFont(size=12, weight="bold"), text_color=C_SUBTEXT,
        ).grid(row=0, column=0, columnspan=3, padx=16, pady=(12, 6), sticky="w")

        ctk.CTkLabel(
            fc, text="경로", font=ctk.CTkFont(size=13), text_color=C_TEXT,
        ).grid(row=1, column=0, padx=16, pady=(0, 14), sticky="w")

        self.folder_entry = ctk.CTkEntry(
            fc,
            placeholder_text="C:/Users/.../StarCraft/maps/replays",
            fg_color=C_SURFACE, border_color=C_BORDER,
            text_color=C_TEXT, font=ctk.CTkFont(size=12),
            corner_radius=8,
        )
        self.folder_entry.grid(row=1, column=1, padx=(0, 10), pady=(0, 14), sticky="ew")

        ctk.CTkButton(
            fc, text="찾기", width=60, height=32,
            fg_color=C_BTN_SEC, hover_color=C_SURFACE,
            text_color=C_TEXT, border_color=C_BORDER, border_width=1,
            corner_radius=8,
            command=self._browse_folder,
        ).grid(row=1, column=2, padx=(0, 16), pady=(0, 14))

        # ── 컨트롤 버튼 행 ─────────────────────────────────────────────────────
        ctrl = ctk.CTkFrame(content, fg_color="transparent")
        ctrl.grid(row=2, column=0, sticky="ew", pady=(0, 10))

        ctk.CTkButton(
            ctrl, text="설정 저장", width=90, height=36,
            fg_color=C_BTN_SEC, hover_color=C_SURFACE,
            text_color=C_TEXT, border_color=C_BORDER, border_width=1,
            corner_radius=10,
            command=self._save_settings,
        ).pack(side="left", padx=(0, 8))

        self.btn_test = ctk.CTkButton(
            ctrl, text="시트 전송 테스트", width=130, height=36,
            fg_color=C_BTN_SEC, hover_color=C_SURFACE,
            text_color=C_TEXT, border_color=C_BORDER, border_width=1,
            corner_radius=10,
            command=self._test_sheet_send,
        )
        self.btn_test.pack(side="left", padx=(0, 8))

        _links = [
            ("ELO보드",  "https://tufelo.vercel.app/"),
            ("승부예측", "https://tufpl.vercel.app/"),
            ("전적시트", "https://docs.google.com/spreadsheets/d/1kKeA8Y8AmO99qS6v4Xsu_95z6kdKnXL8DXLSLXoCUx8/edit?gid=1549482567#gid=1549482567"),
        ]
        for name, url in _links:
            ctk.CTkButton(
                ctrl, text=name, width=70, height=36,
                fg_color=C_BTN_SEC, hover_color=C_SURFACE,
                text_color=C_ACCENT, border_color=C_BORDER, border_width=1,
                corner_radius=10,
                command=lambda u=url: webbrowser.open(u),
            ).pack(side="left", padx=(0, 8))

        self.btn_launch = ctk.CTkButton(
            ctrl, text="런처 시작", width=110, height=36,
            fg_color=C_ACCENT, hover_color=C_ACCENT_H,
            text_color="#ffffff", corner_radius=10,
            command=self._toggle_watcher,
        )
        self.btn_launch.pack(side="right")

        # ── 로그 카드 ──────────────────────────────────────────────────────────
        lc = ctk.CTkFrame(content, fg_color=C_CARD, corner_radius=12)
        lc.grid(row=3, column=0, sticky="nsew")
        lc.grid_columnconfigure(0, weight=1)
        lc.grid_rowconfigure(1, weight=1)

        log_hdr = ctk.CTkFrame(lc, fg_color="transparent")
        log_hdr.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 6))
        log_hdr.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            log_hdr, text="로그",
            font=ctk.CTkFont(size=12, weight="bold"), text_color=C_SUBTEXT,
        ).grid(row=0, column=0, sticky="w")

        font_ctrl = ctk.CTkFrame(log_hdr, fg_color="transparent")
        font_ctrl.grid(row=0, column=1, sticky="e")

        ctk.CTkButton(
            font_ctrl, text="A-", width=30, height=24,
            fg_color=C_BTN_SEC, hover_color=C_SURFACE,
            text_color=C_SUBTEXT, border_color=C_BORDER, border_width=1,
            corner_radius=6, font=ctk.CTkFont(size=11),
            command=lambda: self._change_log_font(-1),
        ).pack(side="left", padx=(0, 4))

        ctk.CTkButton(
            font_ctrl, text="A+", width=30, height=24,
            fg_color=C_BTN_SEC, hover_color=C_SURFACE,
            text_color=C_SUBTEXT, border_color=C_BORDER, border_width=1,
            corner_radius=6, font=ctk.CTkFont(size=11),
            command=lambda: self._change_log_font(+1),
        ).pack(side="left")

        self.log_box = ctk.CTkTextbox(
            lc,
            fg_color=C_LOG_BG, text_color=C_TEXT,
            font=ctk.CTkFont(family="Consolas", size=self._log_font_size),
            corner_radius=8,
            scrollbar_button_color=C_BTN_SEC,
            scrollbar_button_hover_color=C_BORDER,
            state="disabled",
        )
        self.log_box.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))

    # ── Config ─────────────────────────────────────────────────────────────────

    def _load_ui_from_config(self):
        folder = self._cfg.get("replay_folder") or _find_sc_replay_folder()
        self.folder_entry.insert(0, folder)
        saved = self._cfg.get("selected_member_name", "")
        if saved:
            self.member_combo.set(saved)

    def _save_settings(self):
        me = self._selected_member()
        self._cfg["replay_folder"]        = self.folder_entry.get().strip()
        self._cfg["selected_member_id"]   = me["id"]   if me else ""
        self._cfg["selected_member_name"] = me["name"] if me else self.member_combo.get().strip()
        save_config(self._cfg)
        self._log("설정이 config.json에 저장되었습니다.")

    def _browse_folder(self):
        path = filedialog.askdirectory(title="리플레이 폴더 선택")
        if path:
            self.folder_entry.delete(0, "end")
            self.folder_entry.insert(0, path)

    # ── 초기화 (백그라운드) ────────────────────────────────────────────────────

    def _init_app(self):
        settings = database.fetch_settings()
        self.after(0, lambda: self._apply_settings(settings))
        self._refresh_members()

    def _apply_settings(self, s: dict) -> None:
        is_maintenance = bool(s.get("is_maintenance", False))
        notice         = s.get("notice") or ""
        server_version = s.get("current_version")
        screp_url      = s.get("screp_url")

        self._maintenance_mode = is_maintenance

        if is_maintenance:
            self._set_status("⚠ 서버 점검 중", C_DANGER)
            self._log("[점검] 서버 점검 중. 런처를 시작할 수 없습니다.")
        else:
            self._set_status(notice if notice else "정상 운영 중", C_SUCCESS)
            if notice:
                self._log(f"[공지] {notice}")

        if server_version and _is_version_outdated(APP_VERSION, server_version):
            msg = f"새 버전이 있습니다!\n현재: {APP_VERSION}  →  최신: {server_version}"
            if screp_url:
                msg += f"\n다운로드: {screp_url}"
            messagebox.showwarning("업데이트 필요", msg)
            self._log(f"[업데이트] 최신 버전: {server_version} (현재: {APP_VERSION})")
        elif server_version:
            self._log(f"[버전 확인] 최신 버전 ({APP_VERSION})")

    def _set_status(self, text: str, color: str) -> None:
        self._status_label.configure(text=text, text_color=color)
        self._status_dot.configure(text_color=color)

    # ── 클랜원 ─────────────────────────────────────────────────────────────────

    def _refresh_members(self) -> None:
        members = database.fetch_all_members()

        def _update():
            self._members = members
            names = [m["name"] for m in members]
            self.member_combo.configure(values=names)
            saved = self._cfg.get("selected_member_name", "")
            if saved in names:
                self.member_combo.set(saved)
            elif names:
                self.member_combo.set(names[0])
            self._log(f"클랜원 {len(members)}명 로드 완료: {', '.join(names)}")

        self.after(0, _update)

    def _selected_member(self) -> dict | None:
        name = self.member_combo.get().strip()
        return next((m for m in self._members if m["name"] == name), None)

    def _find_clan_member(self, replay_name: str) -> dict | None:
        replay_lower = replay_name.lower()
        for member in self._members:
            if member["name"].lower() in replay_lower:
                return member
        return None

    # ── 감시 제어 ──────────────────────────────────────────────────────────────

    def _toggle_watcher(self):
        if self._watcher_thread and self._watcher_thread.is_alive():
            self._stop_watcher()
        else:
            self._start_watcher()

    def _start_watcher(self):
        if self._maintenance_mode:
            messagebox.showinfo("점검 중", "서버 점검 중입니다.\n점검 종료 후 다시 시도해 주세요.")
            return

        folder = self.folder_entry.get().strip()
        if not folder or not Path(folder).is_dir():
            self._log("[오류] 유효한 리플레이 폴더를 설정하세요.")
            return

        if not self._members:
            self._log("[오류] 클랜원 목록이 아직 로드되지 않았습니다. 잠시 후 다시 시도하세요.")
            return

        me = self._selected_member()
        if me is None:
            self._log("[오류] 활동명을 선택하세요.")
            return

        self._active_me = me
        self._cfg["selected_member_id"]   = me["id"]
        self._cfg["selected_member_name"] = me["name"]

        self._stop_event     = threading.Event()
        self._watcher_thread = threading.Thread(
            target=self._watcher_run, args=(folder,), daemon=True
        )
        self._watcher_thread.start()

        self.btn_launch.configure(
            text="런처 중지", fg_color=C_RED, hover_color=C_RED_H,
        )
        self._log(f"[시작] 활동명: {me['name']} | 폴더: {folder}")

    def _stop_watcher(self):
        if self._stop_event:
            self._stop_event.set()
        self._watcher_thread = None
        self._stop_event     = None
        self.btn_launch.configure(
            text="런처 시작", fg_color=C_ACCENT, hover_color=C_ACCENT_H,
        )

    def _watcher_run(self, folder: str):
        handler  = ReplayEventHandler(lambda p: self._on_new_replay(p))
        observer = Observer()
        observer.schedule(handler, folder, recursive=False)
        observer.start()
        self._log(f"[감시 중] {folder}")
        try:
            while not self._stop_event.is_set():
                time.sleep(0.5)
        finally:
            observer.stop()
            observer.join()
            self._log("[감시 중지]")

    # ── 시트 전송 테스트 ───────────────────────────────────────────────────────

    def _test_sheet_send(self):
        self.btn_test.configure(state="disabled")
        self._log("[테스트] Apps Script로 TEST 행 전송 중...")

        def _run():
            try:
                database.send_match(
                    tier_w="테스트티어", name_w="테스트승자",
                    tier_l="테스트티어", name_l="테스트패자",
                    map="테스트맵", match_type="TEST",
                    played_at="2000-01-01 00:00:00",
                    replay_hash="test_hash_00000000",
                )
                self._log("[테스트] 시트 전송 성공! 구글 시트에서 TEST 행을 확인하고 삭제해 주세요.")
            except Exception as e:
                self._log(f"[테스트] 전송 실패: {e}")
            finally:
                self.after(0, lambda: self.btn_test.configure(state="normal"))

        threading.Thread(target=_run, daemon=True).start()

    # ── 리플레이 처리 ──────────────────────────────────────────────────────────

    def _on_new_replay(self, path: str):
        if self._maintenance_mode:
            self._log(f"[점검 중 무시] {Path(path).name}")
            return

        self._log(f"[감지] {Path(path).name}")

        # 1. 파싱
        try:
            parsed = rep_parser.parse_replay(path)
        except FileNotFoundError as e:
            self._log(f"[경고] {e}")
            return
        except Exception as e:
            self._log(f"[파싱 오류] {e}")
            return

        # 2. $ 태그 필터
        match_type = rep_parser.extract_match_type(parsed)
        if match_type is None:
            self._log(f"[무시] $ 태그 없음 — {parsed['title'] or parsed['map_name']}")
            return

        players = parsed["players"]

        # 3. 1v1 확인
        if len(players) != 2:
            self._log(f"[무시] 플레이어 {len(players)}명 — 1v1 게임만 처리합니다.")
            return

        # 4. 클랜원 매칭 (둘 다 클랜원이어야 함)
        player_members: list[dict] = []
        for p in players:
            member = self._find_clan_member(p["name"])
            if member is None:
                self._log(f"[무시] 클랜원 간의 내전이 아닙니다. (외부인: '{p['name']}')")
                return
            player_members.append(member)

        # 5. 옵저버 체크 — 내가 실제 플레이어인지 확인
        me = self._active_me
        if me and not any(m["id"] == me["id"] for m in player_members):
            self._log("[무시] 본인은 이 경기의 실제 플레이어가 아닙니다 (옵저버 참관).")
            return

        # 6. 승자/패자 결정
        winner_name = parsed.get("winner_name")
        if not winner_name:
            self._log("[무시] 승자를 판정할 수 없습니다.")
            return

        winner_member = self._find_clan_member(winner_name)
        if winner_member is None:
            self._log(f"[무시] 승자 '{winner_name}'가 클랜원 목록에 없습니다.")
            return

        loser_member = next(m for m in player_members if m["id"] != winner_member["id"])

        # 7. Apps Script로 전송
        played_at_str = parsed["played_at"].replace("T", " ")[:19]
        short_hash    = parsed["replay_hash"][:8]

        try:
            database.send_match(
                tier_w      = winner_member.get("tier") or "",
                name_w      = winner_member["name"],
                tier_l      = loser_member.get("tier") or "",
                name_l      = loser_member["name"],
                map         = parsed["map_name"],
                match_type  = match_type,
                played_at   = played_at_str,
                replay_hash = parsed["replay_hash"],
            )
            self._log(
                f"[수집 완료] 유형: {match_type} | 맵: {parsed['map_name']} | "
                f"결과: {winner_member['name']} vs {loser_member['name']} | hash: {short_hash}"
            )
            _send_notification(
                "TuFlauncher — 전적 기록 완료",
                f"{winner_member['name']} vs {loser_member['name']}  |  유형: {match_type}",
            )
        except DuplicateMatchError:
            self._log(f"[중복] 이미 등록된 경기입니다. (상대방이 먼저 업로드) | hash: {short_hash}")
        except RuntimeError as e:
            self._log(f"[전송 실패] {e} | hash: {short_hash}")
        except Exception as e:
            self._log(f"[전송 오류] {e} | hash: {short_hash}")

    # ── 로그 ───────────────────────────────────────────────────────────────────

    def _change_log_font(self, delta: int) -> None:
        self._log_font_size = max(8, min(24, self._log_font_size + delta))
        self.log_box.configure(font=ctk.CTkFont(family="Consolas", size=self._log_font_size))

    def _log(self, msg: str) -> None:
        self._log_queue.put(msg)

    def _flush_log(self) -> None:
        while not self._log_queue.empty():
            msg = self._log_queue.get_nowait()
            self.log_box.configure(state="normal")
            self.log_box.insert("end", msg + "\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        self.after(100, self._flush_log)

    # ── 종료 ───────────────────────────────────────────────────────────────────

    def on_closing(self):
        self._stop_watcher()
        self.destroy()


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = App()
    app.protocol("WM_DELETE_WINDOW", app.on_closing)
    app.mainloop()
