import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
import webbrowser
import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk
from PIL import Image
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
import pystray

import database
from database import DuplicateMatchError
import parser as rep_parser

# ──────────────────────────────────────────────────────────────────────────────
# 상수 / 컬러 팔레트 (ELO 보드 톤)
# ──────────────────────────────────────────────────────────────────────────────

APP_VERSION = "1.0.4"


def _app_dir() -> Path:
    """PyInstaller --onefile 번들과 개발 환경 모두에서 실행 파일 디렉터리를 반환합니다."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


def _resource(relative: str) -> str:
    """번들 내 리소스 경로(개발 환경에서는 프로젝트 루트 기준)를 반환합니다."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return str(base / relative)


CONFIG_PATH = _app_dir() / "TuFconfig.json"

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


def _send_notification(title: str, msg: str) -> None:
    try:
        from plyer import notification
        notification.notify(title=title, message=msg, app_name="TuFlauncher", timeout=5)
    except Exception:
        pass


def _is_version_outdated(local: str, server: str) -> bool:
    try:
        return (
            tuple(int(x) for x in local.split("."))
            < tuple(int(x) for x in server.split("."))
        )
    except (ValueError, AttributeError):
        return local != server



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
        self._active_additional_name: str = ""  # 감시 시작 시점에 캡처된 추가 닉네임
        self._pending_matches: list[dict] = []
        self._log_font_size     = 12
        self._base_status_text  = "연결 중..."
        self._base_status_color = C_SUBTEXT
        self._version_outdated  = False
        self._notice_text       = ""
        self._tray_icon: pystray.Icon | None = None

        self.title(f"TuFlauncher v{APP_VERSION}")
        self.geometry("820x780")
        self.minsize(720, 640)
        self.configure(fg_color=C_BG)
        try:
            self.iconbitmap(_resource("public/favicon.ico"))
        except Exception:
            pass

        self._build_ui()
        self._load_ui_from_config()
        self._setup_tray()

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
        content.grid_rowconfigure(4, weight=1)

        # ── 멤버 카드 ──────────────────────────────────────────────────────────
        mc = ctk.CTkFrame(content, fg_color=C_CARD, corner_radius=12)
        mc.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        mc.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            mc, text="내 정보",
            font=ctk.CTkFont(size=12, weight="bold"), text_color=C_SUBTEXT,
        ).grid(row=0, column=0, columnspan=3, padx=16, pady=(12, 6), sticky="w")

        ctk.CTkLabel(
            mc, text="클랜 닉네임", font=ctk.CTkFont(size=13), text_color=C_TEXT,
        ).grid(row=1, column=0, padx=16, pady=(0, 14), sticky="w")

        self._ac_names: list[str] = []
        self._ac_popup: tk.Toplevel | None = None
        self._ac_listbox: tk.Listbox | None = None

        self.member_entry = ctk.CTkEntry(
            mc,
            placeholder_text="닉네임 입력 또는 검색(필수)",
            fg_color=C_SURFACE, border_color=C_BORDER,
            text_color=C_TEXT, font=ctk.CTkFont(size=13),
            corner_radius=8,
        )
        self.member_entry.grid(row=1, column=1, padx=(0, 8), pady=(0, 14), sticky="ew")
        self.member_entry.bind("<KeyRelease>", self._ac_on_key)
        self.member_entry.bind("<FocusOut>", lambda e: self.after(150, self._ac_hide))
        self.member_entry.bind("<Down>",   self._ac_focus_list)
        self.member_entry.bind("<Escape>", lambda e: self._ac_hide())
        self.member_entry.bind("<Return>", lambda e: self._confirm_nickname())

        ctk.CTkButton(
            mc, text="확인", width=60, height=32,
            fg_color=C_ACCENT, hover_color=C_ACCENT_H,
            text_color="#ffffff",
            corner_radius=8,
            command=self._confirm_nickname,
        ).grid(row=1, column=2, padx=(0, 8), pady=(0, 14))

        ctk.CTkButton(
            mc, text="새로고침", width=80, height=32,
            fg_color=C_BTN_SEC, hover_color=C_SURFACE,
            text_color=C_TEXT, border_color=C_BORDER, border_width=1,
            corner_radius=8,
            command=lambda: threading.Thread(target=self._refresh_members, daemon=True).start(),
        ).grid(row=1, column=3, padx=(0, 16), pady=(0, 14))

        ctk.CTkLabel(
            mc, text="추가 닉네임", font=ctk.CTkFont(size=13), text_color=C_TEXT,
        ).grid(row=2, column=0, padx=16, pady=(0, 14), sticky="w")

        self.additional_name_entry = ctk.CTkEntry(
            mc,
            placeholder_text="인게임 추가 닉네임 (선택사항)...",
            fg_color=C_SURFACE, border_color=C_BORDER,
            text_color=C_TEXT, font=ctk.CTkFont(size=13),
            corner_radius=8,
        )
        self.additional_name_entry.grid(row=2, column=1, padx=(0, 8), pady=(0, 14), sticky="ew")
        self.additional_name_entry.bind("<Return>", lambda e: self._confirm_additional_name())

        ctk.CTkButton(
            mc, text="확인", width=60, height=32,
            fg_color=C_ACCENT, hover_color=C_ACCENT_H,
            text_color="#ffffff", corner_radius=8,
            command=self._confirm_additional_name,
        ).grid(row=2, column=2, padx=(0, 8), pady=(0, 14))

        ctk.CTkButton(
            mc, text="삭제", width=60, height=32,
            fg_color=C_RED, hover_color=C_RED_H,
            text_color="#ffffff", corner_radius=8,
            command=self._delete_additional_name,
        ).grid(row=2, column=3, padx=(0, 16), pady=(0, 14))

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
            fc, text="열기", width=60, height=32,
            fg_color=C_BTN_SEC, hover_color=C_SURFACE,
            text_color=C_TEXT, border_color=C_BORDER, border_width=1,
            corner_radius=8,
            command=self._open_folder,
        ).grid(row=1, column=2, padx=(0, 6), pady=(0, 14))

        ctk.CTkButton(
            fc, text="찾기", width=60, height=32,
            fg_color=C_BTN_SEC, hover_color=C_SURFACE,
            text_color=C_TEXT, border_color=C_BORDER, border_width=1,
            corner_radius=8,
            command=self._browse_folder,
        ).grid(row=1, column=3, padx=(0, 16), pady=(0, 14))

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

        ctk.CTkButton(
            ctrl, text="공지", width=60, height=36,
            fg_color=C_BTN_SEC, hover_color=C_SURFACE,
            text_color=C_TEXT, border_color=C_BORDER, border_width=1,
            corner_radius=10,
            command=self._show_notice,
        ).pack(side="right", padx=(0, 8))

        # ── 미확인 상대 패널 ───────────────────────────────────────────────────
        pc = ctk.CTkFrame(content, fg_color=C_CARD, corner_radius=12)
        pc.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        pc.grid_columnconfigure(0, weight=1)

        pc_hdr = ctk.CTkFrame(pc, fg_color="transparent")
        pc_hdr.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 4))
        pc_hdr.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            pc_hdr, text="미확인 상대",
            font=ctk.CTkFont(size=12, weight="bold"), text_color=C_SUBTEXT,
        ).grid(row=0, column=0, sticky="w")

        self._select_all_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            pc_hdr, text="전체선택",
            variable=self._select_all_var,
            command=self._on_select_all,
            font=ctk.CTkFont(size=11), text_color=C_SUBTEXT,
            fg_color=C_ACCENT, hover_color=C_ACCENT_H,
            checkmark_color="#ffffff",
        ).grid(row=0, column=1, sticky="e", padx=(0, 4))

        ctk.CTkButton(
            pc_hdr, text="삭제", width=44, height=24,
            fg_color=C_RED, hover_color=C_RED_H,
            text_color="#ffffff", corner_radius=8,
            font=ctk.CTkFont(size=11),
            command=self._delete_pending_matches,
        ).grid(row=0, column=2, sticky="e", padx=(0, 0))

        self._pending_check_vars: list[tk.BooleanVar] = []
        self._pending_check_widgets: list[ctk.CTkCheckBox] = []
        _cb_outer = tk.Frame(pc, bg=C_LOG_BG, height=96)
        _cb_outer.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 4))
        _cb_outer.pack_propagate(False)
        self._pending_cb_frame = ctk.CTkScrollableFrame(
            _cb_outer, fg_color=C_LOG_BG, corner_radius=6,
        )
        self._pending_cb_frame.pack(fill="both", expand=True)
        self._pending_cb_frame.grid_columnconfigure(0, weight=1)

        # ── 인라인 상대선수 입력 (항목 선택 시 표시) ───────────────────────────
        self._pending_input_frame = ctk.CTkFrame(pc, fg_color=C_SURFACE, corner_radius=8)
        self._pending_input_frame.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 10))
        self._pending_input_frame.grid_columnconfigure(1, weight=1)
        self._pending_input_frame.grid_remove()

        # ── 단일 모드 위젯 (row 0, 1) ─────────────────────────────────────────
        self._pending_opp_label = ctk.CTkLabel(
            self._pending_input_frame,
            text="",
            font=ctk.CTkFont(size=11), text_color=C_SUBTEXT,
        )
        self._pending_opp_label.grid(row=0, column=0, padx=(10, 8), pady=(8, 4), sticky="w")

        self._opp_inline_entry = ctk.CTkEntry(
            self._pending_input_frame,
            placeholder_text="클랜원 닉네임...",
            fg_color=C_CARD, border_color=C_BORDER,
            text_color=C_TEXT, font=ctk.CTkFont(size=12),
            corner_radius=6, height=30,
        )
        self._opp_inline_entry.grid(row=0, column=1, padx=(0, 8), pady=(8, 4), sticky="ew")
        self._opp_inline_entry.bind("<KeyRelease>", self._opp_ac_on_key)
        self._opp_inline_entry.bind("<Return>",  lambda e: self._confirm_pending_inline())
        self._opp_inline_entry.bind("<Escape>",  lambda e: self._cancel_pending_inline())

        self._btn_confirm_single = ctk.CTkButton(
            self._pending_input_frame, text="확인", width=60, height=30,
            fg_color=C_ACCENT, hover_color=C_ACCENT_H,
            text_color="#ffffff", corner_radius=6,
            command=self._confirm_pending_inline,
        )
        self._btn_confirm_single.grid(row=0, column=2, padx=(0, 4), pady=(8, 4))

        self._btn_cancel_single = ctk.CTkButton(
            self._pending_input_frame, text="취소", width=50, height=30,
            fg_color=C_BTN_SEC, hover_color=C_SURFACE,
            text_color=C_TEXT, border_color=C_BORDER, border_width=1,
            corner_radius=6, command=self._cancel_pending_inline,
        )
        self._btn_cancel_single.grid(row=0, column=3, padx=(0, 10), pady=(8, 4))

        self._opp_ac_wrap = tk.Frame(self._pending_input_frame, bg=C_CARD, highlightthickness=0)
        self._opp_ac_wrap.grid(row=1, column=0, columnspan=4, sticky="ew", padx=10, pady=(0, 6))

        self._opp_ac_listbox = tk.Listbox(
            self._opp_ac_wrap,
            bg=C_CARD, fg=C_TEXT,
            selectbackground=C_ACCENT, selectforeground="#ffffff",
            font=("맑은 고딕", 11),
            borderwidth=0, highlightthickness=0,
            relief="flat", activestyle="none",
            height=0,
        )
        self._opp_ac_listbox.pack(fill="x", padx=1)
        self._opp_ac_listbox.bind("<<ListboxSelect>>", self._opp_ac_select)

        # ── 더블 모드 위젯 (row 2, 3, 4) — 옵저버: 두 플레이어 모두 미확인 ──────
        self._pending_p1_label = ctk.CTkLabel(
            self._pending_input_frame, text="",
            font=ctk.CTkFont(size=11), text_color=C_SUBTEXT,
        )
        self._pending_p1_label.grid(row=2, column=0, padx=(10, 8), pady=(8, 2), sticky="w")
        self._pending_p1_label.grid_remove()

        self._p1_inline_entry = ctk.CTkEntry(
            self._pending_input_frame,
            placeholder_text="P1 클랜원 닉네임...",
            fg_color=C_CARD, border_color=C_BORDER,
            text_color=C_TEXT, font=ctk.CTkFont(size=12),
            corner_radius=6, height=30,
        )
        self._p1_inline_entry.grid(row=2, column=1, columnspan=3, padx=(0, 10), pady=(8, 2), sticky="ew")
        self._p1_inline_entry.bind("<KeyRelease>", self._p12_ac_on_key)
        self._p1_inline_entry.bind("<Return>",   lambda e: self._confirm_pending_inline())
        self._p1_inline_entry.bind("<Escape>",   lambda e: self._cancel_pending_inline())
        self._p1_inline_entry.bind("<FocusIn>",  lambda e: self._set_p12_active(self._p1_inline_entry))
        self._p1_inline_entry.grid_remove()

        self._pending_p2_label = ctk.CTkLabel(
            self._pending_input_frame, text="",
            font=ctk.CTkFont(size=11), text_color=C_SUBTEXT,
        )
        self._pending_p2_label.grid(row=3, column=0, padx=(10, 8), pady=(2, 8), sticky="w")
        self._pending_p2_label.grid_remove()

        self._p2_inline_entry = ctk.CTkEntry(
            self._pending_input_frame,
            placeholder_text="P2 클랜원 닉네임...",
            fg_color=C_CARD, border_color=C_BORDER,
            text_color=C_TEXT, font=ctk.CTkFont(size=12),
            corner_radius=6, height=30,
        )
        self._p2_inline_entry.grid(row=3, column=1, padx=(0, 8), pady=(2, 8), sticky="ew")
        self._p2_inline_entry.bind("<KeyRelease>", self._p12_ac_on_key)
        self._p2_inline_entry.bind("<Return>",   lambda e: self._confirm_pending_inline())
        self._p2_inline_entry.bind("<Escape>",   lambda e: self._cancel_pending_inline())
        self._p2_inline_entry.bind("<FocusIn>",  lambda e: self._set_p12_active(self._p2_inline_entry))
        self._p2_inline_entry.grid_remove()

        self._btn_confirm_double = ctk.CTkButton(
            self._pending_input_frame, text="확인", width=60, height=30,
            fg_color=C_ACCENT, hover_color=C_ACCENT_H,
            text_color="#ffffff", corner_radius=6,
            command=self._confirm_pending_inline,
        )
        self._btn_confirm_double.grid(row=3, column=2, padx=(0, 4), pady=(2, 8))
        self._btn_confirm_double.grid_remove()

        self._btn_cancel_double = ctk.CTkButton(
            self._pending_input_frame, text="취소", width=50, height=30,
            fg_color=C_BTN_SEC, hover_color=C_SURFACE,
            text_color=C_TEXT, border_color=C_BORDER, border_width=1,
            corner_radius=6, command=self._cancel_pending_inline,
        )
        self._btn_cancel_double.grid(row=3, column=3, padx=(0, 10), pady=(2, 8))
        self._btn_cancel_double.grid_remove()

        self._p12_ac_wrap = tk.Frame(self._pending_input_frame, bg=C_CARD, highlightthickness=0)
        self._p12_ac_wrap.grid(row=4, column=0, columnspan=4, sticky="ew", padx=10, pady=(0, 6))
        self._p12_ac_wrap.grid_remove()

        self._p12_ac_listbox = tk.Listbox(
            self._p12_ac_wrap,
            bg=C_CARD, fg=C_TEXT,
            selectbackground=C_ACCENT, selectforeground="#ffffff",
            font=("맑은 고딕", 11),
            borderwidth=0, highlightthickness=0,
            relief="flat", activestyle="none",
            height=0,
        )
        self._p12_ac_listbox.pack(fill="x", padx=1)
        self._p12_ac_listbox.bind("<<ListboxSelect>>", self._p12_ac_select)

        self._p12_active_entry: ctk.CTkEntry | None = None

        # ── 로그 카드 ──────────────────────────────────────────────────────────
        lc = ctk.CTkFrame(content, fg_color=C_CARD, corner_radius=12)
        lc.grid(row=4, column=0, sticky="nsew")
        lc.grid_columnconfigure(0, weight=1)
        lc.grid_rowconfigure(1, weight=1)

        log_hdr = ctk.CTkFrame(lc, fg_color="transparent")
        log_hdr.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 4))
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
        ).pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            font_ctrl, text="지우기", width=52, height=24,
            fg_color=C_BTN_SEC, hover_color=C_SURFACE,
            text_color=C_SUBTEXT, border_color=C_BORDER, border_width=1,
            corner_radius=6, font=ctk.CTkFont(size=11),
            command=self._clear_log,
        ).pack(side="left")

        self._last_match_label = ctk.CTkLabel(
            log_hdr, text="",
            font=ctk.CTkFont(size=11), text_color=C_SUBTEXT,
        )
        self._last_match_label.grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 4))

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
            self._ac_set(saved)
        additional = self._cfg.get("additional_ingame_name", "")
        if additional:
            self.additional_name_entry.insert(0, additional)
        if geo := self._cfg.get("window_geometry"):
            self.geometry(geo)
        if font_size := self._cfg.get("log_font_size"):
            self._log_font_size = int(font_size)
            self.log_box.configure(font=ctk.CTkFont(family="Consolas", size=self._log_font_size))

    def _save_settings(self):
        me = self._selected_member()
        self._cfg["replay_folder"]          = self.folder_entry.get().strip()
        self._cfg["selected_member_id"]     = me["id"]   if me else ""
        self._cfg["selected_member_name"]   = me["name"] if me else self.member_entry.get().strip()
        self._cfg["additional_ingame_name"] = self.additional_name_entry.get().strip()
        self._cfg["window_geometry"]        = self.geometry()
        self._cfg["log_font_size"]          = self._log_font_size
        save_config(self._cfg)
        self._log("설정이 TuFconfig.json에 저장되었습니다.")

    def _open_folder(self):
        path = self.folder_entry.get().strip()
        if not path or not Path(path).is_dir():
            self._log("[열기] 유효한 폴더 경로가 없습니다.")
            return
        os.startfile(path)

    def _browse_folder(self):
        path = filedialog.askdirectory(title="리플레이 폴더 선택")
        if path:
            self.folder_entry.delete(0, "end")
            self.folder_entry.insert(0, path)

    # ── 초기화 (백그라운드) ────────────────────────────────────────────────────

    def _init_app(self):
        settings = database.fetch_settings()
        self.after(0, lambda: self._apply_settings(settings))
        has_saved_member = bool(self._cfg.get("selected_member_name", "").strip())
        self._refresh_members(auto_start=has_saved_member)

    def _apply_settings(self, s: dict) -> None:
        is_maintenance = bool(s.get("is_maintenance", False))
        notice         = s.get("notice") or ""
        server_version = s.get("current_version")
        screp_url      = s.get("screp_url")

        self._maintenance_mode = is_maintenance
        self._notice_text = notice

        if is_maintenance:
            self._set_status("⚠ 서버 점검 중", C_DANGER)
            self._log("[점검] 서버 점검 중. 런처를 시작할 수 없습니다.")
        else:
            first_line = notice.splitlines()[0] if notice else "정상 운영 중"
            self._set_status(first_line, C_SUCCESS)
            if notice:
                self._log(f"[공지] {notice}")

        if server_version and _is_version_outdated(APP_VERSION, server_version):
            self._version_outdated = True
            self._set_status(f"업데이트 필요 (v{server_version})", C_DANGER)
            self._log(f"[업데이트 필요] 현재: v{APP_VERSION} → 최신: v{server_version}")
            if screp_url:
                self._log(f"[업데이트] 다운로드: {screp_url}")
            self._show_update_dialog(server_version, screp_url or "")
        elif server_version:
            self._log(f"[버전 확인] 최신 버전 (v{APP_VERSION})")

    def _show_update_dialog(self, server_version: str, download_url: str) -> None:
        import requests as _requests

        dlg = ctk.CTkToplevel(self)
        dlg.title("업데이트 알림")
        dlg.resizable(False, False)
        dlg.grab_set()

        ctk.CTkLabel(
            dlg,
            text=(
                f"새 버전 (v{server_version}) 이 출시되었습니다.\n"
                "업데이트 없이는 런처를 이용할 수 없습니다.\n\n"
                "지금 바로 다운로드 하시겠습니까?"
            ),
            wraplength=360,
            justify="center",
        ).pack(padx=24, pady=(20, 10))

        progress_label = ctk.CTkLabel(dlg, text="", font=ctk.CTkFont(size=11), text_color=C_SUBTEXT)
        progress_label.pack(padx=24)

        progress_bar = ctk.CTkProgressBar(dlg, width=360)
        progress_bar.set(0)

        btn_frame = ctk.CTkFrame(dlg, fg_color="transparent")
        btn_frame.pack(pady=(10, 20))

        def _start_download():
            btn_download.configure(state="disabled")
            btn_cancel.configure(state="disabled")
            progress_bar.pack(padx=24, pady=(4, 8), before=btn_frame)
            progress_label.configure(text="다운로드 준비 중...")
            dlg.update_idletasks()

            def _do():
                try:
                    if getattr(sys, "frozen", False):
                        save_dir = Path(sys.executable).parent
                        tmp_path = save_dir / f"_TuFlauncher_update.exe"
                    else:
                        tmp_path = Path(__file__).parent / f"TuFlauncher_v{server_version}.exe"

                    resp = _requests.get(download_url, stream=True, timeout=30)
                    resp.raise_for_status()
                    total = int(resp.headers.get("content-length", 0))
                    downloaded = 0
                    with open(tmp_path, "wb") as f:
                        for chunk in resp.iter_content(chunk_size=65536):
                            if chunk:
                                f.write(chunk)
                                downloaded += len(chunk)
                                if total:
                                    pct = downloaded / total
                                    dlg.after(0, lambda p=pct: progress_bar.set(p))
                                    dlg.after(0, lambda p=pct: progress_label.configure(
                                        text=f"다운로드 중... {int(p * 100)}%"
                                    ))
                    dlg.after(0, lambda: _on_done(tmp_path))
                except Exception as e:
                    dlg.after(0, lambda err=str(e): _on_error(err))

            threading.Thread(target=_do, daemon=True).start()

        def _on_done(tmp_path: Path):
            progress_bar.set(1)
            progress_label.configure(text="다운로드 완료!", text_color=C_SUCCESS)

            for w in btn_frame.winfo_children():
                w.destroy()

            if getattr(sys, "frozen", False):
                current_exe = Path(sys.executable)
                bat_path = current_exe.parent / "_tuf_update.bat"
                bat_path.write_text(
                    "@echo off\r\n"
                    "timeout /t 2 /nobreak >nul\r\n"
                    f"move /y \"{tmp_path}\" \"{current_exe}\"\r\n"
                    f"start \"\" \"{current_exe}\"\r\n"
                    "del \"%~f0\"\r\n",
                    encoding="ascii",
                )

                def _restart():
                    subprocess.Popen(
                        ["cmd", "/c", str(bat_path)],
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                    dlg.destroy()
                    self._quit_app()

                ctk.CTkLabel(
                    btn_frame, text="런처를 재시작하면 업데이트가 적용됩니다.",
                    font=ctk.CTkFont(size=11), text_color=C_SUBTEXT,
                ).pack(pady=(0, 8))
                ctk.CTkButton(
                    btn_frame, text="지금 재시작", command=_restart, width=120,
                    fg_color=C_ACCENT, hover_color=C_ACCENT_H, text_color="#ffffff",
                ).pack(side="left", padx=8)
                ctk.CTkButton(
                    btn_frame, text="나중에", command=dlg.destroy, width=80,
                    fg_color=C_BTN_SEC, hover_color=C_SURFACE,
                    text_color=C_TEXT, border_color=C_BORDER, border_width=1,
                ).pack(side="left", padx=8)
            else:
                ctk.CTkLabel(
                    btn_frame, text=f"저장됨: {tmp_path.name}",
                    font=ctk.CTkFont(size=11), text_color=C_SUBTEXT,
                ).pack(pady=(0, 8))
                ctk.CTkButton(btn_frame, text="확인", command=dlg.destroy, width=100).pack()

        def _on_error(msg: str):
            progress_label.configure(text=f"다운로드 실패: {msg}", text_color=C_DANGER)
            btn_download.configure(state="normal")
            btn_cancel.configure(state="normal")

        if download_url:
            btn_download = ctk.CTkButton(
                btn_frame, text="업데이트 다운로드", command=_start_download, width=140,
                fg_color=C_ACCENT, hover_color=C_ACCENT_H, text_color="#ffffff",
            )
            btn_download.pack(side="left", padx=8)

        btn_cancel = ctk.CTkButton(
            btn_frame, text="취소", command=dlg.destroy, width=80,
            fg_color=C_BTN_SEC, hover_color=C_SURFACE,
            text_color=C_TEXT, border_color=C_BORDER, border_width=1,
        )
        btn_cancel.pack(side="left", padx=8)

        dlg.update_idletasks()
        x = self.winfo_x() + (self.winfo_width()  - dlg.winfo_width())  // 2
        y = self.winfo_y() + (self.winfo_height() - dlg.winfo_height()) // 2
        dlg.geometry(f"+{x}+{y}")
        self.wait_window(dlg)

    def _set_status(self, text: str, color: str) -> None:
        self._base_status_text  = text
        self._base_status_color = color
        self._status_label.configure(text=text, text_color=color)
        self._status_dot.configure(text_color=color)

    # ── 클랜원 ─────────────────────────────────────────────────────────────────

    def _refresh_members(self, auto_start: bool = False) -> None:
        members = database.fetch_all_members()

        def _update():
            self._members = members
            names = [m["name"] for m in members]
            self._ac_names = names
            saved = self._cfg.get("selected_member_name", "")
            if saved in names:
                self._ac_set(saved)
            self._log(f"클랜원 {len(members)}명 로드 완료.")
            if auto_start:
                self._start_watcher(silent=True)

        self.after(0, _update)

    def _selected_member(self) -> dict | None:
        name = self.member_entry.get().strip().lower()
        return next((m for m in self._members if m["name"].lower() == name), None)

    def _confirm_nickname(self) -> None:
        name = self.member_entry.get().strip()
        if not name:
            self._log("[닉네임] 입력 실패 — 닉네임을 입력해 주세요.")
            return
        matched = next((m for m in self._members if m["name"].lower() == name.lower()), None)
        if matched:
            self._ac_set(matched["name"])
            self._log(f"[닉네임] 확인 완료 — '{matched['name']}' 으로 설정되었습니다.")
        else:
            self._log(f"[닉네임] 입력 실패 — '{name}' 닉네임이 클랜원 목록에 없습니다.")

    def _confirm_additional_name(self) -> None:
        name = self.additional_name_entry.get().strip()
        self._active_additional_name = name
        if name:
            self._log(f"[추가 닉네임] '{name}' 이(가) 추가 닉네임으로 설정되었습니다.")
        else:
            self._log("[추가 닉네임] 추가 닉네임이 지워졌습니다.")

    def _delete_additional_name(self) -> None:
        self.additional_name_entry.delete(0, "end")
        self._active_additional_name = ""
        self._log("[추가 닉네임] 추가 닉네임이 삭제되었습니다.")

    @staticmethod
    def _fmt_tier(raw) -> str:
        t = str(raw).strip() if raw is not None else ""
        return f"{t}티어" if t else ""

    @staticmethod
    def _name_matches(db_name: str, ingame_name: str) -> bool:
        """DB 닉네임이 인게임 닉네임과 일치하는지 확인합니다.
        배틀넷 형식(ClanTag`Name, [TAG]Name)의 클랜태그를 제거한 뒤 비교합니다."""
        dl = db_name.lower()
        il = ingame_name.lower()
        if dl == il:
            return True
        # "ClanTag`PlayerName" → 백틱 뒤 이름만 비교
        if '`' in il and il.split('`', 1)[1] == dl:
            return True
        # "[TAG]PlayerName" → ']' 뒤 이름만 비교
        if il.startswith('[') and ']' in il:
            if il[il.index(']') + 1:] == dl:
                return True
        # "ClanTag_PlayerName" → 언더바 뒤 이름만 비교
        if '_' in il and il.split('_', 1)[1] == dl:
            return True
        return False

    def _find_clan_member(self, replay_name: str) -> dict | None:
        for member in self._members:
            if self._name_matches(member["name"], replay_name):
                return member
        return None

    # ── 감시 제어 ──────────────────────────────────────────────────────────────

    def _toggle_watcher(self):
        if self._watcher_thread and self._watcher_thread.is_alive():
            self._stop_watcher()
        else:
            self._start_watcher()

    def _start_watcher(self, silent: bool = False):
        if self._version_outdated:
            if not silent:
                messagebox.showerror(
                    "업데이트 필요",
                    "최신 버전으로 업데이트 후 사용 가능합니다.\n깃허브에서 최신 버전을 다운로드 해주세요.",
                )
            return

        if self._maintenance_mode:
            if not silent:
                messagebox.showinfo("점검 중", "서버 점검 중입니다.\n점검 종료 후 다시 시도해 주세요.")
            return

        folder = self.folder_entry.get().strip()
        if not folder or not Path(folder).is_dir():
            if not silent:
                self._log("[오류] 유효한 리플레이 폴더를 설정하세요.")
            return

        if not self._members:
            if not silent:
                self._log("[오류] 클랜원 목록이 아직 로드되지 않았습니다. 잠시 후 다시 시도하세요.")
            return

        me = self._selected_member()
        if me is None:
            if not silent:
                self._log("[오류] 닉네임을 선택하세요.")
            return

        self._active_me = me
        self._active_additional_name = self.additional_name_entry.get().strip()
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
        self._log(f"[시작] 닉네임: {me['name']} | 폴더: {folder}")

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

        try:
            self._process_replay(parsed)
        except Exception as e:
            self._log(f"[처리 오류] {e}")

    def _process_replay(self, parsed: dict):
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

        # 4. 승자 결정
        winner_name = parsed.get("winner_name")
        if not winner_name:
            self._log("[무시] 승자를 판정할 수 없습니다.")
            return

        # 5. 옵저버 체크 — 인게임 닉네임에 내 닉네임(또는 추가 닉네임)이 일치해야 실제 플레이어
        me = self._active_me
        me_name = me["name"] if me else ""
        additional = self._active_additional_name
        user_player = next(
            (p for p in players
             if self._name_matches(me_name, p["name"])
             or (additional and additional.lower() == p["name"].lower())),
            None,
        )
        if user_player is None:
            self._log("[옵저버] 본인은 플레이어가 아닙니다 — 옵저버 모드로 처리합니다.")
            self._process_replay_as_observer(parsed, players, match_type, winner_name)
            return

        opp_player   = next(p for p in players if p is not user_player)
        opp_raw_name = opp_player["name"]
        player1_won  = winner_name.lower() == user_player["name"].lower()

        self._log(
            f"[분석] match_type={match_type} | 내닉={me_name}({user_player['name']}) "
            f"| 상대={opp_raw_name} | 승자={winner_name}"
        )

        # 6. 상대방 클랜원 DB 확인 (인게임명 → DB명 부분일치)
        opp_member   = self._find_clan_member(opp_raw_name)
        me_tier      = self._fmt_tier(me.get("tier") if me else None)
        played_at_str = parsed["played_at"].replace("T", " ")[:19]
        short_hash    = parsed["replay_hash"][:8]
        result_str    = "승리" if player1_won else "패배"

        if opp_member is None:
            # 미확인 상대 → 펜딩 패널에 추가
            match_data = {
                "me_name":      me_name,
                "me_tier":      me_tier,
                "opp_raw_name": opp_raw_name,
                "player1_won":  player1_won,
                "map":          parsed["map_name"],
                "match_type":   match_type,
                "played_at":    played_at_str,
                "replay_hash":  parsed["replay_hash"],
            }
            self.after(0, lambda d=match_data: self._add_pending_match(d))
            return

        # 7. 클랜원 확인됨 → Apps Script로 전송
        opp_name = opp_member["name"]
        try:
            database.send_match(
                tier_p1     = me_tier,
                name_p1     = me_name,
                tier_p2     = self._fmt_tier(opp_member.get("tier")),
                name_p2     = opp_name,
                player1_won = player1_won,
                map         = parsed["map_name"],
                match_type  = match_type,
                played_at   = played_at_str,
                replay_hash = parsed["replay_hash"],
            )
            self._log(
                f"[수집 완료] 유형: {match_type} | 맵: {parsed['map_name']} | "
                f"{me_name} {result_str} vs {opp_name} | hash: {short_hash}"
            )
            winner_log = me_name if player1_won else opp_name
            loser_log  = opp_name if player1_won else me_name
            self.after(0, lambda wl=winner_log, ll=loser_log, mt=match_type: self._flash_success(wl, ll, mt))
        except DuplicateMatchError:
            self._log(f"[중복] 이미 등록된 경기입니다. (상대방이 먼저 업로드) | hash: {short_hash}")
        except RuntimeError as e:
            self._log(f"[전송 실패] {e} | hash: {short_hash}")
        except Exception as e:
            self._log(f"[전송 오류] {e} | hash: {short_hash}")

    def _process_replay_as_observer(
        self, parsed: dict, players: list, match_type: str, winner_name: str
    ) -> None:
        """옵저버 모드: 런처 사용자가 플레이어가 아닐 때 두 플레이어 기준으로 전적 처리."""
        p1, p2 = players[0], players[1]
        member1 = self._find_clan_member(p1["name"])
        member2 = self._find_clan_member(p2["name"])
        played_at_str = parsed["played_at"].replace("T", " ")[:19]
        short_hash = parsed["replay_hash"][:8]

        self._log(
            f"[옵저버 분석] match_type={match_type} | "
            f"P1={p1['name']}({'매칭' if member1 else '미확인'}) "
            f"P2={p2['name']}({'매칭' if member2 else '미확인'}) | 승자={winner_name}"
        )

        if member1 and member2:
            player1_won = winner_name.lower() == p1["name"].lower()
            try:
                database.send_match(
                    tier_p1=self._fmt_tier(member1.get("tier")),
                    name_p1=member1["name"],
                    tier_p2=self._fmt_tier(member2.get("tier")),
                    name_p2=member2["name"],
                    player1_won=player1_won,
                    map=parsed["map_name"],
                    match_type=match_type,
                    played_at=played_at_str,
                    replay_hash=parsed["replay_hash"],
                )
                winner_log = member1["name"] if player1_won else member2["name"]
                loser_log  = member2["name"] if player1_won else member1["name"]
                self._log(
                    f"[수집 완료] 유형: {match_type} | 맵: {parsed['map_name']} | "
                    f"{winner_log} 승 vs {loser_log} | hash: {short_hash}"
                )
                self.after(0, lambda wl=winner_log, ll=loser_log, mt=match_type: self._flash_success(wl, ll, mt))
            except DuplicateMatchError:
                self._log(f"[중복] 이미 등록된 경기입니다. | hash: {short_hash}")
            except RuntimeError as e:
                self._log(f"[전송 실패] {e} | hash: {short_hash}")
            except Exception as e:
                self._log(f"[전송 오류] {e} | hash: {short_hash}")

        elif member1 and not member2:
            # player2 미확인 → pending (member1이 "me" 역할)
            player1_won = winner_name.lower() == p1["name"].lower()
            match_data = {
                "me_name":      member1["name"],
                "me_tier":      self._fmt_tier(member1.get("tier")),
                "opp_raw_name": p2["name"],
                "player1_won":  player1_won,
                "map":          parsed["map_name"],
                "match_type":   match_type,
                "played_at":    played_at_str,
                "replay_hash":  parsed["replay_hash"],
            }
            self.after(0, lambda d=match_data: self._add_pending_match(d))

        elif member2 and not member1:
            # player1 미확인 → pending (member2가 "me" 역할, player1_won 관점 전환)
            player1_won = winner_name.lower() == p2["name"].lower()
            match_data = {
                "me_name":      member2["name"],
                "me_tier":      self._fmt_tier(member2.get("tier")),
                "opp_raw_name": p1["name"],
                "player1_won":  player1_won,
                "map":          parsed["map_name"],
                "match_type":   match_type,
                "played_at":    played_at_str,
                "replay_hash":  parsed["replay_hash"],
            }
            self.after(0, lambda d=match_data: self._add_pending_match(d))

        else:
            # 둘 다 미확인 → 수동 입력 패널로 전달
            player1_won = winner_name.lower() == p1["name"].lower()
            match_data = {
                "me_name":        "",
                "me_tier":        "",
                "opp_raw_name":   "",
                "player1_won":    player1_won,
                "map":            parsed["map_name"],
                "match_type":     match_type,
                "played_at":      played_at_str,
                "replay_hash":    parsed["replay_hash"],
                "both_unmatched": True,
                "p1_raw_name":    p1["name"],
                "p2_raw_name":    p2["name"],
            }
            self.after(0, lambda d=match_data: self._add_pending_match(d))

    # ── 미확인 상대 패널 ────────────────────────────────────────────────────────

    def _add_pending_match(self, match_data: dict) -> None:
        try:
            self._pending_matches.append(match_data)
            if match_data.get("both_unmatched"):
                display = (
                    f"[{match_data['match_type']}] {match_data['map']}  "
                    f"{match_data['p1_raw_name']} vs {match_data['p2_raw_name']} (미확인쌍)"
                )
            else:
                result_str = "승리" if match_data["player1_won"] else "패배"
                display = (
                    f"[{match_data['match_type']}] {match_data['map']}  "
                    f"{match_data['me_name']} {result_str} vs {match_data['opp_raw_name']}"
                )
            var = tk.BooleanVar(value=False)
            self._pending_check_vars.append(var)
            cb = ctk.CTkCheckBox(
                self._pending_cb_frame,
                text=display,
                variable=var,
                command=self._on_check_changed,
                font=ctk.CTkFont(family="Consolas", size=11),
                text_color=C_TEXT,
                fg_color=C_ACCENT, hover_color=C_ACCENT_H,
                checkmark_color="#ffffff",
            )
            cb.pack(fill="x", padx=4, pady=2, anchor="w")
            self._pending_check_widgets.append(cb)
            if match_data.get("both_unmatched"):
                self._log(
                    f"[미확인 쌍] P1={match_data['p1_raw_name']}, P2={match_data['p2_raw_name']} "
                    "— 두 선수 모두 클랜원 목록에 없습니다. 패널에서 직접 입력하세요."
                )
            else:
                self._log(
                    f"[미확인 상대] '{match_data['opp_raw_name']}' 이(가) 클랜원 목록에 없습니다 "
                    f"— 미확인 상대 패널에서 선수 입력 후 전송하세요."
                )
        except Exception as e:
            self._log(f"[미확인 상대 오류] {e}")

    def _on_check_changed(self) -> None:
        checked = [i for i, v in enumerate(self._pending_check_vars) if v.get()]
        self._select_all_var.set(
            bool(checked) and len(checked) == len(self._pending_check_vars)
        )
        if not checked:
            self._pending_input_frame.grid_remove()
            return

        # 단일 both_unmatched 선택 → 더블 모드
        if len(checked) == 1 and self._pending_matches[checked[0]].get("both_unmatched"):
            m = self._pending_matches[checked[0]]
            self._show_double_input(m["p1_raw_name"], m["p2_raw_name"])
        else:
            single_checked = [i for i in checked if not self._pending_matches[i].get("both_unmatched")]
            if not single_checked:
                label = f"{len(checked)}경기 선택 (미확인쌍 — 하나씩 선택)"
            else:
                raws = list(dict.fromkeys(self._pending_matches[i]["opp_raw_name"] for i in single_checked))
                if len(raws) == 1:
                    label = f"인게임: {raws[0]}"
                    if len(single_checked) > 1:
                        label += f"  ({len(single_checked)}경기 선택)"
                else:
                    label = f"{len(single_checked)}경기 선택 (상대 다수)"
            self._show_single_input(label)

        self._pending_input_frame.grid()

    def _show_single_input(self, label: str) -> None:
        self._pending_opp_label.configure(text=label)
        self._pending_opp_label.grid()
        self._opp_inline_entry.grid()
        self._btn_confirm_single.grid()
        self._btn_cancel_single.grid()
        self._opp_ac_wrap.grid()
        self._pending_p1_label.grid_remove()
        self._p1_inline_entry.grid_remove()
        self._pending_p2_label.grid_remove()
        self._p2_inline_entry.grid_remove()
        self._btn_confirm_double.grid_remove()
        self._btn_cancel_double.grid_remove()
        self._p12_ac_wrap.grid_remove()
        self._opp_inline_entry.focus_set()

    def _show_double_input(self, p1_raw: str, p2_raw: str) -> None:
        self._pending_opp_label.grid_remove()
        self._opp_inline_entry.grid_remove()
        self._btn_confirm_single.grid_remove()
        self._btn_cancel_single.grid_remove()
        self._opp_ac_wrap.grid_remove()
        self._pending_p1_label.configure(text=f"P1 인게임: {p1_raw}")
        self._pending_p1_label.grid()
        self._p1_inline_entry.grid()
        self._pending_p2_label.configure(text=f"P2 인게임: {p2_raw}")
        self._pending_p2_label.grid()
        self._p2_inline_entry.grid()
        self._btn_confirm_double.grid()
        self._btn_cancel_double.grid()
        self._p12_ac_wrap.grid()
        self._p12_active_entry = self._p1_inline_entry
        self._p1_inline_entry.focus_set()

    def _on_select_all(self) -> None:
        state = self._select_all_var.get()
        for var in self._pending_check_vars:
            var.set(state)
        self._on_check_changed()

    def _opp_ac_on_key(self, event) -> None:
        if event.keysym in ("Return", "Escape"):
            return
        typed = self._opp_inline_entry.get().strip()
        filtered = [m["name"] for m in self._members if typed.lower() in m["name"].lower()]
        self._opp_ac_listbox.delete(0, "end")
        for n in filtered:
            self._opp_ac_listbox.insert("end", f"  {n}")
        self._opp_ac_listbox.configure(height=min(len(filtered), 4))

    def _opp_ac_select(self, event=None) -> None:
        sel = self._opp_ac_listbox.curselection()
        if sel:
            name = self._opp_ac_listbox.get(sel[0]).strip()
            self._opp_inline_entry.delete(0, "end")
            self._opp_inline_entry.insert(0, name)
            self._opp_ac_listbox.configure(height=0)
            self._opp_ac_listbox.delete(0, "end")

    def _confirm_pending_inline(self) -> None:
        checked = [i for i, v in enumerate(self._pending_check_vars) if v.get()]
        if not checked:
            return

        # 더블 모드 (both_unmatched 단일 선택)
        if len(checked) == 1 and self._pending_matches[checked[0]].get("both_unmatched"):
            p1_name = self._p1_inline_entry.get().strip()
            p2_name = self._p2_inline_entry.get().strip()
            if not p1_name or not p2_name:
                self._log("[미확인 쌍] P1, P2 닉네임을 모두 입력해 주세요.")
                return
            member1 = next((m for m in self._members if m["name"].lower() == p1_name.lower()), None)
            member2 = next((m for m in self._members if m["name"].lower() == p2_name.lower()), None)
            if not member1:
                self._log(f"[미확인 쌍] P1 '{p1_name}' 닉네임이 클랜원 목록에 없습니다.")
                return
            if not member2:
                self._log(f"[미확인 쌍] P2 '{p2_name}' 닉네임이 클랜원 목록에 없습니다.")
                return
            self._cancel_pending_inline()
            self._send_pending_double_match(checked[0], member1, member2)
            return

        # 단일 모드
        name = self._opp_inline_entry.get().strip()
        if not name:
            self._log("[미확인 상대] 닉네임을 입력해 주세요.")
            return
        matched = next(
            (m for m in self._members if m["name"].lower() == name.lower()), None
        )
        if not matched:
            self._log(f"[미확인 상대] '{name}' 닉네임이 클랜원 목록에 없습니다.")
            return
        self._cancel_pending_inline()
        for idx in sorted(checked, reverse=True):
            if not self._pending_matches[idx].get("both_unmatched"):
                self._send_pending_match(idx, matched)

    def _cancel_pending_inline(self) -> None:
        for var in self._pending_check_vars:
            var.set(False)
        self._select_all_var.set(False)
        self._pending_input_frame.grid_remove()
        self._opp_inline_entry.delete(0, "end")
        self._opp_ac_listbox.configure(height=0)
        self._opp_ac_listbox.delete(0, "end")
        self._p1_inline_entry.delete(0, "end")
        self._p2_inline_entry.delete(0, "end")
        self._p12_ac_listbox.configure(height=0)
        self._p12_ac_listbox.delete(0, "end")
        self._p12_active_entry = None

    def _send_pending_match(self, idx: int, opp_member: dict) -> None:
        if idx >= len(self._pending_matches):
            return
        match_data  = self._pending_matches[idx]
        short_hash  = match_data["replay_hash"][:8]
        result_str  = "승리" if match_data["player1_won"] else "패배"
        opp_name    = opp_member["name"]

        try:
            database.send_match(
                tier_p1     = match_data["me_tier"],
                name_p1     = match_data["me_name"],
                tier_p2     = self._fmt_tier(opp_member.get("tier")),
                name_p2     = opp_name,
                player1_won = match_data["player1_won"],
                map         = match_data["map"],
                match_type  = match_data["match_type"],
                played_at   = match_data["played_at"],
                replay_hash = match_data["replay_hash"],
            )
            self._log(
                f"[수집 완료] 유형: {match_data['match_type']} | 맵: {match_data['map']} | "
                f"{match_data['me_name']} {result_str} vs {opp_name} | hash: {short_hash}"
            )
            winner_log = match_data["me_name"] if match_data["player1_won"] else opp_name
            loser_log  = opp_name if match_data["player1_won"] else match_data["me_name"]
            self._flash_success(winner_log, loser_log, match_data["match_type"])
        except DuplicateMatchError:
            self._log(f"[중복] 이미 등록된 경기입니다. | hash: {short_hash}")
        except RuntimeError as e:
            self._log(f"[전송 실패] {e} | hash: {short_hash}")
        except Exception as e:
            self._log(f"[전송 오류] {e} | hash: {short_hash}")
        finally:
            self._pending_matches.pop(idx)
            self._pending_check_vars.pop(idx)
            widget = self._pending_check_widgets.pop(idx)
            widget.destroy()

    def _send_pending_double_match(self, idx: int, member1: dict, member2: dict) -> None:
        if idx >= len(self._pending_matches):
            return
        match_data  = self._pending_matches[idx]
        short_hash  = match_data["replay_hash"][:8]
        player1_won = match_data["player1_won"]

        try:
            database.send_match(
                tier_p1=self._fmt_tier(member1.get("tier")),
                name_p1=member1["name"],
                tier_p2=self._fmt_tier(member2.get("tier")),
                name_p2=member2["name"],
                player1_won=player1_won,
                map=match_data["map"],
                match_type=match_data["match_type"],
                played_at=match_data["played_at"],
                replay_hash=match_data["replay_hash"],
            )
            winner_log = member1["name"] if player1_won else member2["name"]
            loser_log  = member2["name"] if player1_won else member1["name"]
            self._log(
                f"[수집 완료] 유형: {match_data['match_type']} | 맵: {match_data['map']} | "
                f"{winner_log} 승 vs {loser_log} | hash: {short_hash}"
            )
            self._flash_success(winner_log, loser_log, match_data["match_type"])
        except DuplicateMatchError:
            self._log(f"[중복] 이미 등록된 경기입니다. | hash: {short_hash}")
        except RuntimeError as e:
            self._log(f"[전송 실패] {e} | hash: {short_hash}")
        except Exception as e:
            self._log(f"[전송 오류] {e} | hash: {short_hash}")
        finally:
            self._pending_matches.pop(idx)
            self._pending_check_vars.pop(idx)
            widget = self._pending_check_widgets.pop(idx)
            widget.destroy()

    def _delete_pending_matches(self) -> None:
        checked = [i for i, v in enumerate(self._pending_check_vars) if v.get()]
        if not checked:
            self._log("[삭제] 삭제할 항목을 선택해 주세요.")
            return
        self._cancel_pending_inline()
        for idx in sorted(checked, reverse=True):
            self._pending_matches.pop(idx)
            self._pending_check_vars.pop(idx)
            widget = self._pending_check_widgets.pop(idx)
            widget.destroy()
        self._log(f"[삭제] {len(checked)}개 항목이 삭제되었습니다.")

    def _set_p12_active(self, entry: ctk.CTkEntry) -> None:
        self._p12_active_entry = entry

    def _p12_ac_on_key(self, event) -> None:
        if event.keysym in ("Return", "Escape") or self._p12_active_entry is None:
            return
        typed = self._p12_active_entry.get().strip()
        filtered = [m["name"] for m in self._members if typed.lower() in m["name"].lower()]
        self._p12_ac_listbox.delete(0, "end")
        for n in filtered:
            self._p12_ac_listbox.insert("end", f"  {n}")
        self._p12_ac_listbox.configure(height=min(len(filtered), 4))

    def _p12_ac_select(self, event=None) -> None:
        sel = self._p12_ac_listbox.curselection()
        if sel and self._p12_active_entry:
            name = self._p12_ac_listbox.get(sel[0]).strip()
            self._p12_active_entry.delete(0, "end")
            self._p12_active_entry.insert(0, name)
            self._p12_ac_listbox.configure(height=0)
            self._p12_ac_listbox.delete(0, "end")

    # ── 로그 ───────────────────────────────────────────────────────────────────

    def _flash_success(self, winner: str, loser: str, match_type: str) -> None:
        """전적 전송 완료 시 헤더 상태 텍스트를 3초간 녹색으로 깜빡입니다."""
        self._last_match_label.configure(
            text=f"최근: {winner} vs {loser}  ({match_type})",
            text_color=C_SUCCESS,
        )
        ticks = [6]  # 6 × 500ms = 3초

        def _tick():
            if ticks[0] <= 0:
                self._status_label.configure(
                    text=self._base_status_text, text_color=self._base_status_color,
                )
                self._status_dot.configure(text_color=self._base_status_color)
                self._last_match_label.configure(text_color=C_SUBTEXT)
                return
            color = C_SUCCESS if ticks[0] % 2 == 0 else C_CARD
            self._status_label.configure(text="전적 전송 완료!", text_color=color)
            self._status_dot.configure(text_color=color)
            ticks[0] -= 1
            self.after(500, _tick)

        _tick()

    def _clear_log(self) -> None:
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

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

    # ── 활동명 자동완성 ────────────────────────────────────────────────────────

    def _ac_set(self, name: str) -> None:
        self.member_entry.delete(0, "end")
        self.member_entry.insert(0, name)

    def _ac_on_key(self, event) -> None:
        if event.keysym in ("Down", "Up", "Return", "Escape", "Tab"):
            return
        typed = self.member_entry.get().strip()
        if typed:
            filtered = [n for n in self._ac_names if typed.lower() in n.lower()]
        else:
            filtered = self._ac_names
        self._ac_show(filtered)

    def _ac_show(self, names: list[str]) -> None:
        if not names:
            self._ac_hide()
            return
        if self._ac_popup is None or not self._ac_popup.winfo_exists():
            self._ac_popup = tk.Toplevel(self)
            self._ac_popup.overrideredirect(True)
            self._ac_popup.configure(bg=C_BORDER)
            self._ac_listbox = tk.Listbox(
                self._ac_popup,
                bg=C_CARD, fg=C_TEXT,
                selectbackground=C_ACCENT, selectforeground="#ffffff",
                font=("맑은 고딕", 12),
                borderwidth=0, highlightthickness=0,
                relief="flat", activestyle="none",
            )
            self._ac_listbox.pack(fill="both", expand=True, padx=1, pady=1)
            self._ac_listbox.bind("<ButtonRelease-1>", self._ac_select)
            self._ac_listbox.bind("<Return>",   self._ac_select)
            self._ac_listbox.bind("<FocusOut>", lambda e: self.after(100, self._ac_hide))

        self._ac_listbox.delete(0, "end")
        for name in names:
            self._ac_listbox.insert("end", f"  {name}")

        x = self.member_entry.winfo_rootx()
        y = self.member_entry.winfo_rooty() + self.member_entry.winfo_height() + 2
        w = self.member_entry.winfo_width()
        h = min(len(names), 6) * 28 + 4
        self._ac_popup.geometry(f"{w}x{h}+{x}+{y}")
        self._ac_popup.lift()
        self._ac_popup.deiconify()

    def _ac_hide(self) -> None:
        if self._ac_popup and self._ac_popup.winfo_exists():
            self._ac_popup.withdraw()

    def _ac_select(self, event=None) -> None:
        if self._ac_listbox:
            sel = self._ac_listbox.curselection()
            if sel:
                self._ac_set(self._ac_listbox.get(sel[0]).strip())
                self._ac_hide()

    def _ac_focus_list(self, event=None) -> None:
        if self._ac_popup and self._ac_popup.winfo_exists() and self._ac_listbox.size() > 0:
            self._ac_listbox.focus_set()
            self._ac_listbox.selection_set(0)
            self._ac_listbox.activate(0)

    # ── 공지 ───────────────────────────────────────────────────────────────────

    def _show_notice(self) -> None:
        if self._notice_text:
            self._log(f"[공지] {self._notice_text}")
        else:
            self._log("[공지] 현재 공지사항이 없습니다.")

    # ── 시스템 트레이 ───────────────────────────────────────────────────────────

    def _setup_tray(self) -> None:
        try:
            img = Image.open(_resource("public/favicon.ico")).convert("RGBA").resize((64, 64))
        except Exception:
            img = Image.new("RGBA", (64, 64), color=(79, 142, 247, 255))

        menu = pystray.Menu(
            pystray.MenuItem("열기", lambda icon, item: self.after(0, self._restore_window), default=True),
            pystray.MenuItem("종료", lambda icon, item: self._quit_app()),
        )
        self._tray_icon = pystray.Icon("TuFlauncher", img, "TuFlauncher", menu)
        threading.Thread(target=self._tray_icon.run, daemon=True).start()

    def _hide_to_tray(self) -> None:
        self.withdraw()
        threading.Thread(
            target=_send_notification,
            args=("TuFlauncher", "런처가 백그라운드에서 실행 중입니다."),
            daemon=True,
        ).start()

    def _restore_window(self) -> None:
        self.deiconify()
        self.lift()
        self.focus_force()

    def _quit_app(self, icon=None, item=None) -> None:
        self._stop_watcher()
        if self._tray_icon:
            self._tray_icon.stop()
            self._tray_icon = None
        self.after(0, self.destroy)

    # ── 종료 ───────────────────────────────────────────────────────────────────

    def on_closing(self):
        self._hide_to_tray()


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = App()
    app.protocol("WM_DELETE_WINDOW", app.on_closing)
    app.mainloop()
