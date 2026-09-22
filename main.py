"""The desktop pet's brain: polls herdr, watches agent state changes, drives the pet.

Run styles:
    pythonw main.py                     GUI (renderer pet_render.py, separate lane)
    python  main.py --headless          console renderer (pet_render_headless)
    python  main.py --simulate --headless   scripted feed, no socket, exits when done

This process holds the single-instance mutex, polls `pane.list` every
poll_ms, and reacts to state *transitions* (StateMachine below) with a state
change, a speech bubble and sometimes a beep. herdr's event hooks feed
`inbox.jsonl`, which is used only as a low-latency nudge plus the human title
from the event payload; the poll is always the authority on what happened.

Every renderer touch goes through app.post(...) - tkinter is single-threaded
and the poll/inbox loops live on daemon threads.
"""

import argparse
import ctypes
import json
import os
import sys
import threading
import time

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

from herdr_client import HerdrClient, candidates as discover_candidates
from herdr_client import data_dirs
from dsh_watcher import DshWatcher, DEFAULT_IDLE_SECONDS

CONFIG_DIR, STATE_DIR = data_dirs()
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
INBOX_PATH = os.path.join(STATE_DIR, "inbox.jsonl")
LOG_PATH = os.path.join(os.environ.get("TEMP") or ".", "herdr-desktop-pet.log")

MUTEX_NAME = "Local\\herdr-desktop-pet-single"
ERROR_ALREADY_EXISTS = 183

DEFAULT_CONFIG = {
    "poll_ms": 1500,
    "notify": ["done", "blocked", "working"],
    "sound": True,
    "position": None,
    "language": "zh",
    # renderer-owned keys carried through so drag/scale changes survive restarts
    "scale": "m72",
    "reduced_motion": False,
    # DSH (DeepSeek Harness) session source; off switches it back to herdr-only
    "dsh_watch": True,
    "dsh_poll_ms": 1500,
    "dsh_idle_seconds": 120,
}

OFFLINE_POLL_FAILURES = 20  # ~30s at the default poll rate
OFFLINE_EXIT_SECONDS = 6.0
HINT_TTL_SECONDS = 5.0  # an inbox title only decorates a bubble while fresh

BUBBLE_TEXT = {
    "done": "✅ 「%s」 已完成",
    "blocked": "⏸ 「%s」 等你确认",
    "working": "▶ 「%s」 开始干活",
}
BUBBLE_MS = {"done": 8000, "blocked": 10000, "working": 3000}
SOUND_KINDS = {"done", "blocked"}

# Referenced until exit so the kernel handle survives; guards double spawns.
_mutex_handle = None


def log(message):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
    except OSError:
        pass


# --------------------------------------------------------------------------
# config


def load_config():
    config = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            stored = json.load(handle)
        if isinstance(stored, dict):
            for key in DEFAULT_CONFIG:
                if key in stored:
                    config[key] = stored[key]
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as error:
        log("config load failed, using defaults: %s" % error)
    return config


def save_config(config):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------
# state machine (pure; test_statemachine.py imports it)


class StateMachine:
    """Watches pane snapshots and emits notification-worthy transitions.

    on_snapshot(panes) takes a list of {'pane_id','name','status'} dicts and
    returns a list of (pane_id, name, prev_status, new_status) events:
    - the first snapshot is the baseline and never emits;
    - a new pane appearing has no prev, so it never emits either;
    - only transitions *into* a notified status count (working->idle: nothing);
    - the same (pane_id, new_status) cannot re-emit within `dedupe_seconds`,
      which absorbs flapping like done->working->done;
    - panes that disappear are forgotten, dedupe history included.
    """

    def __init__(self, notify=("done", "blocked", "working"), clock=time.time, dedupe_seconds=4.0):
        self.notify = set(notify or ())
        self.clock = clock
        self.dedupe_seconds = dedupe_seconds
        self.prev = {}           # pane_id -> (name, status)
        self.last_emitted = {}   # (pane_id, new_status) -> clock value
        self.baselined = False

    def on_snapshot(self, panes):
        now = self.clock()
        current = {}
        for pane in panes or ():
            pane_id = pane.get("pane_id")
            if not pane_id:
                continue
            current[pane_id] = (
                pane.get("name") or pane_id,
                pane.get("status") or "unknown",
            )

        if not self.baselined:
            self.baselined = True
            self.prev = dict(current)
            return []

        events = []
        for pane_id, (name, status) in current.items():
            previous = self.prev.get(pane_id)
            self.prev[pane_id] = (name, status)
            if previous is None or previous[1] == status or status not in self.notify:
                continue
            key = (pane_id, status)
            last = self.last_emitted.get(key)
            if last is not None and now - last < self.dedupe_seconds:
                continue
            self.last_emitted[key] = now
            events.append((pane_id, name, previous[1], status))

        for pane_id in [pid for pid in self.prev if pid not in current]:
            self.prev.pop(pane_id, None)
            for key in [k for k in self.last_emitted if k[0] == pane_id]:
                self.last_emitted.pop(key, None)
        return events


def display_name(pane):
    """Task name for a pane, in the documented resolution order."""
    for key in ("terminal_title_stripped", "title", "label"):
        value = pane.get(key)
        if value and str(value).strip():
            return str(value).strip()
    cwd = pane.get("cwd")
    if cwd:
        base = os.path.basename(str(cwd).rstrip("\\/"))
        if base:
            return base
    return pane.get("pane_id") or "pane"


# --------------------------------------------------------------------------
# brain: glues poller, hooks inbox and renderer together


class Brain:
    def __init__(self, app, client, config, watcher=None):
        self.app = app
        self.client = client
        self.config = config
        self.watcher = watcher  # DshWatcher or None: extra DSH task source
        self.sm = StateMachine(notify=config.get("notify") or [])
        self.lock = threading.Lock()
        self.latest_tracked = []          # [{'pane_id','name','status','workspace_id'}]
        self.hints = {}                   # pane_id -> {'t','status','title'} from hooks
        self.last_notified_pane_id = None
        self.return_at = 0.0              # monotonic instant bubbles stop owning the pet
        self.last_displayed = None        # last state we asked the renderer to show
        self.nudge = threading.Event()    # inbox hook asks for an immediate poll
        self.poll_ms = max(200, int(config.get("poll_ms") or 1500))
        self.quitting = False

    # -- snapshot -> events -------------------------------------------------

    def feed(self, raw_panes):
        tracked = []
        for pane in raw_panes:
            if not pane.get("agent"):     # sidebar & co. have no agent: not tasks
                continue
            pane_id = pane.get("pane_id")
            if not pane_id:
                continue
            tracked.append(
                {
                    "pane_id": pane_id,
                    "name": display_name(pane),
                    "status": pane.get("agent_status") or "unknown",
                    "workspace_id": pane.get("workspace_id"),
                }
            )
        with self.lock:
            self.latest_tracked = tracked

        try:
            events = self.sm.on_snapshot(tracked)
        except Exception as error:  # a broken snapshot must not kill the loop
            log("state machine error: %r" % (error,))
            events = []

        self.app.post(self.app.set_summary, tracked)
        for event in events:
            self.app.post(self.on_event, event)
        self.app.post(self.refresh_state)

    def on_event(self, event):
        """Runs on the renderer's main thread (posted). event=(pane_id,name,prev,new)."""
        pane_id, name, previous, status = event
        hook_title = self._hook_title(pane_id)
        if hook_title:
            name = hook_title  # the human label straight from the hook payload
        log("transition pane=%s %s->%s name=%s" % (pane_id, previous, status, name))

        self.last_notified_pane_id = pane_id
        template = BUBBLE_TEXT.get(status, "%s")
        ms = BUBBLE_MS.get(status, 6000)
        self.app.post(self.app.set_state, status)
        self.app.post(self.app.bubble, template % name, status, ms)
        if status in SOUND_KINDS and self.config.get("sound"):
            self._play_sound(status)

        self.return_at = time.monotonic() + ms / 1000.0
        timer = threading.Timer(ms / 1000.0 + 0.05, lambda: self.app.post(self.refresh_state))
        timer.daemon = True
        timer.start()

    def refresh_state(self):
        """Re-derive the pet state unless a bubble still owns the moment."""
        if time.monotonic() < self.return_at:
            return
        state = self.aggregate()
        if state != self.last_displayed:
            self.last_displayed = state
            log("aggregate -> %s" % state)
        self.app.post(self.app.set_state, state)

    def aggregate(self):
        """Display state = priority over CURRENT pane statuses.

        'done' (finished but UNREAD) is persistent on purpose: the pet keeps
        celebrating on row8 until herdr flips the pane to idle when the user
        actually views it — priority blocked > working > done > idle."""
        with self.lock:
            statuses = {pane["status"] for pane in self.latest_tracked}
        if "blocked" in statuses:
            return "blocked"
        if "working" in statuses:
            return "working"
        if "done" in statuses:
            return "done"
        return "idle"

    def _hook_title(self, pane_id):
        with self.lock:
            hint = self.hints.get(pane_id)
        if not hint:
            return ""
        if time.time() - float(hint.get("t") or 0.0) > HINT_TTL_SECONDS:
            return ""
        return hint.get("title") or ""

    # -- sound ------------------------------------------------------------

    @staticmethod
    def _play_sound(kind):
        """Beeps block, so they never run on the renderer or poll threads."""

        def run():
            try:
                import winsound

                if kind == "done":
                    winsound.Beep(988, 120)
                    winsound.Beep(1319, 180)
                elif kind == "blocked":
                    winsound.Beep(440, 200)
                    time.sleep(0.08)
                    winsound.Beep(440, 200)
            except Exception:
                pass  # a mute pet is better than a dead one

        threading.Thread(target=run, daemon=True).start()

    # -- renderer callbacks --------------------------------------------------

    def activate(self):
        """Pet was clicked: focus the pane that last spoke up, or the neediest."""
        # Socket I/O on a worker thread keeps the mainloop responsive.
        threading.Thread(target=self._focus_best, daemon=True).start()

    def _focus_best(self):
        if self.client is None:
            return
        with self.lock:
            tracked = list(self.latest_tracked)
        if not tracked:
            return
        ids = {pane["pane_id"] for pane in tracked}
        target = self.last_notified_pane_id if self.last_notified_pane_id in ids else None
        if target is None:
            for wanted in ("blocked", "working"):
                for pane in tracked:
                    if pane["status"] == wanted:
                        target = pane["pane_id"]
                        break
                if target:
                    break
        if not target:
            return
        try:
            ok = self.client.pane_focus(target)
            log("activate focus pane=%s ok=%s" % (target, ok))
        except Exception as error:
            log("activate failed: %r" % (error,))

    def drag_end(self, x, y):
        try:
            self.config["position"] = [int(x), int(y)]
            save_config(self.config)
            log("position saved: %s" % self.config["position"])
        except Exception as error:
            log("position save failed: %r" % (error,))

    # -- background loops -----------------------------------------------------

    def poll_loop(self):
        failures = 0
        transport_logged = False
        while True:
            panes = None
            if self.client is not None:
                try:
                    panes = self.client.pane_list()
                except Exception as error:
                    log("poll raised: %r" % (error,))
                    panes = None
            dsh_batches = []
            if self.watcher is not None:
                try:
                    dsh_batches = self.watcher.tick()
                except Exception as error:
                    log("dsh watcher raised: %r" % (error,))
                    dsh_batches = []
            if panes is None and not dsh_batches:
                failures += 1
                log("poll failed (%d/%d)" % (failures, OFFLINE_POLL_FAILURES))
                if failures >= OFFLINE_POLL_FAILURES:
                    self.go_offline()
                    return
            else:
                failures = 0
                if not transport_logged and self.client is not None:
                    log("poll transport=%s" % self.client.transport)
                    transport_logged = True
                try:
                    if dsh_batches:
                        for batch in dsh_batches:
                            # herdr panes stay current when present; DSH batches
                            # are fed separately so done->idle is visible too.
                            self.feed(list(panes or []) + batch)
                    elif panes is not None:
                        self.feed(panes)
                except Exception as error:
                    log("feed error: %r" % (error,))
            # A hook nudge short-circuits the wait for a low-latency re-poll.
            self.nudge.wait(self.poll_ms / 1000.0)
            self.nudge.clear()

    def inbox_loop(self):
        """Consume hook-written inbox lines: log, cache titles, nudge the poller."""
        offset = 0  # start at 0 so the spawning event's payload is picked up
        while True:
            time.sleep(0.25)
            try:
                size = os.path.getsize(INBOX_PATH)
            except OSError:
                continue
            if size < offset:
                offset = 0  # the bootstrap rotated the file
            if size == offset:
                continue
            try:
                with open(INBOX_PATH, "r", encoding="utf-8", errors="replace") as handle:
                    handle.seek(offset)
                    lines = handle.read().splitlines()
                offset = size
            except OSError:
                continue
            fresh = False
            with self.lock:
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    pane_id = record.get("pane_id") or ""
                    if pane_id:
                        self.hints[pane_id] = {
                            "t": float(record.get("t") or time.time()),
                            "status": record.get("status") or "",
                            "title": record.get("title") or "",
                        }
                    log(
                        "inbox event=%s pane=%s status=%s title=%s"
                        % (record.get("event"), pane_id, record.get("status"), record.get("title"))
                    )
                    fresh = True
            if fresh:
                self.nudge.set()

    def go_offline(self):
        if self.quitting:
            return
        self.quitting = True
        log("sources unreachable, pet goes to sleep")
        self.app.post(self.app.set_state, "asleep")
        self.app.post(self.app.bubble, "任务源都不在线,先睡了 💤", "info", 5000)
        threading.Timer(OFFLINE_EXIT_SECONDS, lambda: (log("offline exit"), os._exit(0))).start()


# --------------------------------------------------------------------------
# process wiring


def acquire_singleton_lock():
    """False when another pet instance already owns the mutex."""
    global _mutex_handle
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, 0, MUTEX_NAME)
        if not handle:
            return True  # cannot tell; better one extra pet than none
        _mutex_handle = handle
        return ctypes.get_last_error() != ERROR_ALREADY_EXISTS
    except Exception as error:
        log("mutex unavailable: %r" % (error,))
        return True


def load_pet_app_class(headless):
    if headless:
        import pet_render_headless as module
        return module.PetApp
    try:
        import pet_render as module
        return module.PetApp
    except ImportError as error:
        log("GUI renderer not available (pet_render.py): %r" % (error,))
        try:
            print("desktop-pet: pet_render.py 未安装,无法启动 GUI;可用 --headless 试跑", file=sys.stderr)
        except Exception:
            pass
        os._exit(1)


SIMULATE_SCRIPT = ["working", "done", "blocked"]


def simulate_panes(status):
    return [
        {
            "pane_id": "sim-1",
            "agent": "sim-agent",
            "agent_status": status,
            "terminal_title_stripped": "示例任务",
            "title": "sim title",
            "workspace_id": "ws-sim",
        }
    ]


def run_simulate(brain):
    """Scripted snapshots, 1s apart, through the real feed pipeline. No socket."""
    log("simulate: start")
    for index, status in enumerate(SIMULATE_SCRIPT):
        brain.feed(simulate_panes(status))
        if index + 1 < len(SIMULATE_SCRIPT):
            time.sleep(1.0)
    time.sleep(0.5)  # let the last sound thread finish its beeps
    log("simulate: end")


def run_simulate_dsh(brain):
    """Headless: tick the real DSH watcher, feed the real projcache data."""
    log("simulate-dsh: start (watcher=%s)" % (brain.watcher is not None))
    for _ in range(12):
        if brain.watcher is not None:
            try:
                for batch in brain.watcher.tick():
                    brain.feed(batch)
            except Exception as error:
                log("dsh watcher raised: %r" % (error,))
        time.sleep(1.0)
    log("simulate-dsh: end")


def make_watcher(config, force=False):
    """Build the DSH watcher, or None when disabled by config or unavailable."""
    if not force and not config.get("dsh_watch"):
        return None
    watcher = DshWatcher(
        idle_seconds=float(config.get("dsh_idle_seconds") or DEFAULT_IDLE_SECONDS)
    )
    return watcher if watcher.enabled else None


def main(argv=None):
    parser = argparse.ArgumentParser(description="herdr desktop pet")
    parser.add_argument("--headless", action="store_true", help="console renderer instead of tkinter")
    parser.add_argument("--simulate", action="store_true", help="scripted feed, no socket, exits when done")
    parser.add_argument("--simulate-dsh", action="store_true", help="headless: feed real DSH projcache snapshots")
    args = parser.parse_args(argv)

    if not acquire_singleton_lock():
        return 0  # lost the race with another spawn: exit silently
    config = load_config()
    for directory in (CONFIG_DIR, STATE_DIR):
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as error:
            log("data dir unavailable: %s %s" % (directory, error))

    pet_app_class = load_pet_app_class(args.headless)
    app = pet_app_class(config)
    client = None if (args.simulate or args.simulate_dsh) else HerdrClient()
    watcher = None if args.simulate else make_watcher(config, force=args.simulate_dsh)
    brain = Brain(app, client, config, watcher=watcher)
    app.on_activate(brain.activate)
    app.on_drag_end(brain.drag_end)

    if args.simulate:
        run_simulate(brain)
        return 0
    if args.simulate_dsh:
        run_simulate_dsh(brain)
        return 0

    log("start pid=%s headless=%s candidates=%d dsh=%s"
        % (os.getpid(), args.headless, len(discover_candidates()), watcher is not None))
    threading.Thread(target=brain.poll_loop, daemon=True).start()
    threading.Thread(target=brain.inbox_loop, daemon=True).start()
    try:
        app.run()
    except KeyboardInterrupt:
        log("interrupted")
    return 0


if __name__ == "__main__":
    try:
        exit_code = main()
    except SystemExit:
        raise
    except Exception as error:
        log("fatal: %r" % (error,))
        try:
            print("desktop-pet fatal: %r" % (error,), file=sys.stderr)
        except Exception:
            pass
        exit_code = 1
    os._exit(exit_code)
