#!/usr/bin/env python3
"""
Interview Cracker AI - AI-powered interview assistant
Helps you ace interviews by listening to questions and providing smart answers
"""

import customtkinter as ctk
import tkinter as tk
from tkinter import messagebox
import threading
import queue
import json
import os
import uuid
import hashlib
import socket
import time
import re
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

# Optional imports with graceful fallback
try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False

try:
    from PIL import Image, ImageGrab, ImageTk
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

try:
    import mss
    MSS_AVAILABLE = True
except ImportError:
    MSS_AVAILABLE = False

try:
    import pytesseract
    pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False

try:
    import speech_recognition as sr
    AUDIO_AVAILABLE = True
except ImportError:
    AUDIO_AVAILABLE = False

# ─── Configuration ────────────────────────────────────────────────────────────

CONFIG_FILE   = Path(__file__).parent / "config.json"
HISTORY_FILE  = Path(__file__).parent / "history.json"
ENV_FILE      = Path(__file__).parent / ".env"
APP_SECRET = "InterviewCrackerAI-2024-SecretKey"  # Used for license HMAC


def _load_env():
    """Load KEY=VALUE pairs from .env into os.environ (no third-party lib needed)."""
    if not ENV_FILE.exists():
        return
    with open(ENV_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

_load_env()


# ─── ConfigManager ────────────────────────────────────────────────────────────

class ConfigManager:
    def __init__(self, path: Path = CONFIG_FILE):
        self.path = path
        self._data = self.load()

    def load(self) -> dict:
        if self.path.exists():
            try:
                with open(self.path, "r") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"api_key": "", "license_key": "", "device_id": "", "model": "claude-opus-4-5"}

    def save(self):
        with open(self.path, "w") as f:
            json.dump(self._data, f, indent=2)

    def get(self, key, default=None):
        if key == "api_key":
            return os.environ.get("ANTHROPIC_API_KEY") or self._data.get(key, default)
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value
        self.save()


# ─── DeviceID ─────────────────────────────────────────────────────────────────

class DeviceID:
    @staticmethod
    def generate() -> str:
        try:
            mac = str(uuid.getnode())
            hostname = socket.gethostname()
            raw = f"{mac}-{hostname}-{APP_SECRET}"
            digest = hashlib.sha256(raw.encode()).hexdigest().upper()
            # Format as 8 hex characters like the screenshot
            return digest[:8]
        except Exception:
            return "UNKNOWN"


# ─── LicenseManager ───────────────────────────────────────────────────────────

class LicenseManager:
    def __init__(self, config: ConfigManager):
        self.config = config
        self.device_id = DeviceID.generate()

    def is_licensed(self) -> bool:
        # For now, always return True so users can start without a license
        # You can implement real license validation here
        stored_key = self.config.get("license_key", "")
        if stored_key == "DEMO-MODE":
            return True
        return self._validate(stored_key)

    def activate(self, key: str) -> tuple:
        key = key.strip().upper()
        if not key:
            return False, "Please enter a license key"
        if key == "DEMO-TRIAL-2024":
            self.config.set("license_key", "DEMO-MODE")
            return True, "Trial license activated!"
        if self._validate(key):
            self.config.set("license_key", key)
            return True, "License activated successfully!"
        return False, "Invalid license key"

    def _validate(self, key: str) -> bool:
        if not key:
            return False
        try:
            import hmac as _hmac
            sig = _hmac.new(
                APP_SECRET.encode(),
                self.device_id.encode(),
                hashlib.sha256
            ).hexdigest().upper()
            expected_key = sig[:16]  # type: ignore[index]
            return key == expected_key
        except Exception:
            return False


# ─── ScreenCapture ────────────────────────────────────────────────────────────

class ScreenCapture:
    def __init__(self, hide_callback=None, show_callback=None):
        self.hide_callback = hide_callback
        self.show_callback = show_callback

    def capture_screen(self):
        """Capture the primary screen as PIL Image"""
        if MSS_AVAILABLE:
            with mss.mss() as sct:
                monitor = sct.monitors[1]
                shot = sct.grab(monitor)
                img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
                return img
        elif PIL_AVAILABLE:
            return ImageGrab.grab()
        return None

    def extract_text(self, image) -> str:
        """Run OCR on image to extract text"""
        if not OCR_AVAILABLE or image is None:
            return ""
        try:
            config = "--psm 6 --oem 3"
            return pytesseract.image_to_string(image, config=config)
        except Exception:
            return ""

    def capture_and_extract(self, window=None) -> str:
        """Capture screen and extract text, hiding overlay if needed"""
        if window and self.hide_callback:
            self.hide_callback()
            time.sleep(0.15)
        try:
            img = self.capture_screen()
            return self.extract_text(img)
        finally:
            if window and self.show_callback:
                self.show_callback()


# ─── AudioListener ────────────────────────────────────────────────────────────

class AudioListener:
    def __init__(self, callback):
        self.callback = callback
        self._stop_event = threading.Event()
        self._thread = None
        self._recognizer = None
        self._mic = None
        self._bg_listener = None

    def start(self):
        if not AUDIO_AVAILABLE:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._bg_listener:
            try:
                self._bg_listener(wait_for_stop=False)
            except Exception:
                pass

    def _listen_loop(self):
        if not AUDIO_AVAILABLE:
            return
        try:
            self._recognizer = sr.Recognizer()
            self._mic = sr.Microphone()
            with self._mic as source:
                self._recognizer.adjust_for_ambient_noise(source, duration=1)

            def audio_callback(recognizer, audio):
                if self._stop_event.is_set():
                    return
                try:
                    text = recognizer.recognize_google(audio)
                    if text and len(text.strip()) > 10:
                        self.callback(("audio", text))
                except sr.UnknownValueError:
                    pass
                except Exception:
                    pass

            self._bg_listener = self._recognizer.listen_in_background(
                self._mic, audio_callback, phrase_time_limit=10
            )
            # Keep thread alive until stopped
            while not self._stop_event.is_set():
                time.sleep(0.5)
        except Exception:
            pass


# ─── ClaudeClient ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an expert interview assistant. You receive interview questions
captured from a screen or microphone. Provide clear, concise, and technically accurate answers.

Format your response as:
**Direct Answer:** (1-2 sentences)

**Key Points:**
- Point 1
- Point 2
- Point 3

Keep answers under 300 words unless depth is required. For coding questions, include a brief code example."""


class ClaudeClient:
    def __init__(self, api_key: str, model: str = "claude-opus-4-5"):
        self.api_key = api_key
        self.model = model
        self._executor = ThreadPoolExecutor(max_workers=3)
        self._client = None

    def _get_client(self):
        if not ANTHROPIC_AVAILABLE:
            raise RuntimeError("anthropic package not installed. Run: pip install anthropic")
        if not self.api_key:
            raise RuntimeError("No API key configured. Click settings to add your Anthropic API key.")
        if self._client is None:
            self._client = anthropic.Anthropic(api_key=self.api_key)
        return self._client

    def ask(self, question: str, system_prompt: str = None) -> str:
        client = self._get_client()
        message = client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=system_prompt or SYSTEM_PROMPT,
            messages=[{"role": "user", "content": f"Interview question: {question}"}]
        )
        return message.content[0].text

    def ask_async(self, question: str, callback, system_prompt: str = None):
        def run():
            try:
                answer = self.ask(question, system_prompt=system_prompt)
                callback(("answer", question, answer))
            except Exception as e:
                callback(("error", question, str(e)))
        self._executor.submit(run)

    def update_key(self, api_key: str):
        self.api_key = api_key
        self._client = None


# ─── AnswerPanel ──────────────────────────────────────────────────────────────

class AnswerPanel(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self._history: list = []   # list of (question, answer)
        self._idx: int = -1
        self._build()

    def _build(self):
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # Header
        header = ctk.CTkFrame(self, fg_color="#0F3460", corner_radius=6)
        header.grid(row=0, column=0, sticky="ew", padx=6, pady=(6, 3))

        ctk.CTkLabel(
            header, text="AI Answer",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#E94560"
        ).pack(side="left", padx=10, pady=6)

        ctk.CTkButton(
            header, text="Clear", width=55, height=24,
            fg_color="#2A2A4A", hover_color="#3A3A6A",
            font=ctk.CTkFont(size=10),
            command=self.clear
        ).pack(side="right", padx=8, pady=4)

        # Scrollable answer container
        self._scroll = ctk.CTkScrollableFrame(
            self, fg_color="#111122", corner_radius=6
        )
        self._scroll.grid(row=1, column=0, sticky="nsew", padx=6, pady=(3, 6))
        self._scroll.grid_columnconfigure(0, weight=1)

        self._question_label = ctk.CTkLabel(
            self._scroll, text="",
            font=ctk.CTkFont(size=10, slant="italic"),
            text_color="#888888", wraplength=340, justify="left", anchor="w"
        )
        self._question_label.grid(row=0, column=0, sticky="w", padx=8, pady=(8, 4))

        self._answer_label = ctk.CTkLabel(
            self._scroll, text="Waiting for question...",
            font=ctk.CTkFont(size=11),
            text_color="#DDDDDD", wraplength=340, justify="left", anchor="w"
        )
        self._answer_label.grid(row=1, column=0, sticky="w", padx=8, pady=(0, 8))

    def show_loading(self):
        self._question_label.configure(text="Processing question...")
        self._answer_label.configure(text="Thinking...", text_color="#FFA726")

    def show_answer(self, question: str, answer: str):
        self._history.append((question, answer))
        self._idx = len(self._history) - 1
        self._render(self._idx)

    def _render(self, idx: int):
        question, answer = self._history[idx]
        q_display = question.strip()[:120] + ("..." if len(question) > 120 else "")
        self._question_label.configure(text=f"Q: {q_display}")
        self._answer_label.configure(text=answer, text_color="#DDDDDD")

    def prev_answer(self):
        if self._history and self._idx > 0:
            self._idx -= 1
            self._render(self._idx)

    def next_answer(self):
        if self._history and self._idx < len(self._history) - 1:
            self._idx += 1
            self._render(self._idx)

    def get_last_answer(self) -> str:
        return self._history[-1][1] if self._history else ""

    def show_error(self, message: str):
        self._question_label.configure(text="Error occurred")
        self._answer_label.configure(text=f"Error: {message}", text_color="#FF6B6B")

    def clear(self):
        self._question_label.configure(text="")
        self._answer_label.configure(text="Waiting for question...", text_color="#DDDDDD")


# ─── HistoryManager ───────────────────────────────────────────────────────────

class HistoryManager:
    def __init__(self, path: Path = HISTORY_FILE):
        self.path = path
        self._data = self._load()
        self._current_session_id = None

    def _load(self) -> dict:
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"sessions": []}

    def _save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)

    def new_session(self):
        import datetime
        session = {
            "id": str(uuid.uuid4())[:8],
            "date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "messages": []
        }
        self._data["sessions"].insert(0, session)
        self._current_session_id = session["id"]
        self._save()

    def add_message(self, question: str, answer: str):
        import datetime
        if not self._current_session_id:
            return
        for s in self._data["sessions"]:
            if s["id"] == self._current_session_id:
                s["messages"].append({
                    "question": question,
                    "answer": answer,
                    "timestamp": datetime.datetime.now().strftime("%H:%M:%S")
                })
                self._save()
                break

    def get_sessions(self) -> list:
        return self._data.get("sessions", [])

    def get_stats(self) -> dict:
        import datetime
        sessions = self.get_sessions()
        total_sessions = len(sessions)
        total_messages = sum(len(s["messages"]) for s in sessions)
        week_ago = (datetime.datetime.now() - datetime.timedelta(days=7)).strftime("%Y-%m-%d")
        this_week = sum(
            1 for s in sessions
            if s.get("date", "") >= week_ago and len(s["messages"]) > 0
        )
        return {"sessions": total_sessions, "messages": total_messages, "this_week": this_week}


# ─── ConversationHistoryDialog ─────────────────────────────────────────────────

class ConversationHistoryDialog(ctk.CTkToplevel):
    def __init__(self, master, history_mgr: HistoryManager):
        super().__init__(master)
        self.history_mgr = history_mgr
        self._selected_session = None

        self.title("Conversation History")
        self.geometry("900x580")
        self.resizable(False, False)
        self.attributes("-topmost", True)
        self.grab_set()
        self.configure(fg_color="#0D0D1A")
        self.overrideredirect(True)

        # Centre on screen
        self.update_idletasks()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"900x580+{(sw-900)//2}+{(sh-580)//2}")

        self._build()

    def _build(self):
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # ── Header ────────────────────────────────────────────────────────────
        header = ctk.CTkFrame(self, fg_color="#111120", corner_radius=0, height=52)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_propagate(False)
        header.grid_columnconfigure(0, weight=1)
        header.bind("<Button-1>", self._drag_start)
        header.bind("<B1-Motion>", self._drag_move)

        ctk.CTkLabel(
            header, text="Conversation History",
            font=ctk.CTkFont(size=18, weight="bold"), text_color="white"
        ).grid(row=0, column=0, padx=20, sticky="w")

        ctk.CTkButton(
            header, text="←", width=36, height=32,
            fg_color="#1E1E3A", hover_color="#2E2E5A",
            font=ctk.CTkFont(size=16, weight="bold"),
            command=self.destroy
        ).grid(row=0, column=1, padx=(0, 6), pady=10, sticky="e")

        ctk.CTkButton(
            header, text="✕", width=32, height=32,
            fg_color="#C0392B", hover_color="#E74C3C",
            font=ctk.CTkFont(size=12, weight="bold"),
            corner_radius=16,
            command=self.destroy
        ).grid(row=0, column=2, padx=12, pady=10, sticky="e")

        # ── Body ──────────────────────────────────────────────────────────────
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew", padx=20, pady=16)
        body.grid_rowconfigure(1, weight=1)
        body.grid_columnconfigure(0, weight=1)

        # Tab buttons
        tab_bar = ctk.CTkFrame(body, fg_color="transparent")
        tab_bar.grid(row=0, column=0, sticky="w", pady=(0, 12))

        self._tab_frames = {}
        self._tab_btns = {}

        tabs = [("📋  Sessions", "sessions"), ("💬  Conversation", "conversation"), ("📊  Stats", "stats")]
        for _, (label, key) in enumerate(tabs):
            btn = ctk.CTkButton(
                tab_bar, text=label, width=160, height=38,
                corner_radius=20,
                fg_color="#2D2D4A", hover_color="#3D3D6A",
                font=ctk.CTkFont(size=13, weight="bold"),
                command=lambda k=key: self._switch_tab(k)
            )
            btn.pack(side="left", padx=(0, 8))
            self._tab_btns[key] = btn

        # Tab content area
        content = ctk.CTkFrame(body, fg_color="transparent")
        content.grid(row=1, column=0, sticky="nsew")
        content.grid_rowconfigure(0, weight=1)
        content.grid_columnconfigure(0, weight=1)

        self._tab_frames["sessions"]      = self._build_sessions_tab(content)
        self._tab_frames["conversation"]  = self._build_conversation_tab(content)
        self._tab_frames["stats"]         = self._build_stats_tab(content)

        self._switch_tab("sessions")

    # ── Tabs ──────────────────────────────────────────────────────────────────

    def _switch_tab(self, key: str):
        for f in self._tab_frames.values():
            f.grid_remove()
        for b in self._tab_btns.values():
            b.configure(fg_color="#2D2D4A")
        self._tab_frames[key].grid(row=0, column=0, sticky="nsew")
        self._tab_btns[key].configure(fg_color="#4A4A8A")
        if key == "stats":
            self._refresh_stats()

    def _build_sessions_tab(self, parent):
        frame = ctk.CTkFrame(parent, fg_color="#1A1A2E", corner_radius=14)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(
            frame, text="📋  Conversation Sessions",
            font=ctk.CTkFont(size=15, weight="bold"), text_color="white"
        ).grid(row=0, column=0, sticky="w", padx=20, pady=(16, 8))

        sessions = self.history_mgr.get_sessions()
        if not sessions:
            ctk.CTkLabel(
                frame,
                text="No conversations yet\nStart a session to see your conversation history here",
                font=ctk.CTkFont(size=13), text_color="#555577",
                justify="center"
            ).grid(row=1, column=0, pady=40)
        else:
            scroll = ctk.CTkScrollableFrame(frame, fg_color="transparent")
            scroll.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
            scroll.grid_columnconfigure(0, weight=1)
            for i, s in enumerate(sessions):
                count = len(s["messages"])
                row_frame = ctk.CTkFrame(scroll, fg_color="#2D2D4A", corner_radius=10)
                row_frame.grid(row=i, column=0, sticky="ew", pady=4)
                row_frame.grid_columnconfigure(1, weight=1)
                ctk.CTkLabel(
                    row_frame, text=s["date"],
                    font=ctk.CTkFont(size=11), text_color="#7986CB"
                ).grid(row=0, column=0, padx=12, pady=8)
                ctk.CTkLabel(
                    row_frame, text=f"Session #{s['id']}",
                    font=ctk.CTkFont(size=12, weight="bold"), text_color="white"
                ).grid(row=0, column=1, padx=8, sticky="w")
                ctk.CTkLabel(
                    row_frame, text=f"{count} Q&A",
                    font=ctk.CTkFont(size=11), text_color="#4CAF50"
                ).grid(row=0, column=2, padx=12)
                ctk.CTkButton(
                    row_frame, text="View", width=60, height=26,
                    fg_color="#1E1E3A", hover_color="#3D3D6A",
                    font=ctk.CTkFont(size=10),
                    command=lambda sid=s["id"]: self._open_session(sid)
                ).grid(row=0, column=3, padx=(0, 10))
        return frame

    def _build_conversation_tab(self, parent):
        frame = ctk.CTkFrame(parent, fg_color="#1A1A2E", corner_radius=14)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(
            frame, text="💬  Conversation",
            font=ctk.CTkFont(size=15, weight="bold"), text_color="white"
        ).grid(row=0, column=0, sticky="w", padx=20, pady=(16, 8))

        self._conv_scroll = ctk.CTkScrollableFrame(frame, fg_color="transparent")
        self._conv_scroll.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
        self._conv_scroll.grid_columnconfigure(0, weight=1)

        self._conv_placeholder = ctk.CTkLabel(
            self._conv_scroll,
            text="Select a session from the Sessions tab to view its conversation.",
            font=ctk.CTkFont(size=12), text_color="#555577", justify="center"
        )
        self._conv_placeholder.grid(row=0, column=0, pady=40)
        return frame

    def _build_stats_tab(self, parent):
        frame = ctk.CTkFrame(parent, fg_color="#1A1A2E", corner_radius=14)
        frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            frame, text="📊  Conversation Stats",
            font=ctk.CTkFont(size=15, weight="bold"), text_color="white"
        ).grid(row=0, column=0, sticky="w", padx=20, pady=(16, 8))

        cards = ctk.CTkFrame(frame, fg_color="transparent")
        cards.grid(row=1, column=0, pady=20)

        self._stat_labels = {}
        for key, title in [("sessions", "TOTAL SESSIONS"), ("messages", "TOTAL MESSAGES"), ("this_week", "THIS WEEK")]:
            card = ctk.CTkFrame(cards, fg_color="#2D2D4A", corner_radius=12, width=160, height=90)
            card.pack(side="left", padx=10)
            card.pack_propagate(False)
            num = ctk.CTkLabel(card, text="0", font=ctk.CTkFont(size=30, weight="bold"), text_color="white")
            num.pack(pady=(16, 2))
            ctk.CTkLabel(card, text=title, font=ctk.CTkFont(size=10), text_color="#7986CB").pack()
            self._stat_labels[key] = num

        ctk.CTkLabel(
            frame, text="Track your conversation activity and engagement over time.",
            font=ctk.CTkFont(size=11), text_color="#555577"
        ).grid(row=2, column=0, pady=(0, 20))

        return frame

    def _refresh_stats(self):
        stats = self.history_mgr.get_stats()
        for key, lbl in self._stat_labels.items():
            lbl.configure(text=str(stats.get(key, 0)))

    def _open_session(self, session_id: str):
        self._selected_session = session_id
        # Clear conversation tab
        for w in self._conv_scroll.winfo_children():
            w.destroy()
        # Find session
        session = next((s for s in self.history_mgr.get_sessions() if s["id"] == session_id), None)
        if not session or not session["messages"]:
            ctk.CTkLabel(
                self._conv_scroll,
                text="No messages in this session.",
                font=ctk.CTkFont(size=12), text_color="#555577"
            ).grid(row=0, column=0, pady=40)
        else:
            for i, msg in enumerate(session["messages"]):
                q_frame = ctk.CTkFrame(self._conv_scroll, fg_color="#1E3A5F", corner_radius=10)
                q_frame.grid(row=i*2, column=0, sticky="ew", pady=(6, 2), padx=4)
                q_frame.grid_columnconfigure(0, weight=1)
                ctk.CTkLabel(q_frame, text=f"Q  [{msg['timestamp']}]",
                    font=ctk.CTkFont(size=9), text_color="#7986CB").grid(row=0, column=0, sticky="w", padx=10, pady=(6, 0))
                ctk.CTkLabel(q_frame, text=msg["question"],
                    font=ctk.CTkFont(size=12), text_color="white",
                    wraplength=750, justify="left").grid(row=1, column=0, sticky="w", padx=10, pady=(0, 8))

                a_frame = ctk.CTkFrame(self._conv_scroll, fg_color="#1A2A1A", corner_radius=10)
                a_frame.grid(row=i*2+1, column=0, sticky="ew", pady=(2, 6), padx=4)
                a_frame.grid_columnconfigure(0, weight=1)
                ctk.CTkLabel(a_frame, text="A",
                    font=ctk.CTkFont(size=9), text_color="#4CAF50").grid(row=0, column=0, sticky="w", padx=10, pady=(6, 0))
                ctk.CTkLabel(a_frame, text=msg["answer"],
                    font=ctk.CTkFont(size=11), text_color="#CCFFCC",
                    wraplength=750, justify="left").grid(row=1, column=0, sticky="w", padx=10, pady=(0, 8))

        self._switch_tab("conversation")

    # ── Drag ──────────────────────────────────────────────────────────────────

    def _drag_start(self, e):
        self._dx, self._dy = e.x, e.y

    def _drag_move(self, e):
        x = self.winfo_x() + e.x - self._dx
        y = self.winfo_y() + e.y - self._dy
        self.geometry(f"+{x}+{y}")


# ─── CustomizeDialog ──────────────────────────────────────────────────────────

class CustomizeDialog(ctk.CTkToplevel):
    """Full Customize panel with sidebar navigation, matching the app screenshot style."""

    SECTIONS = [
        ("👤", "Profile"),
        ("🖥", "Appearance"),
        ("🎙", "Audio"),
        ("🌐", "Language"),
        ("📷", "Capture"),
        ("⌨", "Keyboard"),
        ("🔍", "Search"),
        ("⚡", "Response Speed"),
        ("📡", "API Status"),
        ("⚠", "Advanced"),
    ]

    SIDEBAR_W = 180
    CONTENT_W = 560

    def __init__(self, master, config: ConfigManager, on_save=None):
        super().__init__(master)
        self.config = config
        self.on_save = on_save
        self._active_section = "Profile"
        self._section_btns: dict = {}
        self._content_frames: dict = {}

        self.title("Customize")
        total_w = self.SIDEBAR_W + self.CONTENT_W
        self.geometry(f"{total_w}x620")
        self.resizable(False, False)
        self.attributes("-topmost", True)
        self.grab_set()
        self.configure(fg_color="#1A1A2A")
        self.overrideredirect(True)

        self._build()
        self._switch_section("Profile")

    # ── Layout ────────────────────────────────────────────────────────────────

    def _build(self):
        self.grid_rowconfigure(0, weight=0)  # header
        self.grid_rowconfigure(1, weight=1)  # body
        self.grid_columnconfigure(0, weight=0)
        self.grid_columnconfigure(1, weight=1)

        self._build_header()
        self._build_sidebar()
        self._build_content_area()

    def _build_header(self):
        header = ctk.CTkFrame(self, fg_color="#111120", corner_radius=0, height=52)
        header.grid(row=0, column=0, columnspan=2, sticky="ew")
        header.grid_propagate(False)
        header.grid_columnconfigure(1, weight=1)
        header.grid_columnconfigure(2, weight=0)
        header.grid_columnconfigure(3, weight=0)

        ctk.CTkLabel(
            header, text="Customize",
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color="white"
        ).grid(row=0, column=0, padx=20, pady=12, sticky="w")

        ctk.CTkButton(
            header, text="← Back", width=80, height=32,
            fg_color="#1A2A1A", hover_color="#2A3A2A",
            font=ctk.CTkFont(size=11, weight="bold"),
            command=self.destroy
        ).grid(row=0, column=1, padx=(0, 4), pady=10, sticky="e")

        self._theme_btn = ctk.CTkButton(
            header, text="☀ Light", width=80, height=32,
            fg_color="#2A2A3A", hover_color="#3A3A4A",
            font=ctk.CTkFont(size=11),
            command=self._toggle_theme
        )
        self._theme_btn.grid(row=0, column=2, padx=(0, 8), pady=10, sticky="e")

        ctk.CTkButton(
            header, text="✕", width=32, height=32,
            fg_color="#C0392B", hover_color="#E74C3C",
            font=ctk.CTkFont(size=12, weight="bold"),
            corner_radius=16,
            command=self.destroy
        ).grid(row=0, column=3, padx=12, pady=10, sticky="e")

    def _toggle_theme(self):
        current = ctk.get_appearance_mode()
        if current == "Dark":
            ctk.set_appearance_mode("Light")
            self._theme_btn.configure(text="🌙 Dark")
        else:
            ctk.set_appearance_mode("Dark")
            self._theme_btn.configure(text="☀ Light")

    def _build_sidebar(self):
        sidebar = ctk.CTkFrame(
            self, fg_color="#111120", corner_radius=0,
            width=self.SIDEBAR_W
        )
        sidebar.grid(row=1, column=0, sticky="nsew")
        sidebar.grid_propagate(False)
        sidebar.grid_columnconfigure(0, weight=1)

        for i, (icon, label) in enumerate(self.SECTIONS):
            is_advanced = label == "Advanced"
            btn = ctk.CTkButton(
                sidebar,
                text=f"  {icon}  {label}",
                anchor="w",
                height=38,
                corner_radius=6,
                fg_color="transparent",
                hover_color="#2A2A3A",
                text_color="#CC3333" if is_advanced else "#AAAACC",
                font=ctk.CTkFont(size=12),
                command=lambda s=label: self._switch_section(s)
            )
            btn.grid(row=i, column=0, sticky="ew", padx=8, pady=(4 if i == 0 else 1, 1))
            self._section_btns[label] = btn

    def _build_content_area(self):
        self._content_host = ctk.CTkFrame(self, fg_color="#1E1E30", corner_radius=0)
        self._content_host.grid(row=1, column=1, sticky="nsew")
        self._content_host.grid_rowconfigure(0, weight=1)
        self._content_host.grid_columnconfigure(0, weight=1)

        builders = {
            "Profile":        self._page_profile,
            "Appearance":     self._page_appearance,
            "Audio":          self._page_audio,
            "Language":       self._page_language,
            "Capture":        self._page_capture,
            "Keyboard":       self._page_keyboard,
            "Search":         self._page_search,
            "Response Speed": self._page_response_speed,
            "API Status":     self._page_api_status,
            "Advanced":       self._page_advanced,
        }
        for name, builder in builders.items():
            frame = ctk.CTkScrollableFrame(
                self._content_host, fg_color="transparent", corner_radius=0
            )
            frame.grid(row=0, column=0, sticky="nsew")
            frame.grid_columnconfigure(0, weight=1)
            builder(frame)
            self._content_frames[name] = frame

    # ── Section switcher ──────────────────────────────────────────────────────

    def _switch_section(self, name: str):
        # Deactivate old button
        if self._active_section in self._section_btns:
            is_adv = self._active_section == "Advanced"
            self._section_btns[self._active_section].configure(
                fg_color="transparent",
                text_color="#CC3333" if is_adv else "#AAAACC"
            )
        # Activate new button
        is_adv = name == "Advanced"
        self._section_btns[name].configure(
            fg_color="#2E2E50",
            text_color="#CC3333" if is_adv else "white"
        )
        self._active_section = name

        # Hide all, show selected
        for n, frame in self._content_frames.items():
            if n == name:
                frame.lift()
            else:
                frame.lower()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _section_title(self, parent, text: str):
        ctk.CTkLabel(
            parent, text=text,
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color="white", anchor="w"
        ).grid(row=0, column=0, sticky="w", padx=20, pady=(20, 2))

        ctk.CTkFrame(parent, height=1, fg_color="#333355").grid(
            row=1, column=0, sticky="ew", padx=20, pady=(0, 16)
        )

    def _field_label(self, parent, row: int, text: str, hint: str = ""):
        ctk.CTkLabel(
            parent, text=text,
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#CCCCEE", anchor="w"
        ).grid(row=row, column=0, sticky="w", padx=20, pady=(10, 2))
        if hint:
            ctk.CTkLabel(
                parent, text=hint,
                font=ctk.CTkFont(size=10),
                text_color="#666688", anchor="w", wraplength=480
            ).grid(row=row + 1, column=0, sticky="w", padx=20, pady=(0, 4))

    def _save_btn(self, parent, row: int, command):
        ctk.CTkButton(
            parent, text="Save Changes", width=140, height=36,
            fg_color="#4CAF50", hover_color="#3d9142",
            font=ctk.CTkFont(size=12, weight="bold"),
            command=command
        ).grid(row=row, column=0, padx=20, pady=16, sticky="w")

    # ── Page: Profile ─────────────────────────────────────────────────────────

    def _page_profile(self, p: ctk.CTkScrollableFrame):
        self._section_title(p, "AI Profile")

        # Profile Type
        self._field_label(p, 2, "Profile Type")
        profile_badge = ctk.CTkFrame(p, fg_color="#2A3A5A", corner_radius=6)
        profile_badge.grid(row=3, column=0, sticky="w", padx=20, pady=(0, 4))
        ctk.CTkLabel(
            profile_badge, text="Job Interview",
            font=ctk.CTkFont(size=11), text_color="#7AB4FF"
        ).pack(padx=10, pady=4)

        self._profile_type_var = ctk.StringVar(
            value=self.config.get("profile_type", "Job Interview")
        )
        ctk.CTkOptionMenu(
            p, variable=self._profile_type_var,
            values=["Job Interview", "Technical Interview", "Behavioral Interview",
                    "System Design", "Coding Challenge", "HR Round", "General Meeting"],
            width=480, height=38, fg_color="#252535",
            button_color="#333355", dropdown_fg_color="#1E1E30",
            font=ctk.CTkFont(size=12)
        ).grid(row=4, column=0, sticky="w", padx=20, pady=(2, 8))

        # Resume / Job Context
        self._field_label(p, 5, "Resume / Job Context",
                          "Paste your RESUME, JOB DESCRIPTION, or key details here. "
                          "The AI will use this context to tailor its answers.")
        self._resume_text = ctk.CTkTextbox(
            p, width=480, height=160,
            fg_color="#111122", border_color="#333355", border_width=1,
            font=ctk.CTkFont(size=11), text_color="#DDDDDD",
            corner_radius=6
        )
        self._resume_text.grid(row=7, column=0, sticky="ew", padx=20, pady=(2, 4))
        saved_resume = self.config.get("resume_context", "")
        if saved_resume:
            self._resume_text.insert("1.0", saved_resume)
        ctk.CTkLabel(
            p, text="Paste your resume or job details here. The AI will ALWAYS retain this context.",
            font=ctk.CTkFont(size=10), text_color="#555577", anchor="w"
        ).grid(row=8, column=0, sticky="w", padx=20)

        # Custom AI Instructions
        self._field_label(p, 9, "Custom AI Instructions",
                          "Add specific instructions for how you want the AI to behave "
                          "(e.g., 'Be concise', 'Speak like a senior engineer').")
        self._instructions_text = ctk.CTkTextbox(
            p, width=480, height=100,
            fg_color="#111122", border_color="#333355", border_width=1,
            font=ctk.CTkFont(size=11), text_color="#DDDDDD",
            corner_radius=6
        )
        self._instructions_text.grid(row=11, column=0, sticky="ew", padx=20, pady=(2, 4))
        saved_instr = self.config.get("custom_instructions", "")
        if saved_instr:
            self._instructions_text.insert("1.0", saved_instr)

        shortcuts_row = ctk.CTkFrame(p, fg_color="transparent")
        shortcuts_row.grid(row=12, column=0, sticky="ew", padx=20, pady=(0, 4))
        shortcuts_row.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            shortcuts_row,
            text="Specific behavior instructions for the AI model.",
            font=ctk.CTkFont(size=10), text_color="#555577"
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(
            shortcuts_row, text="Shortcuts", width=90, height=28,
            fg_color="#252535", hover_color="#353555",
            font=ctk.CTkFont(size=11),
            command=self._show_shortcuts
        ).grid(row=0, column=1, sticky="e")

        self._save_btn(p, 13, self._save_profile)

    def _save_profile(self):
        self.config.set("profile_type", self._profile_type_var.get())
        self.config.set("resume_context", self._resume_text.get("1.0", "end-1c"))
        self.config.set("custom_instructions", self._instructions_text.get("1.0", "end-1c"))
        self._notify_save()

    # ── Page: Appearance ──────────────────────────────────────────────────────

    def _page_appearance(self, p: ctk.CTkScrollableFrame):
        self._section_title(p, "Appearance")

        # Theme
        self._theme_var = ctk.StringVar(value=self.config.get("theme", "Dark"))
        self._labeled_dropdown(p, 2, "Theme", "Dark",
                               "Choose a color theme for the interface",
                               self._theme_var,
                               ["Dark", "Light", "System"])

        # Layout Mode
        self._layout_var = ctk.StringVar(value=self.config.get("layout_mode", "Normal"))
        self._labeled_dropdown(p, 6, "Layout Mode", self.config.get("layout_mode", "Normal"),
                               "Standard layout with comfortable spacing",
                               self._layout_var,
                               ["Normal", "Compact", "Minimal"])

        # Response Display Style
        ctk.CTkLabel(
            p, text="Response Display Style",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#CCCCEE", anchor="w"
        ).grid(row=10, column=0, sticky="w", padx=20, pady=(14, 4))
        self._display_style_var = ctk.StringVar(
            value=self.config.get("display_style", "Pagination")
        )
        style_seg = ctk.CTkSegmentedButton(
            p, values=["Pagination", "Scrolling"],
            variable=self._display_style_var,
            width=480, height=40,
            fg_color="#1A1A2A", selected_color="#2A4A8A", unselected_color="#1A1A2A",
            font=ctk.CTkFont(size=12, weight="bold")
        )
        style_seg.grid(row=11, column=0, sticky="ew", padx=20, pady=(0, 4))
        ctk.CTkLabel(
            p, text="One response at a time. Use Ctrl+Shift+[ and ] or Prev/Next buttons to navigate.",
            font=ctk.CTkFont(size=10), text_color="#555577", anchor="w"
        ).grid(row=12, column=0, sticky="w", padx=20, pady=(0, 12))

        # Background Transparency
        self._opacity_var = ctk.IntVar(
            value=int(float(self.config.get("opacity", 0.80)) * 100)
        )
        self._slider_field(
            p, 13, "Background Transparency",
            self._opacity_var, 0, 100, "%",
            left_label="Transparent", right_label="Opaque",
            show_pct=True
        )

        # Response Font Size
        self._font_size_var = ctk.IntVar(value=int(self.config.get("font_size", 14)))
        self._slider_field(
            p, 17, "Response Font Size",
            self._font_size_var, 12, 32, "px",
            left_label="12px", right_label="32px"
        )

        self._save_btn(p, 21, self._save_appearance)

    def _labeled_dropdown(self, parent, start_row, label, badge_text, hint,
                          var, values):
        """Helper: label + badge on one row, then dropdown, then hint."""
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.grid(row=start_row, column=0, sticky="w", padx=20, pady=(14, 0))
        ctk.CTkLabel(
            row, text=label,
            font=ctk.CTkFont(size=13, weight="bold"), text_color="#CCCCEE"
        ).pack(side="left")
        badge = ctk.CTkFrame(row, fg_color="#2A3A5A", corner_radius=4)
        badge.pack(side="left", padx=(8, 0))
        ctk.CTkLabel(
            badge, text=badge_text,
            font=ctk.CTkFont(size=10), text_color="#7AB4FF"
        ).pack(padx=8, pady=2)

        ctk.CTkOptionMenu(
            parent, variable=var, values=values,
            width=480, height=38,
            fg_color="#252535", button_color="#333355",
            dropdown_fg_color="#1E1E30", font=ctk.CTkFont(size=12)
        ).grid(row=start_row + 1, column=0, sticky="w", padx=20, pady=(4, 0))

        ctk.CTkLabel(
            parent, text=hint,
            font=ctk.CTkFont(size=10), text_color="#555577", anchor="w"
        ).grid(row=start_row + 2, column=0, sticky="w", padx=20, pady=(2, 0))

    def _slider_field(self, parent, start_row, label, var, from_, to, unit,
                      left_label="", right_label="", show_pct=False):
        """Helper: label row, slider with value badge, min/max labels."""
        # Header row with current value badge
        hrow = ctk.CTkFrame(parent, fg_color="transparent")
        hrow.grid(row=start_row, column=0, sticky="ew", padx=20, pady=(14, 0))
        hrow.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            hrow, text=label,
            font=ctk.CTkFont(size=13, weight="bold"), text_color="#CCCCEE"
        ).grid(row=0, column=0, sticky="w")
        val_badge = ctk.CTkLabel(
            hrow,
            text=f"{var.get()}{unit}",
            font=ctk.CTkFont(size=11, weight="bold"), text_color="white",
            fg_color="#2A3A5A", corner_radius=4
        )
        val_badge.grid(row=0, column=1, sticky="e")

        slider = ctk.CTkSlider(
            parent, from_=from_, to=to, variable=var,
            width=480, progress_color="#555577",
            command=lambda v: val_badge.configure(text=f"{int(v)}{unit}")
        )
        slider.grid(row=start_row + 1, column=0, sticky="ew", padx=20, pady=(4, 0))

        minmax = ctk.CTkFrame(parent, fg_color="transparent")
        minmax.grid(row=start_row + 2, column=0, sticky="ew", padx=20)
        minmax.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            minmax, text=left_label,
            font=ctk.CTkFont(size=10), text_color="#555577"
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            minmax, text=right_label,
            font=ctk.CTkFont(size=10), text_color="#555577"
        ).grid(row=0, column=1, sticky="e")

    def _save_appearance(self):
        self.config.set("theme", self._theme_var.get())
        self.config.set("layout_mode", self._layout_var.get())
        self.config.set("display_style", self._display_style_var.get())
        self.config.set("opacity", round(self._opacity_var.get() / 100, 2))
        self.config.set("font_size", self._font_size_var.get())
        self._notify_save()

    # ── Page: Audio ───────────────────────────────────────────────────────────

    def _page_audio(self, p: ctk.CTkScrollableFrame):
        self._section_title(p, "Audio Settings")

        # Audio Mode
        self._audio_mode_var = ctk.StringVar(
            value=self.config.get("audio_mode", "Speaker Only (Interviewer)")
        )
        self._labeled_dropdown(
            p, 2, "Audio Mode",
            self.config.get("audio_mode", "Speaker Only (Interviewer)"),
            "Choose which audio sources to capture for the AI.",
            self._audio_mode_var,
            ["Speaker Only (Interviewer)", "Microphone Only (You)", "Both Speaker & Mic",
             "System Audio (Loopback)"]
        )

        # Audio Chunk Speed
        self._chunk_speed_var = ctk.StringVar(
            value=self.config.get("chunk_speed", "30ms - Extreme Fast (Recommended)")
        )
        self._labeled_dropdown(
            p, 6, "Audio Chunk Speed (Latency)",
            self.config.get("chunk_speed", "30ms - Extreme Fast (Recommended)"),
            "Faster chunks result in lower latency but may increase CPU usage.",
            self._chunk_speed_var,
            ["30ms - Extreme Fast (Recommended)", "100ms - Fast", "200ms - Normal",
             "500ms - Slow (Low CPU)"]
        )

        # Response Speed
        self._audio_response_speed_var = ctk.StringVar(
            value=self.config.get("audio_response_speed", "Medium (2 seconds)")
        )
        self._labeled_dropdown(
            p, 10, "Response Speed",
            self.config.get("audio_response_speed", "Medium (2 seconds)"),
            "How quickly the AI responds to your voice. Faster = more eager to respond.",
            self._audio_response_speed_var,
            ["Instant (0.5s)", "Fast (1 second)", "Medium (2 seconds)",
             "Slow (3 seconds)", "Patient (5 seconds)"]
        )

        self._save_btn(p, 14, self._save_audio)

    def _save_audio(self):
        self.config.set("audio_mode", self._audio_mode_var.get())
        self.config.set("chunk_speed", self._chunk_speed_var.get())
        self.config.set("audio_response_speed", self._audio_response_speed_var.get())
        self._notify_save()

    # ── Page: Language ────────────────────────────────────────────────────────

    def _page_language(self, p: ctk.CTkScrollableFrame):
        self._section_title(p, "Language")

        self._lang_var = ctk.StringVar(
            value=self.config.get("speech_language", "English (US)")
        )
        self._labeled_dropdown(
            p, 2, "Speech Language",
            self.config.get("speech_language", "English (US)"),
            "Language for speech recognition and AI responses",
            self._lang_var,
            ["English (US)", "English (UK)", "Spanish (ES)", "French (FR)",
             "German (DE)", "Chinese (Simplified)", "Japanese (JP)",
             "Korean (KR)", "Portuguese (BR)", "Hindi (IN)", "Arabic (AR)"]
        )

        self._save_btn(p, 6, self._save_language)

    def _save_language(self):
        self.config.set("speech_language", self._lang_var.get())
        self._notify_save()

    # ── Page: Capture ─────────────────────────────────────────────────────────

    def _page_capture(self, p: ctk.CTkScrollableFrame):
        self._section_title(p, "Screen Capture")

        # Image Quality
        self._img_quality_var = ctk.StringVar(
            value=self.config.get("image_quality", "Medium Quality")
        )
        self._labeled_dropdown(
            p, 2, "Image Quality",
            self.config.get("image_quality", "Medium Quality"),
            "Balanced quality and token usage",
            self._img_quality_var,
            ["Low Quality (Fast, Less Tokens)", "Medium Quality",
             "High Quality (Slower, More Tokens)"]
        )

        # Capture Interval
        self._interval_var = ctk.IntVar(value=int(self.config.get("capture_interval", 4)))
        self._slider_field(
            p, 6, "Capture Interval",
            self._interval_var, 1, 15, "s",
            left_label="1s (Fast)", right_label="15s (Slow)"
        )

        # Toggles
        self._ocr_en_var = ctk.BooleanVar(value=bool(self.config.get("ocr_enabled", True)))
        ctk.CTkCheckBox(
            p, text="Enable OCR text extraction from screenshots",
            variable=self._ocr_en_var,
            checkmark_color="white", fg_color="#2A4A8A",
            font=ctk.CTkFont(size=12)
        ).grid(row=10, column=0, sticky="w", padx=20, pady=(16, 4))

        self._hide_cap_var = ctk.BooleanVar(value=bool(self.config.get("hide_during_capture", True)))
        ctk.CTkCheckBox(
            p, text="Hide overlay window during each screenshot",
            variable=self._hide_cap_var,
            checkmark_color="white", fg_color="#2A4A8A",
            font=ctk.CTkFont(size=12)
        ).grid(row=11, column=0, sticky="w", padx=20, pady=4)

        # Tesseract path
        ctk.CTkLabel(
            p, text="Tesseract OCR Path",
            font=ctk.CTkFont(size=13, weight="bold"), text_color="#CCCCEE"
        ).grid(row=12, column=0, sticky="w", padx=20, pady=(14, 2))
        self._tesseract_entry = ctk.CTkEntry(
            p, width=480, font=ctk.CTkFont(size=11),
            placeholder_text=r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            fg_color="#111122"
        )
        self._tesseract_entry.grid(row=13, column=0, sticky="w", padx=20, pady=(0, 4))
        saved_tess = self.config.get(
            "tesseract_path", r"C:\Program Files\Tesseract-OCR\tesseract.exe"
        )
        self._tesseract_entry.insert(0, saved_tess)

        self._save_btn(p, 14, self._save_capture)

    def _save_capture(self):
        self.config.set("image_quality", self._img_quality_var.get())
        self.config.set("capture_interval", self._interval_var.get())
        self.config.set("ocr_enabled", self._ocr_en_var.get())
        self.config.set("hide_during_capture", self._hide_cap_var.get())
        self.config.set("tesseract_path", self._tesseract_entry.get().strip())
        self._notify_save()

    # ── Page: Keyboard ────────────────────────────────────────────────────────

    def _page_keyboard(self, p: ctk.CTkScrollableFrame):
        self._section_title(p, "Keyboard Shortcuts")

        shortcuts = [
            ("Move Window Up",            "Move the application window up",                    "Ctrl+Up"),
            ("Move Window Down",          "Move the application window down",                  "Ctrl+Down"),
            ("Move Window Left",          "Move the application window left",                  "Ctrl+Left"),
            ("Move Window Right",         "Move the application window right",                 "Ctrl+Right"),
            ("Toggle Window Visibility",  "Show/hide the application window",                  "Ctrl+\\"),
            ("Toggle Click-through Mode", "Enable/disable click-through functionality",        "Ctrl+M"),
            ("Ask Next Step",             "Take screenshot and ask AI for the next step suggestion", "Ctrl+Enter"),
            ("Previous Response",         "Navigate to the previous AI response",              "Ctrl+["),
            ("Next Response",             "Navigate to the next AI response",                  "Ctrl+]"),
            ("Start / Stop Interview",    "Toggle interview session on/off",                   "Ctrl+Shift+I"),
            ("Copy Last Answer",          "Copy the last AI response to clipboard",            "Ctrl+Shift+C"),
        ]

        # Column header
        header = ctk.CTkFrame(p, fg_color="transparent")
        header.grid(row=2, column=0, sticky="ew", padx=20, pady=(4, 2))
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            header, text="ACTION", font=ctk.CTkFont(size=10, weight="bold"),
            text_color="#555577", anchor="w"
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            header, text="SHORTCUT", font=ctk.CTkFont(size=10, weight="bold"),
            text_color="#555577"
        ).grid(row=0, column=1, sticky="e")

        for i, (action, desc, shortcut) in enumerate(shortcuts):
            row_frame = ctk.CTkFrame(p, fg_color="#111122", corner_radius=6)
            row_frame.grid(row=i + 3, column=0, sticky="ew", padx=20, pady=2)
            row_frame.grid_columnconfigure(0, weight=1)

            text_col = ctk.CTkFrame(row_frame, fg_color="transparent")
            text_col.grid(row=0, column=0, sticky="w", padx=12, pady=6)
            ctk.CTkLabel(
                text_col, text=action,
                font=ctk.CTkFont(size=12, weight="bold"), text_color="#CCCCEE", anchor="w"
            ).pack(anchor="w")
            ctk.CTkLabel(
                text_col, text=desc,
                font=ctk.CTkFont(size=10), text_color="#555577", anchor="w"
            ).pack(anchor="w")

            ctk.CTkButton(
                row_frame, text=shortcut,
                width=110, height=28,
                fg_color="#252540", hover_color="#353560",
                font=ctk.CTkFont(size=11, family="Courier"),
                text_color="#AAAACC", corner_radius=4,
                state="disabled"
            ).grid(row=0, column=1, padx=12, pady=6)

    # ── Page: Search ──────────────────────────────────────────────────────────

    def _page_search(self, p: ctk.CTkScrollableFrame):
        self._section_title(p, "Search")

        self._google_search_var = ctk.BooleanVar(
            value=bool(self.config.get("google_search_enabled", False))
        )
        check_frame = ctk.CTkFrame(p, fg_color="transparent")
        check_frame.grid(row=2, column=0, sticky="w", padx=20, pady=(8, 4))
        ctk.CTkCheckBox(
            check_frame, text="Enable Google Search",
            variable=self._google_search_var,
            checkmark_color="white", fg_color="#2A4A8A",
            font=ctk.CTkFont(size=13, weight="bold"), text_color="#CCCCEE"
        ).pack(anchor="w")

        ctk.CTkLabel(
            p,
            text="Allow the AI to search Google for up-to-date information during conversations.\n"
                 "Note: Changes take effect when starting a new AI session.",
            font=ctk.CTkFont(size=11), text_color="#555577",
            justify="left", anchor="w", wraplength=460
        ).grid(row=3, column=0, sticky="w", padx=20, pady=(4, 16))

        self._save_btn(p, 4, self._save_search)

    def _save_search(self):
        self.config.set("google_search_enabled", self._google_search_var.get())
        self._notify_save()

    # ── Page: Response Speed ──────────────────────────────────────────────────

    def _page_response_speed(self, p: ctk.CTkScrollableFrame):
        self._section_title(p, "Response Speed")

        # Safety Reset Timeout
        self._reset_timeout_var = ctk.IntVar(
            value=int(self.config.get("safety_reset_timeout", 5))
        )
        self._slider_field(
            p, 2, "Safety Reset Timeout",
            self._reset_timeout_var, 3, 10, "s",
            left_label="3s (Aggressive)", right_label="10s (Conservative)"
        )
        ctk.CTkLabel(
            p, text="How long to wait before resetting the AI if it gets stuck. "
                    "Lower is faster but riskier.",
            font=ctk.CTkFont(size=10), text_color="#555577", anchor="w", wraplength=460
        ).grid(row=6, column=0, sticky="w", padx=20, pady=(2, 4))

        # Silence Detection Trigger
        self._silence_trigger_var = ctk.IntVar(
            value=int(self.config.get("silence_trigger_ms", 1500))
        )
        self._slider_field(
            p, 7, "Silence Detection Trigger",
            self._silence_trigger_var, 100, 2000, "ms",
            left_label="100ms (Instant)", right_label="2000ms (Patient)"
        )
        ctk.CTkLabel(
            p, text="How long to wait for silence before the AI starts responding.",
            font=ctk.CTkFont(size=10), text_color="#555577", anchor="w"
        ).grid(row=11, column=0, sticky="w", padx=20, pady=(2, 4))

        # Recommended settings info box
        info = ctk.CTkFrame(p, fg_color="#1A2A1A", corner_radius=6)
        info.grid(row=12, column=0, sticky="ew", padx=20, pady=(4, 8))
        ctk.CTkLabel(
            info,
            text="Recommended Settings:\n"
                 "• Default (recommended): 1500ms\n"
                 "• Fast speakers: 800–1000ms\n"
                 "• Normal pace: 1200–1500ms\n"
                 "• Slow/thoughtful: 1500–2000ms",
            font=ctk.CTkFont(size=11), text_color="#88CC88",
            justify="left", anchor="w"
        ).pack(padx=12, pady=10, anchor="w")

        # Follow-up Protection Delay
        self._followup_delay_var = ctk.IntVar(
            value=int(self.config.get("followup_delay_s", 0))
        )
        self._slider_field(
            p, 13, "Follow-up Protection Delay",
            self._followup_delay_var, 0, 2, "s",
            left_label="0.2s (Instant)", right_label="1.5s (Safe)"
        )
        ctk.CTkLabel(
            p, text="Minimum time between responses to prevent double-triggering.",
            font=ctk.CTkFont(size=10), text_color="#555577", anchor="w"
        ).grid(row=17, column=0, sticky="w", padx=20, pady=(2, 4))

        self._save_btn(p, 18, self._save_response_speed)

    def _save_response_speed(self):
        self.config.set("safety_reset_timeout", self._reset_timeout_var.get())
        self.config.set("silence_trigger_ms", self._silence_trigger_var.get())
        self.config.set("followup_delay_s", self._followup_delay_var.get())
        self._notify_save()

    # ── Page: API Status ──────────────────────────────────────────────────────

    def _page_api_status(self, p: ctk.CTkScrollableFrame):
        self._section_title(p, "API Status & Usage")

        # RPM usage display
        rpm_used = int(self.config.get("session_requests", 0))
        rpm_max = 2000

        ctk.CTkLabel(
            p, text="Local Device Usage (RPM)",
            font=ctk.CTkFont(size=13, weight="bold"), text_color="#CCCCEE", anchor="w"
        ).grid(row=2, column=0, sticky="w", padx=20, pady=(8, 4))

        rpm_val_frame = ctk.CTkFrame(p, fg_color="transparent")
        rpm_val_frame.grid(row=3, column=0, sticky="w", padx=20, pady=(0, 4))
        ctk.CTkLabel(
            rpm_val_frame, text=str(rpm_used),
            font=ctk.CTkFont(size=36, weight="bold"),
            text_color="#4CAF50" if rpm_used < rpm_max * 0.8 else "#FF6B6B"
        ).pack(side="left")
        ctk.CTkLabel(
            rpm_val_frame, text=f" / {rpm_max} RPM",
            font=ctk.CTkFont(size=14), text_color="#888888"
        ).pack(side="left", padx=(4, 0))

        ctk.CTkLabel(
            p, text="Requests Per Minute (RPM) across all active users sharing this API key.",
            font=ctk.CTkFont(size=10), text_color="#555577", anchor="w"
        ).grid(row=4, column=0, sticky="w", padx=20, pady=(0, 6))

        # Progress bar
        pct = min(rpm_used / rpm_max, 1.0)
        prog = ctk.CTkProgressBar(p, width=480, height=12, corner_radius=4,
                                  progress_color="#4CAF50" if pct < 0.8 else "#FF6B6B")
        prog.set(pct)
        prog.grid(row=5, column=0, sticky="w", padx=20, pady=(0, 8))

        # Status message
        status_frame = ctk.CTkFrame(p, fg_color="#0F1F0F", corner_radius=4)
        status_frame.grid(row=6, column=0, sticky="ew", padx=20, pady=(0, 16))
        ctk.CTkFrame(status_frame, width=3, fg_color="#4CAF50", corner_radius=0).pack(
            side="left", fill="y"
        )
        ctk.CTkLabel(
            status_frame, text="API usage is within safe limits.",
            font=ctk.CTkFont(size=11), text_color="#4CAF50"
        ).pack(side="left", padx=10, pady=8)

        # What counts as a request
        ctk.CTkLabel(
            p, text="What counts as a request?",
            font=ctk.CTkFont(size=13, weight="bold"), text_color="#CCCCEE", anchor="w"
        ).grid(row=7, column=0, sticky="w", padx=20, pady=(4, 2))
        ctk.CTkLabel(
            p,
            text="• Each automated screenshot (every 2–5 seconds)\n"
                 "• Each manual screenshot analysis\n"
                 "• Each text message sent to the AI",
            font=ctk.CTkFont(size=11), text_color="#888888",
            justify="left", anchor="w"
        ).grid(row=8, column=0, sticky="w", padx=28, pady=(0, 16))

        # API Key entry
        ctk.CTkLabel(
            p, text="Anthropic API Key",
            font=ctk.CTkFont(size=13, weight="bold"), text_color="#CCCCEE", anchor="w"
        ).grid(row=9, column=0, sticky="w", padx=20, pady=(4, 2))
        api_row = ctk.CTkFrame(p, fg_color="transparent")
        api_row.grid(row=10, column=0, sticky="w", padx=20, pady=(0, 4))
        self._api_entry = ctk.CTkEntry(
            api_row, width=360, show="*",
            placeholder_text="sk-ant-api03-...",
            fg_color="#111122", font=ctk.CTkFont(size=11)
        )
        self._api_entry.pack(side="left")
        if self.config.get("api_key"):
            self._api_entry.insert(0, self.config.get("api_key"))

        self._show_key = False
        def toggle_show():
            self._show_key = not self._show_key
            self._api_entry.configure(show="" if self._show_key else "*")
            show_btn.configure(text="Hide" if self._show_key else "Show")
        show_btn = ctk.CTkButton(
            api_row, text="Show", width=70, height=32,
            fg_color="#252535", hover_color="#353555",
            font=ctk.CTkFont(size=11), command=toggle_show
        )
        show_btn.pack(side="left", padx=8)

        self._api_status_label = ctk.CTkLabel(
            p, text="● Not tested", font=ctk.CTkFont(size=11), text_color="#888888"
        )
        self._api_status_label.grid(row=11, column=0, sticky="w", padx=20, pady=(2, 4))

        ctk.CTkButton(
            p, text="Test Connection", width=150, height=32,
            fg_color="#1E3A5F", hover_color="#2E4A7F", font=ctk.CTkFont(size=12),
            command=self._test_api
        ).grid(row=12, column=0, sticky="w", padx=20, pady=(0, 12))

        self._save_btn(p, 13, self._save_api)

    def _test_api(self):
        key = self._api_entry.get().strip()
        if not key:
            self._api_status_label.configure(text="● No API key entered", text_color="#FF6B6B")
            return
        self._api_status_label.configure(text="⏳ Testing...", text_color="#FFA726")
        self.update()

        def do_test():
            try:
                import anthropic as _anth
                client = _anth.Anthropic(api_key=key)
                client.messages.create(
                    model="claude-haiku-4-5-20251001", max_tokens=10,
                    messages=[{"role": "user", "content": "Hi"}]
                )
                self.after(0, lambda: self._api_status_label.configure(
                    text="● Connected successfully", text_color="#4CAF50"
                ))
            except Exception as e:
                err = str(e)[:60]  # type: ignore[index]
                self.after(0, lambda: self._api_status_label.configure(
                    text=f"● Error: {err}", text_color="#FF6B6B"
                ))
        threading.Thread(target=do_test, daemon=True).start()

    def _save_api(self):
        self.config.set("api_key", self._api_entry.get().strip())
        self._notify_save()

    # ── Page: Advanced ────────────────────────────────────────────────────────

    def _page_advanced(self, p: ctk.CTkScrollableFrame):
        # Red title
        ctk.CTkLabel(
            p, text="Advanced",
            font=ctk.CTkFont(size=20, weight="bold"), text_color="#CC3333", anchor="w"
        ).grid(row=0, column=0, sticky="w", padx=20, pady=(20, 2))
        ctk.CTkFrame(p, height=1, fg_color="#333355").grid(
            row=1, column=0, sticky="ew", padx=20, pady=(0, 16)
        )

        # Data Management
        ctk.CTkLabel(
            p, text="Data Management",
            font=ctk.CTkFont(size=14, weight="bold"), text_color="#CC3333", anchor="w"
        ).grid(row=2, column=0, sticky="w", padx=20, pady=(4, 4))
        ctk.CTkLabel(
            p,
            text="Warning: This action will permanently delete all local data including "
                 "API keys, preferences, and session history. This cannot be undone.",
            font=ctk.CTkFont(size=11), text_color="#888888",
            justify="left", anchor="w", wraplength=460
        ).grid(row=3, column=0, sticky="w", padx=20, pady=(0, 12))

        ctk.CTkButton(
            p, text="Clear All Local Data",
            height=42, corner_radius=6,
            fg_color="transparent", hover_color="#3A0A0A",
            border_color="#CC3333", border_width=1,
            font=ctk.CTkFont(size=13, weight="bold"), text_color="#CC3333",
            command=self._clear_all_data
        ).grid(row=4, column=0, sticky="ew", padx=20, pady=(0, 16))

    def _clear_all_data(self):
        if messagebox.askyesno(
            "Clear All Local Data",
            "This will permanently delete all local data including API keys, "
            "preferences, and session history.\n\nThis cannot be undone. Continue?",
            parent=self
        ):
            CONFIG_FILE.unlink(missing_ok=True)
            messagebox.showinfo("Done", "All local data cleared. Restart the app.", parent=self)
            self.destroy()

    # ── Shortcuts popup ───────────────────────────────────────────────────────

    def _show_shortcuts(self):
        win = ctk.CTkToplevel(self)
        win.title("Prompt Shortcuts")
        win.geometry("400x300")
        win.attributes("-topmost", True)
        win.grab_set()
        shortcuts = [
            ("{role}", "Replaced with the selected Profile Type"),
            ("{resume}", "Replaced with your Resume / Job Context"),
            ("{question}", "The detected interview question"),
            ("{language}", "Your chosen response language"),
        ]
        ctk.CTkLabel(
            win, text="Prompt Shortcuts",
            font=ctk.CTkFont(size=14, weight="bold")
        ).pack(pady=(16, 8))
        for var, desc in shortcuts:
            row = ctk.CTkFrame(win, fg_color="#111122", corner_radius=4)
            row.pack(fill="x", padx=20, pady=3)
            ctk.CTkLabel(
                row, text=var,
                font=ctk.CTkFont(size=11, family="Courier"),
                text_color="#7986CB", width=110
            ).pack(side="left", padx=10, pady=6)
            ctk.CTkLabel(
                row, text=desc,
                font=ctk.CTkFont(size=11), text_color="#CCCCEE"
            ).pack(side="left")

    # ── Notify & close ────────────────────────────────────────────────────────

    def _notify_save(self):
        if self.on_save:
            self.on_save(
                self.config.get("api_key", ""),
                self.config.get("model", "claude-opus-4-5")
            )


# ─── LicenseDialog ────────────────────────────────────────────────────────────

class LicenseDialog(ctk.CTkToplevel):
    def __init__(self, master, license_mgr: LicenseManager, on_activate=None):
        super().__init__(master)
        self.license_mgr = license_mgr
        self.on_activate = on_activate
        self.title("Activate License")
        self.geometry("400x240")
        self.attributes("-topmost", True)
        self.grab_set()
        self._build()

    def _build(self):
        ctk.CTkLabel(
            self, text="Activate License",
            font=ctk.CTkFont(size=16, weight="bold")
        ).pack(pady=(16, 4))

        ctk.CTkLabel(
            self,
            text=f"Device ID: {self.license_mgr.device_id}",
            font=ctk.CTkFont(size=11, family="Courier"),
            text_color="#888888"
        ).pack(pady=2)

        ctk.CTkLabel(
            self, text="Use DEMO-TRIAL-2024 to try for free",
            font=ctk.CTkFont(size=10), text_color="#666666"
        ).pack(pady=(0, 8))

        self._entry = ctk.CTkEntry(
            self, width=340, placeholder_text="Enter license key..."
        )
        self._entry.pack(pady=4)

        self._msg = ctk.CTkLabel(self, text="", text_color="#FF6B6B")
        self._msg.pack(pady=2)

        ctk.CTkButton(
            self, text="Activate", width=140,
            fg_color="#533483", hover_color="#6344A3",
            command=self._activate
        ).pack(pady=8)

    def _activate(self):
        key = self._entry.get().strip()
        success, message = self.license_mgr.activate(key)
        if success:
            self._msg.configure(text=message, text_color="#4CAF50")
            if self.on_activate:
                self.on_activate()
            self.after(1500, self.destroy)
        else:
            self._msg.configure(text=message, text_color="#FF6B6B")


# ─── MainWindow ──────────────────────────────────────────────────────────────────────────────


class MainWindow(ctk.CTk):
    WINDOW_W   = 950
    EXPANDED_W = 1400
    WINDOW_H   = 720

    def __init__(self):
        super().__init__()

        self.config_mgr = ConfigManager()
        self.device_id = DeviceID.generate()
        self.license_mgr = LicenseManager(self.config_mgr)
        self.history_mgr = HistoryManager()
        self.claude = ClaudeClient(
            api_key=self.config_mgr.get("api_key", ""),
            model=self.config_mgr.get("model", "claude-opus-4-5")
        )

        self._running = False
        self._panel_visible = False
        self._text_queue = queue.Queue()
        self._last_text_hash = ""
        self._last_question = ""
        self._destroyed = False
        self._drag_x = 0
        self._drag_y = 0
        self._timer_seconds = 0
        self._timer_running = False
        self._analyze_count = 0
        self._analyze_max = 40
        self._answers: list = []   # (question, answer)
        self._answer_idx = -1

        self._audio_listener = AudioListener(
            callback=lambda item: self._text_queue.put(item)
        )
        self._screen_capture = ScreenCapture()

        self._configure_window()
        self._build_ui()
        self._bind_events()
        self._set_status("✨  Ready to start your AI assistant session", "#7986CB")

    # ── Window setup ──────────────────────────────────────────────────────────

    def _configure_window(self):
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")

        self.title("Interview Cracker AI")
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.85)
        self.configure(fg_color="#0A0A16")

        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        x = (sw - self.WINDOW_W) // 2
        y = (sh - self.WINDOW_H) // 2
        self.geometry(f"{self.WINDOW_W}x{self.WINDOW_H}+{x}+{y}")

        self.after(200, self._setup_taskbar)

    def _setup_taskbar(self):
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            GWL_EXSTYLE = -20
            WS_EX_TOOLWINDOW = 0x00000080
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_TOOLWINDOW)
        except Exception:
            pass

    # ── UI Build ──────────────────────────────────────────────────────────────

    def _get_days_left(self) -> int:
        import datetime
        first_run = self.config_mgr.get("first_run_date", "")
        if not first_run:
            today = datetime.datetime.now().strftime("%Y-%m-%d")
            self.config_mgr.set("first_run_date", today)
            return 7
        try:
            first = datetime.datetime.strptime(first_run, "%Y-%m-%d")
            elapsed = (datetime.datetime.now() - first).days
            return max(0, 7 - elapsed)
        except Exception:
            return 7

    def _build_ui(self):
        self.grid_rowconfigure(0, weight=0)   # top bar
        self.grid_rowconfigure(1, weight=1)   # content
        self.grid_rowconfigure(2, weight=0)   # bottom bar
        self.grid_columnconfigure(0, weight=1)

        # Build persistent top bar frame (contents swap between modes)
        self._top_bar = ctk.CTkFrame(self, fg_color="#111120", corner_radius=0, height=60)
        self._top_bar.grid(row=0, column=0, sticky="ew")
        self._top_bar.grid_propagate(False)

        # Build both content frames
        self._build_home_content()
        self._build_interview_content()
        self._build_bottom_bar()

        # Show home by default
        self._interview_frame.grid_remove()
        self._bottom_bar.grid_remove()

        # Dummy answer panel (kept for API compatibility)
        self.answer_panel = AnswerPanel(self, fg_color="#0D0D20", corner_radius=10)

        self._show_home_topbar()

    # ── Top Bar: Home ─────────────────────────────────────────────────────────

    def _show_home_topbar(self):
        for w in self._top_bar.winfo_children():
            w.destroy()
        for i in range(10):
            self._top_bar.grid_columnconfigure(i, weight=0)
        self._top_bar.grid_columnconfigure(1, weight=1)

        self._top_bar.bind("<Button-1>", self._on_drag_start)
        self._top_bar.bind("<B1-Motion>", self._on_drag_motion)

        days_left = self._get_days_left()
        ctk.CTkButton(
            self._top_bar, text=f"📅 {days_left}d left",
            width=110, height=36,
            fg_color="#1A3A1A", hover_color="#2A4A2A",
            text_color="#4CAF50",
            font=ctk.CTkFont(size=12, weight="bold"),
            corner_radius=18,
            command=self.open_license
        ).grid(row=0, column=0, padx=(16, 4), pady=12)

        btn_frame = ctk.CTkFrame(self._top_bar, fg_color="transparent")
        btn_frame.grid(row=0, column=2, padx=12, pady=10, sticky="e")

        self._reveal_btn = ctk.CTkButton(
            btn_frame, text="👁  Reveal",
            width=100, height=36,
            fg_color="#252535", hover_color="#353555",
            font=ctk.CTkFont(size=12, weight="bold"),
            corner_radius=18,
            command=self.toggle_reveal
        )
        self._reveal_btn.pack(side="left", padx=4)

        ctk.CTkButton(
            btn_frame, text="📋", width=38, height=36,
            fg_color="#252535", hover_color="#353555",
            font=ctk.CTkFont(size=16), corner_radius=18,
            command=self.open_history
        ).pack(side="left", padx=4)

        self._main_theme_btn = ctk.CTkButton(
            btn_frame, text="☀", width=38, height=36,
            fg_color="#252535", hover_color="#353555",
            font=ctk.CTkFont(size=16), corner_radius=18,
            command=self._toggle_main_theme
        )
        self._main_theme_btn.pack(side="left", padx=4)

        ctk.CTkButton(
            btn_frame, text="⚙", width=38, height=36,
            fg_color="#252535", hover_color="#353555",
            font=ctk.CTkFont(size=16), corner_radius=18,
            command=self.open_settings
        ).pack(side="left", padx=4)

        ctk.CTkButton(
            btn_frame, text="?", width=38, height=36,
            fg_color="#252535", hover_color="#353555",
            font=ctk.CTkFont(size=14, weight="bold"),
            corner_radius=18,
            command=self._show_help
        ).pack(side="left", padx=4)

        ctk.CTkButton(
            btn_frame, text="✕", width=38, height=36,
            fg_color="#C0392B", hover_color="#E74C3C",
            font=ctk.CTkFont(size=12, weight="bold"),
            corner_radius=18,
            command=self.close_app
        ).pack(side="left", padx=(4, 0))

    # ── Top Bar: Interview ────────────────────────────────────────────────────

    def _show_interview_topbar(self):
        for w in self._top_bar.winfo_children():
            w.destroy()
        # Reset column weights
        for i in range(30):
            self._top_bar.grid_columnconfigure(i, weight=0)
        self._top_bar.grid_columnconfigure(99, weight=1)  # spacer

        col = 0

        # 7d left
        days_left = self._get_days_left()
        ctk.CTkButton(
            self._top_bar, text=f"📅 {days_left}d left",
            width=110, height=36,
            fg_color="#1A3A1A", hover_color="#2A4A2A",
            text_color="#4CAF50",
            font=ctk.CTkFont(size=12, weight="bold"),
            corner_radius=18,
            command=self.open_license
        ).grid(row=0, column=col, padx=(12, 4), pady=12)
        col += 1

        # Timer
        self._timer_label = ctk.CTkLabel(
            self._top_bar, text="0s",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#AAAACC", width=40
        )
        self._timer_label.grid(row=0, column=col, padx=6)
        col += 1

        # < Prev
        ctk.CTkButton(
            self._top_bar, text="< Prev", width=72, height=36,
            fg_color="#252535", hover_color="#353555",
            font=ctk.CTkFont(size=11), corner_radius=18,
            command=self._prev_answer
        ).grid(row=0, column=col, padx=3)
        col += 1

        # 0 / 0 counter
        self._counter_btn = ctk.CTkButton(
            self._top_bar, text="0 / 0", width=62, height=36,
            fg_color="#1A3A5A", hover_color="#1A3A5A",
            text_color="#7AB4FF",
            font=ctk.CTkFont(size=12, weight="bold"),
            corner_radius=18,
            state="disabled"
        )
        self._counter_btn.grid(row=0, column=col, padx=3)
        col += 1

        # Next >
        ctk.CTkButton(
            self._top_bar, text="Next >", width=72, height=36,
            fg_color="#252535", hover_color="#353555",
            font=ctk.CTkFont(size=11), corner_radius=18,
            command=self._next_answer
        ).grid(row=0, column=col, padx=3)
        col += 1

        # Stop Listening
        ctk.CTkButton(
            self._top_bar, text="Stop Listening", width=128, height=36,
            fg_color="#2D2D40", hover_color="#3D3D55",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#CCCCCC",
            corner_radius=18,
            command=self.stop_interview
        ).grid(row=0, column=col, padx=6)
        col += 1

        # Live badge
        ctk.CTkLabel(
            self._top_bar, text="Live",
            font=ctk.CTkFont(size=13),
            text_color="#AAAAAA"
        ).grid(row=0, column=col, padx=4)
        col += 1

        # Spacer column
        spacer = ctk.CTkFrame(self._top_bar, fg_color="transparent", width=1)
        spacer.grid(row=0, column=99, sticky="ew")
        self._top_bar.grid_columnconfigure(99, weight=1)

        # Reveal (gold border highlight)
        self._reveal_btn = ctk.CTkButton(
            self._top_bar, text="👁  Reveal",
            width=100, height=36,
            fg_color="#252535", hover_color="#353555",
            border_color="#D4A017", border_width=2,
            font=ctk.CTkFont(size=12, weight="bold"),
            corner_radius=18,
            command=self.toggle_reveal
        )
        self._reveal_btn.grid(row=0, column=100, padx=4)

        # Hide Ctrl \
        ctk.CTkButton(
            self._top_bar, text="Hide   Ctrl  \\",
            width=140, height=36,
            fg_color="#252535", hover_color="#353555",
            font=ctk.CTkFont(size=11),
            corner_radius=18,
            command=self._toggle_visibility
        ).grid(row=0, column=101, padx=4)

        # Back arrow (go home)
        ctk.CTkButton(
            self._top_bar, text="←", width=38, height=36,
            fg_color="#252535", hover_color="#353555",
            font=ctk.CTkFont(size=16), corner_radius=18,
            command=self._go_home
        ).grid(row=0, column=102, padx=4)

        # Close
        ctk.CTkButton(
            self._top_bar, text="✕", width=38, height=36,
            fg_color="#C0392B", hover_color="#E74C3C",
            font=ctk.CTkFont(size=12, weight="bold"),
            corner_radius=18,
            command=self.close_app
        ).grid(row=0, column=103, padx=(4, 12))

    # ── Home content ──────────────────────────────────────────────────────────

    def _build_home_content(self):
        self._home_frame = ctk.CTkFrame(self, fg_color="transparent")
        self._home_frame.grid(row=1, column=0, sticky="nsew")
        self._home_frame.grid_rowconfigure(0, weight=1)
        self._home_frame.grid_columnconfigure(0, weight=1)

        center = ctk.CTkFrame(self._home_frame, fg_color="transparent")
        center.grid(row=0, column=0)
        center.grid_columnconfigure(0, weight=1)

        # Logo pill (drag handle)
        logo_pill = ctk.CTkFrame(center, fg_color="#1A1A3A", corner_radius=28)
        logo_pill.grid(row=0, column=0, pady=(0, 48))
        logo_pill.bind("<Button-1>", self._on_drag_start)
        logo_pill.bind("<B1-Motion>", self._on_drag_motion)

        inner = ctk.CTkFrame(logo_pill, fg_color="transparent")
        inner.pack(padx=28, pady=16)
        inner.bind("<Button-1>", self._on_drag_start)
        inner.bind("<B1-Motion>", self._on_drag_motion)

        ctk.CTkLabel(inner, text="⚡", font=ctk.CTkFont(size=26)).pack(side="left", padx=(0, 10))
        ctk.CTkLabel(
            inner, text="Interview Cracker AI",
            font=ctk.CTkFont(size=24, weight="bold"), text_color="white"
        ).pack(side="left")

        # Start Interview
        self._interview_btn = ctk.CTkButton(
            center, text="Start Interview",
            width=350, height=54, corner_radius=27,
            fg_color="#252535", hover_color="#353550",
            font=ctk.CTkFont(size=15, weight="bold"),
            command=self.toggle_interview
        )
        self._interview_btn.grid(row=1, column=0, pady=10)

        # Activate License
        ctk.CTkButton(
            center, text="🔑  Activate License",
            width=350, height=50, corner_radius=25,
            fg_color="#252535", hover_color="#353550",
            font=ctk.CTkFont(size=14),
            command=self.open_license
        ).grid(row=2, column=0, pady=10)

        # Status
        self._status_label = ctk.CTkLabel(
            center, text="✨  Ready to start your AI assistant session",
            font=ctk.CTkFont(size=13),
            text_color="#7986CB", wraplength=540, justify="center"
        )
        self._status_label.grid(row=3, column=0, pady=(32, 4))

        ctk.CTkLabel(
            center, text="Your intelligent companion for interviews and meetings",
            font=ctk.CTkFont(size=12), text_color="#555577"
        ).grid(row=4, column=0, pady=(0, 28))

        # Device ID row
        id_frame = ctk.CTkFrame(center, fg_color="transparent")
        id_frame.grid(row=5, column=0)

        ctk.CTkLabel(
            id_frame,
            text=f"🖥  Your Device ID:  {self.device_id}",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#7986CB"
        ).pack(side="left", padx=(0, 12))

        ctk.CTkButton(
            id_frame, text="📋  Copy", width=90, height=32,
            fg_color="#252540", hover_color="#353560",
            font=ctk.CTkFont(size=11), corner_radius=16,
            command=self.copy_device_id
        ).pack(side="left")

        ctk.CTkLabel(
            center, text="(Send this to get your license key)",
            font=ctk.CTkFont(size=11), text_color="#444466"
        ).grid(row=6, column=0, pady=(6, 0))

        caps = []
        if not ANTHROPIC_AVAILABLE: caps.append("anthropic")
        if not OCR_AVAILABLE:       caps.append("pytesseract")
        if not AUDIO_AVAILABLE:     caps.append("SpeechRecognition")
        if caps:
            ctk.CTkLabel(
                center,
                text=f"Missing: {', '.join(caps)}\nRun: pip install {' '.join(caps)}",
                font=ctk.CTkFont(size=9), text_color="#FF6B6B",
                wraplength=300, justify="center"
            ).grid(row=7, column=0, pady=(14, 0))

    # ── Interview content ─────────────────────────────────────────────────────

    def _build_interview_content(self):
        self._interview_frame = ctk.CTkFrame(self, fg_color="transparent")
        self._interview_frame.grid(row=1, column=0, sticky="nsew")
        self._interview_frame.grid_rowconfigure(0, weight=1)
        self._interview_frame.grid_columnconfigure(0, weight=1)

        # Large dark listening area
        listen_area = ctk.CTkFrame(
            self._interview_frame,
            fg_color="#0D0D18",
            corner_radius=12
        )
        listen_area.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        listen_area.grid_rowconfigure(1, weight=1)
        listen_area.grid_columnconfigure(0, weight=1)

        # Listening / question text
        self._listen_label = ctk.CTkLabel(
            listen_area,
            text="Hey, I'm listening to your Job Interview...",
            font=ctk.CTkFont(size=18, slant="italic"),
            text_color="#555577",
            wraplength=860, justify="left", anchor="nw"
        )
        self._listen_label.grid(row=0, column=0, sticky="nw", padx=24, pady=(24, 8))

        # Answer display
        self._interview_answer_label = ctk.CTkLabel(
            listen_area, text="",
            font=ctk.CTkFont(size=13),
            text_color="#DDDDDD",
            wraplength=860, justify="left", anchor="nw"
        )
        self._interview_answer_label.grid(row=1, column=0, sticky="nsew", padx=24, pady=(0, 16))

    # ── Bottom bar ────────────────────────────────────────────────────────────

    def _build_bottom_bar(self):
        self._bottom_bar = ctk.CTkFrame(
            self, fg_color="#111120", corner_radius=0, height=58
        )
        self._bottom_bar.grid(row=2, column=0, sticky="ew")
        self._bottom_bar.grid_propagate(False)
        self._bottom_bar.grid_columnconfigure(1, weight=1)

        # Shortcuts dropdown
        ctk.CTkButton(
            self._bottom_bar, text="Shortcuts  ∨",
            width=120, height=36,
            fg_color="#252535", hover_color="#353555",
            font=ctk.CTkFont(size=11), corner_radius=18,
            command=self._show_shortcuts_popup
        ).grid(row=0, column=0, padx=(14, 8), pady=11)

        # Message input
        self._msg_entry = ctk.CTkEntry(
            self._bottom_bar,
            placeholder_text="Type a message...",
            fg_color="transparent",
            border_color="#333355", border_width=0,
            font=ctk.CTkFont(size=13), text_color="#CCCCCC",
            height=36
        )
        self._msg_entry.grid(row=0, column=1, sticky="ew", padx=8, pady=11)
        self._msg_entry.bind("<Return>", lambda e: self._send_message())

        # Analyze button
        self._analyze_btn = ctk.CTkButton(
            self._bottom_bar,
            text=f"Analyze (0/{self._analyze_max})",
            width=145, height=36,
            fg_color="white", hover_color="#E8E8E8",
            text_color="#111111",
            font=ctk.CTkFont(size=12, weight="bold"),
            corner_radius=18,
            command=self._send_message
        )
        self._analyze_btn.grid(row=0, column=2, padx=4, pady=11)

        # Copy button
        ctk.CTkButton(
            self._bottom_bar, text="📋", width=36, height=36,
            fg_color="transparent", hover_color="#252535",
            font=ctk.CTkFont(size=16), corner_radius=18,
            command=self._copy_last_answer
        ).grid(row=0, column=3, padx=(0, 14), pady=11)

    # ── Events ────────────────────────────────────────────────────────────────

    def _bind_events(self):
        self.bind("<Escape>",              lambda _: self.close_app())
        self.bind("<Control-Up>",          lambda _: self._move_window(0, -20))
        self.bind("<Control-Down>",        lambda _: self._move_window(0, 20))
        self.bind("<Control-Left>",        lambda _: self._move_window(-20, 0))
        self.bind("<Control-Right>",       lambda _: self._move_window(20, 0))
        self.bind("<Control-backslash>",   lambda _: self._toggle_visibility())
        self.bind("<Control-m>",           lambda _: self._toggle_clickthrough())
        self.bind("<Control-I>",           lambda _: self.toggle_interview())
        self.bind("<Control-bracketleft>", lambda _: self._prev_answer())
        self.bind("<Control-bracketright>",lambda _: self._next_answer())
        self.bind("<Control-C>",           lambda _: self._copy_last_answer())
        self.bind("<Control-Return>",      lambda _: self._ask_next_step())

    def _on_drag_start(self, event):
        self._drag_x = event.x_root - self.winfo_x()
        self._drag_y = event.y_root - self.winfo_y()

    def _on_drag_motion(self, event):
        x = event.x_root - self._drag_x
        y = event.y_root - self._drag_y
        self.geometry(f"+{x}+{y}")

    # ── Timer ─────────────────────────────────────────────────────────────────

    def _start_timer(self):
        self._timer_seconds = 0
        self._timer_running = True

        def tick():
            while self._timer_running and not self._destroyed:
                time.sleep(1)
                self._timer_seconds += 1
                if not self._destroyed:
                    self.after(0, self._update_timer_label)

        threading.Thread(target=tick, daemon=True).start()

    def _stop_timer(self):
        self._timer_running = False

    def _update_timer_label(self):
        if hasattr(self, '_timer_label') and not self._destroyed:
            try:
                self._timer_label.configure(text=f"{self._timer_seconds}s")
            except Exception:
                pass

    # ── Answer navigation ─────────────────────────────────────────────────────

    def _prev_answer(self):
        if self._answers and self._answer_idx > 0:
            self._answer_idx -= 1
            self._render_current_answer()

    def _next_answer(self):
        if self._answers and self._answer_idx < len(self._answers) - 1:
            self._answer_idx += 1
            self._render_current_answer()

    def _render_current_answer(self):
        if not self._answers or self._answer_idx < 0:
            return
        q, a = self._answers[self._answer_idx]
        total = len(self._answers)
        idx   = self._answer_idx + 1
        q_preview = q.strip()[:80].replace("\n", " ")
        if hasattr(self, '_listen_label'):
            self._listen_label.configure(
                text=f"Q: {q_preview}{'...' if len(q) > 80 else ''}",
                text_color="#7986CB", slant="roman"
            )
        if hasattr(self, '_interview_answer_label'):
            self._interview_answer_label.configure(text=a)
        if hasattr(self, '_counter_btn'):
            try:
                self._counter_btn.configure(text=f"{idx} / {total}")
            except Exception:
                pass
        # Also sync legacy answer_panel
        self.answer_panel.show_answer(q, a)

    def _update_counter(self):
        total = len(self._answers)
        idx   = self._answer_idx + 1 if self._answer_idx >= 0 else 0
        if hasattr(self, '_counter_btn'):
            try:
                self._counter_btn.configure(text=f"{idx} / {total}")
            except Exception:
                pass

    # ── Message / Analyze ─────────────────────────────────────────────────────

    def _send_message(self):
        text = self._msg_entry.get().strip()
        if not text or not self.config_mgr.get("api_key"):
            return
        self._msg_entry.delete(0, "end")
        self._analyze_count += 1
        remaining = self._analyze_max - self._analyze_count
        self._analyze_btn.configure(
            text=f"Analyze ({self._analyze_count}/{self._analyze_max})"
        )
        if hasattr(self, '_listen_label'):
            self._listen_label.configure(
                text=f"Analyzing: {text[:70]}...",
                text_color="#FFA726", slant="italic"
            )
        self.claude.ask_async(
            text,
            callback=self._on_claude_result,
            system_prompt=self._build_system_prompt(text)
        )

    def _show_shortcuts_popup(self):
        win = ctk.CTkToplevel(self)
        win.title("Shortcuts")
        win.geometry("420x280")
        win.attributes("-topmost", True)
        win.grab_set()
        shortcuts = [
            ("Ctrl+Enter",   "Analyze current screen"),
            ("Ctrl+[",       "Previous answer"),
            ("Ctrl+]",       "Next answer"),
            ("Ctrl+C",       "Copy last answer"),
            ("Ctrl+\\",      "Hide/show window"),
            ("Ctrl+M",       "Toggle click-through"),
            ("Ctrl+I",       "Start/stop interview"),
        ]
        ctk.CTkLabel(
            win, text="Keyboard Shortcuts",
            font=ctk.CTkFont(size=14, weight="bold")
        ).pack(pady=(14, 8))
        for key, desc in shortcuts:
            row = ctk.CTkFrame(win, fg_color="#111122", corner_radius=4)
            row.pack(fill="x", padx=20, pady=2)
            ctk.CTkLabel(
                row, text=key,
                font=ctk.CTkFont(size=11, family="Courier"),
                text_color="#7986CB", width=110
            ).pack(side="left", padx=10, pady=5)
            ctk.CTkLabel(
                row, text=desc,
                font=ctk.CTkFont(size=11), text_color="#CCCCEE"
            ).pack(side="left")

    # ── Actions ───────────────────────────────────────────────────────────────

    def toggle_reveal(self):
        # In interview mode reveal just shows/hides the answer text overlay
        if self._panel_visible:
            self._panel_visible = False
            if hasattr(self, '_interview_answer_label'):
                self._interview_answer_label.configure(text="")
            if hasattr(self, '_reveal_btn'):
                self._reveal_btn.configure(border_width=0)
        else:
            self._panel_visible = True
            if hasattr(self, '_reveal_btn'):
                self._reveal_btn.configure(border_color="#D4A017", border_width=2)
            self._render_current_answer()

    def _set_screen_capture_visible(self, visible: bool):
        try:
            import ctypes
            WDA_NONE               = 0x00000000
            WDA_EXCLUDEFROMCAPTURE = 0x00000011
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id()) or self.winfo_id()
            affinity = WDA_NONE if visible else WDA_EXCLUDEFROMCAPTURE
            ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, affinity)
        except Exception:
            pass

    def _animate_width(self, from_w: int, to_w: int, steps: int = 8):
        geo = self.geometry()
        pos = geo.split("+", 1)[1] if "+" in geo else "0+0"
        x, y = pos.split("+")
        delta = (to_w - from_w) // steps

        def step(cur, remaining):
            if self._destroyed:
                return
            if remaining <= 0:
                self.geometry(f"{to_w}x{self.WINDOW_H}+{x}+{y}")
                return
            self.geometry(f"{cur + delta}x{self.WINDOW_H}+{x}+{y}")
            self.after(16, lambda: step(cur + delta, remaining - 1))

        step(from_w, steps)

    def toggle_interview(self):
        if self._running:
            self.stop_interview()
        else:
            self.start_interview()

    def start_interview(self):
        if not self.config_mgr.get("api_key"):
            self._set_status("No API key! Click ⚙ to add your Anthropic API key.", "#FF6B6B")
            return

        self._running = True
        self._answers = []
        self._answer_idx = -1
        self._analyze_count = 0
        self._panel_visible = True
        self.history_mgr.new_session()

        # Switch to interview view
        self._home_frame.grid_remove()
        self._interview_frame.grid(row=1, column=0, sticky="nsew")
        self._bottom_bar.grid(row=2, column=0, sticky="ew")
        self._show_interview_topbar()

        # Update listening label with profile
        profile = self.config_mgr.get("profile_type", "Job Interview")
        self._listen_label.configure(
            text=f"Hey, I'm listening to your {profile}...",
            text_color="#555577", slant="italic"
        )
        self._interview_answer_label.configure(text="")
        self._analyze_btn.configure(
            text=f"Analyze (0/{self._analyze_max})"
        )

        # Expand window
        self._animate_width(self.WINDOW_W, self.EXPANDED_W)
        self._set_screen_capture_visible(False)

        # Start timer and background threads
        self._start_timer()
        threading.Thread(target=self._capture_loop, daemon=True).start()
        self._audio_listener.start()
        self._process_queue()

    def stop_interview(self):
        self._running = False
        self._timer_running = False
        self._audio_listener.stop()

        # Switch back to home view
        self._interview_frame.grid_remove()
        self._bottom_bar.grid_remove()
        self._home_frame.grid(row=1, column=0, sticky="nsew")
        self._show_home_topbar()
        self._animate_width(self.EXPANDED_W, self.WINDOW_W)
        self._set_screen_capture_visible(True)

    def _go_home(self):
        if self._running:
            self.stop_interview()
        else:
            self._interview_frame.grid_remove()
            self._bottom_bar.grid_remove()
            self._home_frame.grid(row=1, column=0, sticky="nsew")
            self._show_home_topbar()
        self.answer_panel.clear()

    def _show_help(self):
        from tkinter import messagebox
        messagebox.showinfo(
            "Help",
            "Interview Cracker AI\n\n"
            "1. Click 'Start Interview' to begin listening.\n"
            "2. Speak or share your screen – AI answers automatically.\n"
            "3. Use Prev/Next to navigate answers.\n"
            "4. Type in the bottom bar and click Analyze to ask anything.\n"
            "5. Use ⚙ Settings to add your API key.\n\n"
            "Device ID is needed to activate your license.",
            parent=self
        )

    def _move_window(self, dx: int, dy: int):
        self.geometry(f"+{self.winfo_x() + dx}+{self.winfo_y() + dy}")

    def _toggle_visibility(self):
        if self.state() == "withdrawn":
            self.deiconify()
        else:
            self.withdraw()

    def _toggle_clickthrough(self):
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            GWL_EXSTYLE = -20
            WS_EX_TRANSPARENT = 0x00000020
            WS_EX_LAYERED     = 0x00080000
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if style & WS_EX_TRANSPARENT:
                ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style & ~WS_EX_TRANSPARENT)
                self._set_status("Click-through OFF", "#7986CB")
            else:
                ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_TRANSPARENT | WS_EX_LAYERED)
                self._set_status("Click-through ON", "#FFA726")
        except Exception:
            pass

    def _copy_last_answer(self):
        if self._answers:
            _, answer = self._answers[self._answer_idx] if self._answer_idx >= 0 else self._answers[-1]
            self.clipboard_clear()
            self.clipboard_append(answer)

    def _ask_next_step(self):
        if not self.config_mgr.get("api_key"):
            return
        try:
            self._screen_capture.capture_screen()
            self.claude.ask_async(
                "[Screenshot] What is the next step I should take?",
                callback=self._on_claude_result,
                system_prompt=self._build_system_prompt()
            )
        except Exception:
            pass

    def _toggle_main_theme(self):
        current = ctk.get_appearance_mode()
        if current == "Dark":
            ctk.set_appearance_mode("Light")
            if hasattr(self, '_main_theme_btn'):
                self._main_theme_btn.configure(text="🌙")
        else:
            ctk.set_appearance_mode("Dark")
            if hasattr(self, '_main_theme_btn'):
                self._main_theme_btn.configure(text="☀")

    def open_settings(self):
        CustomizeDialog(
            self, self.config_mgr,
            on_save=lambda key, model: (
                self.claude.update_key(key),
                setattr(self.claude, 'model', model)
            )
        )

    def open_history(self):
        ConversationHistoryDialog(self, self.history_mgr)

    def open_license(self):
        LicenseDialog(
            self, self.license_mgr,
            on_activate=lambda: self._set_status("License activated!", "#4CAF50")
        )

    def copy_device_id(self):
        self.clipboard_clear()
        self.clipboard_append(self.device_id)
        self._set_status("Device ID copied!", "#7986CB")
        self.after(2000, lambda: self._set_status(
            "Ready to start your AI assistant session", "#7986CB"
        ))

    def close_app(self):
        self._destroyed = True
        self._running = False
        self._timer_running = False
        try:
            self._audio_listener.stop()
        except Exception:
            pass
        self.destroy()

    # ── Interview Logic ───────────────────────────────────────────────────────

    def _capture_loop(self):
        capture = ScreenCapture()
        while self._running:
            try:
                text = capture.capture_and_extract()
                cleaned = self._clean_ocr_text(text)
                if cleaned and len(cleaned.strip()) > 30:
                    self._text_queue.put(("screen", cleaned))
            except Exception as e:
                self._text_queue.put(("error", None, str(e)))
            time.sleep(4)

    def _clean_ocr_text(self, text: str) -> str:
        if not text:
            return ""
        lines = [l.strip() for l in text.splitlines() if len(l.strip()) > 15]
        return "\n".join(lines[:30])

    def _is_duplicate(self, text: str) -> bool:
        h = hashlib.md5(text.strip().lower().encode()).hexdigest()
        if h == self._last_text_hash:
            return True
        self._last_text_hash = h
        return False

    def _process_queue(self):
        if self._destroyed:
            return
        try:
            while not self._text_queue.empty():
                item = self._text_queue.get_nowait()
                kind = item[0]

                if kind == "error":
                    continue

                text = item[1]
                if not text or self._is_duplicate(text):
                    continue

                self._last_question = text
                preview = text.strip()[:60].replace("\n", " ")

                if hasattr(self, '_listen_label'):
                    self._listen_label.configure(
                        text=f"Analyzing: {preview}...",
                        text_color="#FFA726", slant="italic"
                    )

                self.claude.ask_async(
                    text,
                    callback=self._on_claude_result,
                    system_prompt=self._build_system_prompt(text)
                )

        except queue.Empty:
            pass
        finally:
            if self._running and not self._destroyed:
                self.after(500, self._process_queue)

    def _on_claude_result(self, result: tuple):
        if self._destroyed:
            return
        kind = result[0]
        if kind == "answer":
            _, question, answer = result
            self.after(0, lambda: self._show_answer(question, answer))
        elif kind == "error":
            _, question, error = result
            self.after(0, lambda: self._show_error(error))

    def _show_answer(self, question: str, answer: str):
        self._answers.append((question, answer))
        self._answer_idx = len(self._answers) - 1
        self.history_mgr.add_message(question, answer)
        self.answer_panel.show_answer(question, answer)

        # Update interview content area
        q_preview = question.strip()[:80].replace("\n", " ")
        if hasattr(self, '_listen_label'):
            self._listen_label.configure(
                text=f"Q: {q_preview}{'...' if len(question) > 80 else ''}",
                text_color="#7986CB", slant="roman"
            )
        if hasattr(self, '_interview_answer_label'):
            self._interview_answer_label.configure(text=answer)

        self._update_counter()

    def _show_error(self, error: str):
        if hasattr(self, '_listen_label'):
            self._listen_label.configure(
                text=f"Error: {error[:80]}",
                text_color="#FF6B6B", slant="roman"
            )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _build_system_prompt(self, question: str = "") -> str:
        role = self.config_mgr.get("profile_type", "Job Interview")
        resume = self.config_mgr.get("resume_context", "").strip()
        custom = self.config_mgr.get("custom_instructions", "").strip()
        language = self.config_mgr.get("response_language", "English")

        prompt = SYSTEM_PROMPT
        prompt += f"\n\nInterview type: {role}."
        if resume:
            prompt += f"\n\nCandidate context (resume / job description):\n{resume}"
        if custom:
            prompt += f"\n\nAdditional instructions: {custom}"
        if language and language != "English":
            prompt += f"\n\nRespond in {language}."

        prompt = prompt.replace("{role}", role)
        prompt = prompt.replace("{resume}", resume)
        prompt = prompt.replace("{question}", question)
        prompt = prompt.replace("{language}", language)

        return prompt

    def _set_status(self, message: str, color: str = "#7986CB"):
        if not self._destroyed and hasattr(self, '_status_label'):
            try:
                self._status_label.configure(text=message, text_color=color)
            except Exception:
                pass


        return prompt

    def _set_status(self, message: str, color: str = "#7986CB"):
        if not self._destroyed:
            self._status_label.configure(text=message, text_color=color)


# ─── Entry Point ──────────────────────────────────────────────────────────────

def main():
    app = MainWindow()
    app.mainloop()


if __name__ == "__main__":
    main()
