"""Console implementation of the pet_render.PetApp contract.

Lets main.py (and --simulate) run end to end without tkinter: every call is
printed with a timestamp instead of drawing. Method signatures match the GUI
renderer's contract exactly, so main.py cannot tell the difference.
"""

import sys
import threading
import time

# Bubble texts contain emoji (✅⏸▶) that legacy GBK consoles cannot encode;
# degrade those characters to '?' instead of losing the whole printed line.
try:
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    if sys.stderr is not None and hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(errors="replace")
except Exception:
    pass


def _stamp():
    now = time.time()
    return "%s.%03d" % (time.strftime("%H:%M:%S", time.localtime(now)), (now % 1) * 1000)


class PetApp:
    def __init__(self, config):
        self._config = dict(config or {})
        self._print_lock = threading.Lock()
        self._stop = threading.Event()
        self._on_drag_end = None
        self._on_activate = None
        self._line("created (position=%r language=%r)" % (self._config.get("position"), self._config.get("language")))

    # -- contract surface -------------------------------------------------

    def set_state(self, state):
        self._line("state=%s" % state)

    def bubble(self, text, kind, ms=6000):
        self._line("bubble kind=%s ms=%d text=%s" % (kind, ms, text))

    def set_summary(self, panes):
        panes = panes or []
        detail = ", ".join(
            "%s:%s" % (pane.get("name"), pane.get("status")) for pane in panes
        )
        self._line("summary count=%d [%s]" % (len(panes), detail or "-"))

    def on_drag_end(self, cb):
        self._on_drag_end = cb

    def on_activate(self, cb):
        self._on_activate = cb

    def post(self, fn, *args):
        fn(*args)  # single-threaded test lane: run immediately

    def run(self):
        self._line("run() - Ctrl+C to stop")
        try:
            while not self._stop.wait(0.5):
                pass
        except KeyboardInterrupt:
            self._line("interrupted")

    # -- helpers ------------------------------------------------------------

    def _line(self, message):
        with self._print_lock:
            try:
                sys.stdout.write("%s [pet-headless] %s\n" % (_stamp(), message))
                sys.stdout.flush()
            except Exception:
                pass
