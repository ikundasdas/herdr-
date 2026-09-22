# -*- coding: utf-8 -*-
"""
pet_render.py — herdr desktop-pet GUI renderer.

Floating "Codex pet"-style companion. Pure stdlib + tkinter (Tk 8.6 loads
PNG natively in PhotoImage — no Pillow at runtime).

Windows: transparent surround via -transparentcolor magic-pixel trick.
Borderless + overrideredirect => no taskbar button, but fully mouse-
clickable (clicks on magic-color pixels fall through to whatever is below,
clicks on the sprite hit the pet).

Run demo:      pythonw pet_render.py
Run selfcheck: python pet_render.py --selfcheck
"""

import json
import os
import random
import sys
import threading
import time

import tkinter as tk
import tkinter.font as tkfont

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ASSET_DIR = os.path.join(BASE_DIR, "assets")
FRAMES_DIR = os.path.join(ASSET_DIR, "frames")
META_PATH = os.path.join(ASSET_DIR, "frames_meta.json")

# Magic transparency color. Chosen to never appear in the codex palette
# (periwinkle #6E9BFA body / navy outline) or in bubble/panel chrome.
MAGIC = "#fe00fe"

# Preferred scale first: native (192x208) default, m96 (96x104),
# m72 (72x78), m54 fallback.
SCALE_DIRS = ["native", "m96", "m72", "m54"]
# nominal frame height for each preset (files: m54=54x58, m72=72x78, ...). Kept
# as a map so legacy config values still resolve; runtime loads _resolve_scale().
SCALE_FRAME_H = {"m40": 44, "m54": 58, "m72": 78, "m96": 104, "native": 208}

# pet STATE (from main.py aggregate) -> sprite animation name (frames_meta).
# NOTE the naming seam: the aggregate/status word is "done" but the clip row
# is authored as "review" (row8: ✓ check-flag + peek at the stopped wheel).
STATE_ANIM = {
    "idle": "idle",        # row0 = hamster ASLEEP (v3 skin)
    "working": "running",  # row7 = wheel sprint, sustained while any pane works
    "blocked": "waiting",  # row6 = startled beside the wheel, sustained
    "done": "review",      # row8 = task ready, SUSTAINED until the pane is read
    "asleep": "failed",    # row5 = deeper sleep (offline policy) + zZ bubbles
}

# idle "micro life": every 40-90s the pet plays one of these one-shot clips and
# then hands straight back to its state row. None of these rows is mapped to a
# state, so they are pure personality and cost nothing when unused.
QUIRKS = ("jump", "waving", "walk_right", "walk_left")

BUBBLE_ACCENT = {
    "info": "#8b95a5",
    "done": "#2e9e5b",
    "blocked": "#d9902e",
    "working": "#5b7ea6",
}

STATUS_GLYPH = {"done": "✅", "blocked": "⏸", "working": "▶", "idle": "💤"}
STATUS_ZH = {"idle": "空闲", "working": "思考中", "blocked": "待确认", "done": "已完成"}
STATUS_ACCENT = {
    "idle": "#8b95a5",
    "working": "#5b7ea6",
    "blocked": "#d9902e",
    "done": "#2e9e5b",
}

PAPER = "#fffdf4"       # retro paper fill for bubble/panel
INK = "#2b2b33"         # outline / text navy

# fallback faces when PNG assets are missing — pipeline must never crash
FALLBACK_FACE = {
    "idle": "◕‿◕", "working": "◕ᗜ◕", "blocked": "◕﹏◕",
    "done": "＾▽＾", "asleep": "－﹣－",
}


def _pick_font(size=10):
    """Monospace-ish, CJK-capable font chain for the retro pixel vibe."""
    try:
        fams = set(tkfont.families())
    except Exception:
        fams = set()
    for fam in ("SimSun", "MS Gothic", "Microsoft YaHei UI", "Tahoma"):
        if fam in fams:
            return (fam, size)
    return ("TkFixedFont", size)


def _work_area():
    """(left, top, right, bottom) of the desktop excluding the taskbar."""
    try:
        import ctypes

        class RECT(ctypes.Structure):
            _fields_ = [
                ("l", ctypes.c_long), ("t", ctypes.c_long),
                ("r", ctypes.c_long), ("b", ctypes.c_long),
            ]

        r = RECT()
        SPI_GETWORKAREA = 0x0030
        if ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(r), 0):
            return r.l, r.t, r.r, r.b
    except Exception:
        pass
    return None


def _enable_dpi_awareness():
    """Per-monitor DPI awareness so Win 125/150% does not bitmap-stretch the sprite."""
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
            return
        except Exception:
            pass
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    except Exception:
        pass


def _resolve_scale(scale):
    """config scale -> preset dir name. Prefers config.json scale key; illegal -> m72."""
    if scale is None:
        try:
            base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
            with open(os.path.join(base, "herdr-desktop-pet", "config.json"),
                      "r", encoding="utf-8") as f:
                scale = json.load(f).get("scale")
        except Exception:
            scale = None
    if isinstance(scale, str) and scale in SCALE_DIRS:
        return scale
    if isinstance(scale, (int, float)) and not isinstance(scale, bool):
        h = float(scale)
        return min(SCALE_DIRS, key=lambda k: abs(SCALE_FRAME_H[k] - h))
    return "m72"


class PetApp:
    """All visual truth of the desktop pet. main.py only talks to this API."""

    def __init__(self, config: dict):
        _enable_dpi_awareness()
        self.config = config if isinstance(config, dict) else {}

        self.root = tk.Tk()
        self.root.withdraw()                    # hidden until run()
        self.root.overrideredirect(True)
        try:
            self.root.attributes("-transparentcolor", MAGIC)
        except tk.TclError:
            pass                                # fall back to solid surround

        self.scale_dir = _resolve_scale(self.config.get("scale", "m72"))
        self.reduced = bool(self.config.get("reduced_motion", False))

        self.state = "idle"
        self.summary = []
        self.reminders_paused = False           # internal; main.py unaware
        self._drag_cb = None
        self._activate_cb = None
        self._shown = False
        self._entry_pending = False

        self._anim = None          # [(frame_idx, ms), ...]
        self._anim_i = 0
        self._anim_cycles = 0      # 0 = infinite
        self._anim_end = None
        self._anim_job = None
        self._idle_job = None
        self._zz_job = None
        self._bubble_job = None
        self._bubble_win = None
        self._panel_win = None
        self._hover_until_hide = None
        self._show_hooks = []
        # v4: persistent status badge (chip under the pet) + DSH web control
        self._badge_win = None
        self._badge_text = ""
        self._dsh_web_index = None

        self.anims = self._load_meta()
        self.photos = {}
        self.assets_ok = self._load_frames(self.scale_dir)
        self._font = _pick_font(10)
        self._font_small = _pick_font(9)

        self._build_canvas()
        self._bind_interactions()

    # ------------------------------------------------------------------ meta
    def _load_meta(self):
        try:
            with open(META_PATH, "r", encoding="utf-8") as f:
                meta = json.load(f)
            return {k: [(int(i), int(ms)) for i, ms in v]
                    for k, v in meta.get("animations", {}).items()}
        except Exception:
            return {}

    def _load_frames(self, scale_dir):
        """Preload all 72 frames of one scale into PhotoImages. Never load
        PNGs during animation. Returns False if assets unusable."""
        d = os.path.join(FRAMES_DIR, scale_dir)
        photos = {}
        try:
            for i in range(72):
                p = os.path.join(d, "frame_%02d.png" % i)
                photos[i] = tk.PhotoImage(file=p)
        except Exception:
            return False
        self.photos = photos
        self.assets_ok = True
        return True

    # ---------------------------------------------------------------- canvas
    def _frame_size(self):
        if self.assets_ok and 0 in self.photos:
            im = self.photos[0]
            return im.width(), im.height()
        h = SCALE_FRAME_H.get(self.scale_dir, 104)
        return int(h * 192 / 208), h

    def _build_canvas(self):
        w, h = self._frame_size()
        self.pet_w, self.pet_h = w, h
        self.cv = tk.Canvas(self.root, width=w, height=h,
                            bg=MAGIC, highlightthickness=0, bd=0)
        self.cv.pack(fill="both", expand=True)
        self._blob = None
        if self.assets_ok:
            self._img_item = self.cv.create_image(0, 0, anchor="nw",
                                                  image=self.photos[0])
        else:
            self._img_item = None
            self._draw_fallback()

    def _draw_fallback(self):
        """Missing assets: simple drawn circle + face so nothing hard-crashes."""
        self.cv.delete("blob")
        w, h = self.pet_w, self.pet_h
        r = min(w, h) * 0.36
        cx, cy = w // 2, int(h * 0.58)
        self.cv.create_oval(cx - r, cy - r, cx + r, cy + r,
                            fill="#6e9bfa", outline="#2b3557", width=3, tags="blob")
        face = FALLBACK_FACE.get(self.state, "◕‿◕")
        self.cv.create_text(cx, cy, text=face, fill="#2b3557",
                            font=self._font_small, tags="blob")

    def _show_frame_idx(self, idx):
        if self.assets_ok and idx in self.photos:
            self.cv.itemconfig(self._img_item, image=self.photos[idx])
        else:
            self._draw_fallback()

    # ------------------------------------------------------------ animation
    def _play(self, anim_name, cycles, on_end=None):
        seq = self.anims.get(anim_name)
        if not seq:
            seq = [(0, 900)]  # degenerate: hold frame 0
        if self._anim_job:
            self.root.after_cancel(self._anim_job)
            self._anim_job = None
        self._anim = seq
        self._anim_i = 0
        self._anim_cycles = cycles
        self._anim_end = on_end
        self._show_frame_idx(seq[0][0])
        self._schedule_next()

    def _schedule_next(self):
        if self.reduced or not self._anim:
            return
        idx, ms = self._anim[self._anim_i]
        self._anim_job = self.root.after(ms, self._anim_tick)

    def _anim_tick(self):
        self._anim_job = None
        if self.reduced or not self._anim:
            return
        n = len(self._anim)
        self._anim_i = (self._anim_i + 1) % n
        if self._anim_i == 0 and self._anim_cycles > 0:
            self._anim_cycles -= 1
            if self._anim_cycles == 0:
                end, self._anim_end = self._anim_end, None
                if end:
                    end()
                    if self._anim_job:      # end() started a new clip
                        return
                # exhausted without replacement -> keep looping last clip
                self._anim_cycles = 0
        self._show_frame_idx(self._anim[self._anim_i][0])
        self._schedule_next()

    def _play_for_state(self, state):
        # v3.2 policy: EVERY mapped state is sustained — it loops its own row
        # for as long as main.py holds it. done(row8 "task ready") is now one
        # too: aggregate() persists 'done' until herdr flips the pane to idle
        # when the user actually views it, so the celebration holds (a polite
        # nag) instead of the old 3-cycle handoff that fell back to sleep.
        self._play(STATE_ANIM.get(state, "idle"), 0)

    def _hand_to_current(self):
        """Hand-off target for transient clips (entrance waving, one-shot
        quirks): resume the CURRENT aggregate state's row, never sleep."""
        self._play_for_state(self.state)

    def _static_for_state(self):
        anim = STATE_ANIM.get(self.state, "idle")
        seq = self.anims.get(anim) or [(0, 900)]
        self._show_frame_idx(seq[0][0])

    # ------------------------------------------------------------ public API
    def set_state(self, state: str) -> None:
        state = state if state in STATE_ANIM else "idle"
        if state == self.state:
            return
        self.state = state
        self._update_badge()
        if self.reduced:
            self._static_for_state()
            return
        if self._entry_pending:
            return  # entrance clip will apply the state when it finishes
        self._play_for_state(state)
        self._schedule_zz()

    def bubble(self, text: str, kind: str, ms: int = 6000) -> None:
        # last bubble wins: replace whatever is on screen
        if self.reminders_paused or not text:
            return
        self._render_bubble(text, kind if kind in BUBBLE_ACCENT else "info", ms)

    def set_summary(self, panes: list) -> None:
        self.summary = list(panes) if panes else []
        self._update_badge()

    def on_drag_end(self, cb) -> None:
        self._drag_cb = cb

    def on_activate(self, cb) -> None:
        self._activate_cb = cb

    def post(self, fn, *args) -> None:
        self.root.after(0, lambda: fn(*args))

    def when_shown(self, fn) -> None:
        """Extension point used by the demo: call fn once the pet is visible."""
        if self._shown:
            self.root.after(50, fn)
        else:
            getattr(self, "_show_hooks").append(fn)

    # ------------------------------------------------------------------- run
    def run(self) -> None:
        self._show_window()
        self.root.mainloop()

    def _show_window(self):
        if self._shown:
            return
        self._shown = True
        w, h = self.pet_w, self.pet_h

        pos = self.config.get("position")
        if (isinstance(pos, (list, tuple)) and len(pos) == 2
                and all(isinstance(v, (int, float)) for v in pos)):
            x, y = int(pos[0]), int(pos[1])
        else:
            wa = _work_area()
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            right = wa[2] if wa else sw
            bottom = wa[3] if wa else sh - 48   # guess taskbar height
            x, y = right - w - 24, bottom - h - 24
        self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))
        self.root.attributes("-topmost", True)
        self.root.deiconify()
        self.root.lift()

        if self.reduced:
            self._static_for_state()
        else:
            # entrance: 2 cycles of row3 waving, then settle per state
            self._entry_pending = True
            self._play("waving", 2, on_end=self._entrance_done)
        self._schedule_micro_life()
        self._schedule_zz()
        self._update_badge()
        for hook in getattr(self, "_show_hooks", []):
            self.root.after(60, hook)

    def _entrance_done(self):
        self._entry_pending = False
        self._hand_to_current()   # transient clip finished -> resume live state

    # ------------------------------------------------------------- micro life
    def _schedule_micro_life(self):
        if self._idle_job:
            self.root.after_cancel(self._idle_job)
        delay = random.randint(40000, 90000)
        self._idle_job = self.root.after(delay, self._micro_life)

    def _micro_life(self):
        self._idle_job = None
        # Spontaneous idle quirk: hop / greet / look around, then resume the
        # state row. Only fires while genuinely idle — never mid-task, asleep,
        # paused, being dragged or under a bubble — so it can never fight the
        # state clip or the selfcheck probes.
        if (self.state == "idle" and not self.reduced
                and not self.reminders_paused and not self._entry_pending
                and not hasattr(self, "_drag") and not self._bubble_active()
                and self.anims.get("jump")):
            self._play(random.choice(QUIRKS), 1, on_end=self._hand_to_current)
        self._schedule_micro_life()

    # ------------------------------------------------------------ asleep zZ
    def _schedule_zz(self):
        if self._zz_job:
            self.root.after_cancel(self._zz_job)
        self._zz_job = self.root.after(9000, self._zz_tick)

    def _zz_tick(self):
        self._zz_job = None
        if self.state == "asleep" and not self.reminders_paused \
                and not self._bubble_active():
            self._render_bubble("💤 z z Z …", "info", 5000)
        self._schedule_zz()

    # ---------------------------------------------------------------- bubbles
    def _bubble_active(self):
        return self._bubble_win is not None and self._bubble_win.winfo_exists()

    def _render_bubble(self, text, kind, ms):
        accent = BUBBLE_ACCENT[kind]
        # Order matters: _kill_bubble re-shows the badge at its tail, so the
        # incoming bubble must be hidden AFTER the old one is gone.
        self._kill_bubble()
        self._hide_badge()          # a bubble owns the screen; chip steps aside
        win = tk.Toplevel(self.root)
        self._bubble_win = win
        win.overrideredirect(True)
        win.attributes("-topmost", True)

        cv = tk.Canvas(win, bg=PAPER, highlightthickness=0, bd=0)
        cv.pack(fill="both", expand=True)
        f = tkfont.Font(font=self._font)
        tw = f.measure(text)
        pad_x, pad_y, tail_h = 10, 7, 12
        bw, bh = tw + 2 * pad_x, f.metrics("linespace") + 2 * pad_y
        win_w, win_h = bw, bh + tail_h
        cv.config(width=win_w, height=win_h)

        # hard 2px pixel border, no rounded corners, plus stepped tail
        cv.create_rectangle(0, 0, bw, bh, fill=accent, outline=accent)
        cv.create_rectangle(2, 2, bw - 2, bh - 2, fill=PAPER, outline=PAPER)
        for i, (tx, ty, ts) in enumerate([(bw - 30, bh, 14), (bw - 24, bh + 6, 10)]):
            cv.create_rectangle(tx, ty, tx + ts, ty + 6, fill=accent, outline=accent)
            cv.create_rectangle(tx + 2, ty + 2, tx + ts - 2, ty + 6,
                                fill=PAPER, outline=PAPER)
        cv.create_text(pad_x, bh // 2, anchor="w", text=text,
                       fill=INK, font=self._font)
        cv.bind("<Button-1>", lambda e: self._activate())

        x, y = self._clamp(win_w, win_h,
                           self.root.winfo_x() - bw + 34,
                           self.root.winfo_y() - win_h - 4)
        win.geometry("%dx%d+%d+%d" % (win_w, win_h, x, y))
        self._bubble_job = self.root.after(ms, self._kill_bubble)

    def _kill_bubble(self):
        if self._bubble_job:
            self.root.after_cancel(self._bubble_job)
            self._bubble_job = None
        if self._bubble_win is not None:
            try:
                self._bubble_win.destroy()
            except tk.TclError:
                pass
            self._bubble_win = None
        self._update_badge()        # bubble gone -> the status chip comes back

    def _clamp(self, w, h, x, y):
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        return max(0, min(x, sw - w)), max(0, min(y, sh - h))

    # ----------------------------------------------------------- hover panel
    def _show_panel(self):
        if self._hover_until_hide:
            self.root.after_cancel(self._hover_until_hide)
            self._hover_until_hide = None
        if self._panel_win is not None and self._panel_win.winfo_exists():
            return
        win = tk.Toplevel(self.root)
        self._panel_win = win
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        cv = tk.Canvas(win, bg=PAPER, highlightthickness=0, bd=0)
        cv.pack(fill="both", expand=True)

        f = tkfont.Font(font=self._font_small)
        rows = []
        if not self.summary:
            rows = [("·", "#8b95a5", "暂无任务面板")]
        for pane in self.summary[:8]:
            status = str(pane.get("status", "idle"))
            glyph = STATUS_GLYPH.get(status, "·")
            zh = STATUS_ZH.get(status, status)
            name = str(pane.get("name", "") or pane.get("pane_id", ""))
            if len(name) > 14:
                name = name[:13] + "…"
            ws = str(pane.get("workspace_id", "") or "")
            if len(ws) > 8:
                ws = ws[:8] + "…"
            line = "%s %s  %s%s" % (zh, "·", (ws + " ") if ws else "", name)
            rows.append((glyph, STATUS_ACCENT.get(status, INK), line))

        pad = 6
        widths = [f.measure(g + " ") + f.measure(t) for g, _, t in rows]
        pw = max(widths) + 2 * pad + 2
        lh = f.metrics("linespace")
        ph = len(rows) * lh + 2 * pad
        cv.config(width=pw, height=ph)
        cv.create_rectangle(0, 0, pw, ph, fill=INK, outline=INK)
        cv.create_rectangle(2, 2, pw - 2, ph - 2, fill=PAPER, outline=PAPER)
        for i, (glyph, color, line) in enumerate(rows):
            yy = pad + i * lh
            cv.create_text(pad, yy, anchor="nw", text=glyph + " ",
                           fill=color, font=self._font_small)
            cv.create_text(pad + f.measure(glyph + " "), yy, anchor="nw",
                           text=line, fill=INK, font=self._font_small)

        x, y = self._clamp(pw, ph, self.root.winfo_x(),
                           self.root.winfo_y() - ph - 4)
        win.geometry("%dx%d+%d+%d" % (pw, ph, x, y))
        win.bind("<Leave>", lambda e: self._hide_panel())

    def _hide_panel(self, delay=120):
        if self._hover_until_hide:
            self.root.after_cancel(self._hover_until_hide)
            self._hover_until_hide = None
        if self._panel_win is None:
            return
        if not self._panel_win.winfo_exists():
            self._panel_win = None
            return
        self._hover_until_hide = self.root.after(delay, self._panel_destroy)

    def _panel_destroy(self):
        self._hover_until_hide = None
        if self._panel_win is not None:
            try:
                if self._panel_win.winfo_exists():
                    self._panel_win.destroy()
            except tk.TclError:
                pass
            self._panel_win = None

    # ----------------------------------------------------------- interactions
    def _bind_interactions(self):
        self.cv.bind("<ButtonPress-1>", self._drag_press)
        self.cv.bind("<B1-Motion>", self._drag_motion)
        self.cv.bind("<ButtonRelease-1>", self._drag_release)
        self.cv.bind("<Double-Button-1>", lambda e: self._activate())
        self.cv.bind("<Button-3>", self._show_menu)
        self.cv.bind("<Enter>", lambda e: self._show_panel())
        self.cv.bind("<Leave>", lambda e: self._hide_panel())
        self._menu = tk.Menu(self.root, tearoff=0)
        self._menu.add_command(label="暂停提醒", command=self._toggle_pause)
        self._menu.add_separator()
        self._menu.add_command(label="打开 DSH Web", command=self._open_dsh_web)
        self._menu.add_command(label="启动 DSH Web", command=self._toggle_dsh_web)
        self._dsh_web_index = self._menu.index("end")
        self._menu.add_separator()
        self._menu.add_command(label="退出", command=lambda: sys.exit(0))

    def _drag_press(self, e):
        self._drag = (e.x_root, e.y_root, self.root.winfo_x(), self.root.winfo_y(), 0)

    def _drag_motion(self, e):
        if not hasattr(self, "_drag"):
            return
        px, py, ox, oy, moved = self._drag
        dx, dy = e.x_root - px, e.y_root - py
        moved = max(moved, abs(dx), abs(dy))
        self._drag = (px, py, ox, oy, moved)
        self.root.geometry("+%d+%d" % (ox + dx, oy + dy))
        self._place_badge()         # the status chip rides along while dragging
        if self._bubble_win is not None and self._bubble_win.winfo_exists():
            # keep bubble parked relative to the pet while dragging
            pass

    def _drag_release(self, e):
        if not hasattr(self, "_drag"):
            return
        moved = self._drag[4]
        del self._drag
        if moved > 3 and self._drag_cb:
            self._drag_cb(self.root.winfo_x(), self.root.winfo_y())

    def _activate(self):
        if self._activate_cb:
            self._activate_cb()

    def _toggle_pause(self):
        self.reminders_paused = not self.reminders_paused
        if self.reminders_paused:
            self._kill_bubble()
        self._menu.entryconfig(0, label="恢复提醒" if self.reminders_paused else "暂停提醒")

    def _show_menu(self, e):
        try:
            # live label: 启动/停止 reflects the real state right now
            try:
                import dsh_ctl
                running = bool(dsh_ctl.web_pids()) or dsh_ctl.is_running()
                label = ("■ 停止 DSH Web" if running else "▶ 启动 DSH Web")
                if self._dsh_web_index is not None:
                    self._menu.entryconfig(self._dsh_web_index, label=label)
            except Exception:
                pass
            self._menu.tk_popup(e.x_root, e.y_root)
        finally:
            self._menu.grab_release()

    # -------------------------------------------------- persistent status badge
    def _update_badge(self):
        """Sustained chip under the pet: current aggregate status + top task."""
        try:
            if self._bubble_active():
                return  # a bubble owns the screen; the chip waits for it
            if self.state == "asleep":
                self._hide_badge()
                return
            pane = self._top_pane()
            name = str(pane.get("name", "") or "") if pane else ""
            if len(name) > 12:
                name = name[:11] + "…"
            zh = STATUS_ZH.get(self.state, self.state)
            if self.state in ("working", "blocked", "done") and name:
                text = "%s · %s" % (zh, name)
            else:
                text = zh
            if (text == self._badge_text and self._badge_win is not None
                    and self._badge_win.winfo_exists()):
                return
            self._badge_text = text
            self._hide_badge()
            self._render_badge(text, STATUS_ACCENT.get(self.state, INK))
        except tk.TclError:
            pass

    def _top_pane(self):
        """Highest-priority pane in the summary (blocked > working > done)."""
        for wanted in ("blocked", "working", "done"):
            for pane in self.summary:
                if pane.get("status") == wanted:
                    return pane
        return self.summary[0] if self.summary else None

    def _render_badge(self, text, accent):
        win = tk.Toplevel(self.root)
        self._badge_win = win
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        cv = tk.Canvas(win, bg=PAPER, highlightthickness=0, bd=0)
        cv.pack(fill="both", expand=True)
        f = tkfont.Font(font=self._font_small)
        tw = f.measure(text)
        pad_x, pad_y, dot = 6, 3, 8
        bw = tw + 2 * pad_x + dot + 6
        bh = f.metrics("linespace") + 2 * pad_y
        cv.config(width=bw, height=bh)
        cv.create_rectangle(0, 0, bw, bh, fill=INK, outline=INK)
        cv.create_rectangle(2, 2, bw - 2, bh - 2, fill=PAPER, outline=PAPER)
        cv.create_oval(pad_x + 1, bh // 2 - dot // 2, pad_x + 1 + dot,
                       bh // 2 + dot // 2, fill=accent, outline=accent)
        cv.create_text(pad_x + dot + 6, bh // 2, anchor="w",
                       text=text, fill=INK, font=self._font_small)
        cv.bind("<Button-1>", lambda e: self._activate())  # chip click focuses
        self._place_badge()

    def _place_badge(self):
        """Park the chip centered under the pet; follow it while dragging."""
        if self._badge_win is None or not self._badge_win.winfo_exists():
            return
        try:
            self.root.update_idletasks()
            w = self._badge_win.winfo_reqwidth()
            h = self._badge_win.winfo_reqheight()
        except tk.TclError:
            return
        x = self.root.winfo_x() + (self.pet_w - w) // 2
        y = self.root.winfo_y() + self.pet_h + 4
        x, y = self._clamp(w, h, x, y)
        self._badge_win.geometry("%dx%d+%d+%d" % (w, h, x, y))

    def _hide_badge(self):
        if self._badge_win is not None:
            try:
                self._badge_win.destroy()
            except tk.TclError:
                pass
            self._badge_win = None

    # ------------------------------------------------- DSH web one-click control
    def _toggle_dsh_web(self):
        def run():
            try:
                import dsh_ctl
                if dsh_ctl.web_pids() or dsh_ctl.is_running():
                    ok, msg = dsh_ctl.stop_web()
                else:
                    ok, msg = dsh_ctl.start_web()
                kind = "done" if ok else "blocked"
                self.root.after(0, lambda: self.bubble(msg, kind, 7000))
            except Exception as error:
                self.root.after(0, lambda: self.bubble(
                    "DSH Web: %r" % (error,), "blocked", 7000))

        threading.Thread(target=run, daemon=True).start()

    def _open_dsh_web(self):
        def run():
            try:
                import dsh_ctl
                ok, msg = True, ""
                if not (dsh_ctl.web_pids() or dsh_ctl.is_running()):
                    ok, msg = dsh_ctl.start_web()
                if ok:
                    try:
                        os.startfile(dsh_ctl.DSH_WEB_URL)
                        msg = "已打开 DSH Web → %s" % dsh_ctl.DSH_WEB_URL
                    except OSError:
                        ok, msg = False, "打开浏览器失败，请手动访问 %s" % dsh_ctl.DSH_WEB_URL
                kind = "done" if ok else "blocked"
                self.root.after(0, lambda: self.bubble(msg, kind, 7000))
            except Exception as error:
                self.root.after(0, lambda: self.bubble(
                    "DSH Web: %r" % (error,), "blocked", 7000))

        threading.Thread(target=run, daemon=True).start()

    # ------------------------------------------------------------- teardown
    def destroy(self):
        self._hide_badge()
        self._kill_bubble()
        try:
            self.root.destroy()
        except tk.TclError:
            pass


# ------------------------------------------------------------------ selfcheck
def _selfcheck():
    """Headless-ish validation: build window, verify assets + bubble,
    tick the tk loop ~3s via update(), assert, destroy."""
    print("[selfcheck] python", sys.version.split()[0])
    t0 = time.time()
    failures = []

    # every scale dir must hold 72 frames
    for d in SCALE_DIRS:
        p = os.path.join(FRAMES_DIR, d)
        n = len([f for f in os.listdir(p) if f.endswith(".png")]) if os.path.isdir(p) else -1
        print("[selfcheck] dir %-7s frames=%d" % (d, n))
        if n != 72:
            failures.append("dir %s has %d pngs (want 72)" % (d, n))

    app = PetApp({"scale": "m54"})
    if not app.assets_ok:
        failures.append("asset load fallback engaged (files missing?)")
    if len(app.photos) != 72:
        failures.append("loaded %d PhotoImages (want 72)" % len(app.photos))
    print("[selfcheck] PhotoImages loaded: %d  assets_ok=%s" % (len(app.photos), app.assets_ok))
    if "review" not in app.anims or len(app.anims.get("idle", [])) != 6:
        failures.append("frames_meta animations parsed wrong: %r" % list(app.anims))

    app._show_window()
    # Keep the probes deterministic: cancel the random 40-90s idle quirk here
    # and exercise it explicitly near the end instead.
    if app._idle_job:
        app.root.after_cancel(app._idle_job)
        app._idle_job = None
    app.set_state("working")
    app.set_summary([
        {"pane_id": "p1", "name": "vibe-dashboard", "status": "working", "workspace_id": "ws1"},
        {"pane_id": "p2", "name": "长名字的任务面板需要截断处理验证", "status": "blocked", "workspace_id": "ws2"},
    ])
    app.bubble("▶ 「demo」 开始干活", "working", 1200)
    # tick past the bubble's ms to prove the auto-dismiss timer works ...
    end = time.time() + 1.8
    while time.time() < end:
        app.root.update()
        time.sleep(0.01)
    if app._bubble_active():
        failures.append("bubble did not auto-dismiss after its ms")
    # ... then show one that must still be alive during the ~3s tick below
    app.bubble("✅ 「demo-task-x」 已完成", "done", 8000)
    if not app._bubble_active():
        failures.append("bubble Toplevel failed to create")

    seen_frames = set()
    end = time.time() + 3.0
    while time.time() < end:
        app.root.update()
        if app._anim:
            seen_frames.add(app._anim[app._anim_i][0])
        time.sleep(0.01)
    print("[selfcheck] distinct frames displayed while ticking: %s" % sorted(seen_frames))
    if len(seen_frames) < 2:
        failures.append("animation did not advance frames")
    if not app._bubble_active():
        failures.append("bubble Toplevel vanished too early?")
    else:
        print("[selfcheck] bubble Toplevel alive: OK")
    app.post(lambda: None)          # marshal path smoke test

    # single fixed scale: m54 must stay m54 (no cycle)
    if app.config.get("scale") != "m54":
        failures.append("scale not fixed to m54: %r" % app.config.get("scale"))
    app._toggle_pause()
    app.bubble("should be suppressed", "info", 500)
    if app._bubble_active():
        failures.append("paused reminders still rendered")
    print("[selfcheck] scale fixed -> m54, pause -> suppressed OK")

    # ---- v3 regression probes: sustained states must never hand to sleep ----
    def sample(seconds):
        """Tick the tk loop for `seconds`, sampling the displayed frame index
        once a second. Returns the per-second trace."""
        tr, t0, last = [], time.time(), -1.0
        end = t0 + float(seconds)
        while time.time() < end:
            app.root.update()
            time.sleep(0.02)
            if app._anim and time.time() - t0 - last >= 1.0:
                last = time.time() - t0
                tr.append((int(last), app._anim[app._anim_i][0]))
        return tr

    def row_of(idx):
        return idx // 8

    # ---- sustained states must loop their own row and never hand to sleep --
    app._toggle_pause()                     # re-arm bubbles for set_state path
    for state, row, anim_name, secs in (("working", 7, "running", 26),
                                        ("blocked", 6, "waiting", 8),
                                        ("done", 8, "review", 26)):
        app.set_state(state)
        tr = sample(secs)
        got = sorted({i for _, i in tr})
        print("[selfcheck] %-7s-probe %ds @1s: %s"
              % (state, secs, " ".join("t%d:f%d" % s for s in tr)))
        bad = [i for i in got if row_of(i) != row]
        if bad or app._anim is not app.anims.get(anim_name):
            failures.append("%s did not SUSTAIN row%d (saw %s)" % (state, row, got))

    # ---- v4 probes: persistent badge + DSH web menu -------------------------
    if getattr(app, "_dsh_web_index", None) is None:
        failures.append("DSH web menu item missing")
    app._update_badge()                     # active state -> chip appears
    if app._badge_win is None or not app._badge_win.winfo_exists():
        failures.append("badge not shown while a state is active")
    app.bubble("▶ 「badge-probe」 开始干活", "working", 8000)
    if app._badge_win is not None and app._badge_win.winfo_exists():
        failures.append("badge should hide while a bubble owns the screen")
    app._kill_bubble()
    app._update_badge()                     # bubble died -> chip returns
    if app._badge_win is None or not app._badge_win.winfo_exists():
        failures.append("badge did not return after the bubble died")
    app.set_state("asleep")
    app._update_badge()
    if app._badge_win is not None and app._badge_win.winfo_exists():
        failures.append("badge should hide while the pet is asleep")
    print("[selfcheck] badge/menu probes: OK")

    app.set_state("idle")                   # aggregate really idle -> sleep OK
    iseen = set()
    end = time.time() + 2.0
    while time.time() < end:
        app.root.update()
        time.sleep(0.02)
        if app._anim:
            iseen.add(app._anim[app._anim_i][0])
    print("[selfcheck] idle-probe frames: %s" % sorted(iseen))
    if any(not 0 <= i <= 7 for i in iseen):
        failures.append("idle not on row0: %s" % sorted(iseen))

    # ---- v5 probe: the idle micro-life quirk plays, then hands back to row0 --
    app._micro_life()                       # fire one quirk deterministically
    q_seen = set()
    end = time.time() + 1.0
    while time.time() < end:
        app.root.update()
        time.sleep(0.02)
        if app._anim:
            q_seen.add(app._anim[app._anim_i][0])
    print("[selfcheck] micro-life quirk frames: %s (rows %s)"
          % (sorted(q_seen), sorted({i // 8 for i in q_seen})))
    if not q_seen or all(0 <= i <= 7 for i in q_seen):
        failures.append("micro-life quirk did not leave row0: %s" % sorted(q_seen))
    end = time.time() + 3.0
    while time.time() < end:
        app.root.update()
        time.sleep(0.02)
    if app._anim is not app.anims.get("idle"):
        failures.append("micro-life quirk did not hand back to idle")
    else:
        print("[selfcheck] micro-life handed back to idle: OK")

    app.destroy()
    dt = time.time() - t0
    if failures:
        print("[selfcheck] FAIL:")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("[selfcheck] PASS (%.1fs)" % dt)
    sys.exit(0)


# ----------------------------------------------------------------------- demo
def _demo():
    app = PetApp({})

    def script():
        r = app.root.after
        r(2000, lambda: (app.set_state("working"),
                         app.bubble("▶ 「demo」 开始干活", "working")))
        r(5000, lambda: (app.set_state("blocked"),
                         app.bubble("⏸ 「demo」 等你确认", "blocked")))
        r(8000, lambda: (app.set_state("done"),
                         app.bubble("✅ 「demo-task-x」 已完成", "done", 8000),
                         app.set_summary([
                             {"pane_id": "p1", "name": "demo-task-x", "status": "done",
                              "workspace_id": "ws"},
                             {"pane_id": "p2", "name": "另一个面板", "status": "working",
                              "workspace_id": "ws"},
                         ])))
        r(12000, lambda: (app.set_state("asleep"),))
        r(22000, lambda: app.set_state("working"))

    app.when_shown(script)
    app.run()


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        _selfcheck()
    _demo()
