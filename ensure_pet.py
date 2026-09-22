"""One-shot bootstrap invoked by herdr hooks (see herdr-plugin.toml).

herdr passes the event name in HERDR_PLUGIN_EVENT and the payload as JSON in
HERDR_PLUGIN_EVENT_JSON (data.pane_id / data.agent_status / data.title). This
script does two cheap things and then dies:

1. Append the event (timestamp, pane, status, title) to the pet's inbox file
   so a live pet reacts within ~250ms instead of waiting for its next poll.
2. Guarantee exactly one pet process: a named mutex tells whether main.py is
   alive; when it is not, spawn `pythonw.exe main.py` detached and move on.

Everything is best-effort. The watchdog kills this process after a few seconds
no matter what, so a stuck pipe or a hung child can never leak invisible
pythonw instances.
"""

import ctypes
import json
import os
import subprocess
import sys
import threading
import time

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

from herdr_client import data_dirs

CONFIG_DIR, STATE_DIR = data_dirs()
INBOX_PATH = os.path.join(STATE_DIR, "inbox.jsonl")
LOG_PATH = os.path.join(os.environ.get("TEMP") or ".", "herdr-desktop-pet-hook.log")

MUTEX_NAME = "Local\\herdr-desktop-pet-single"
ERROR_ALREADY_EXISTS = 183

INBOX_MAX_BYTES = 1024 * 1024
WATCHDOG_SECONDS = 3.0

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200

# Keep a reference so the mutex handle is not garbage collected; the kernel
# releases it when this process exits either way.
_mutex_handle = None


def log(message):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
    except OSError:
        pass


def quit(code=0):
    try:
        if sys.stdout is not None:
            sys.stdout.flush()
        if sys.stderr is not None:
            sys.stderr.flush()
    except Exception:
        pass
    os._exit(code)


def event_fields():
    """Best-effort extraction of pane_id / status / title from the hook payload."""
    raw = os.environ.get("HERDR_PLUGIN_EVENT_JSON")
    data = {}
    if raw:
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                inner = payload.get("data")
                data = inner if isinstance(inner, dict) else payload
        except ValueError:
            data = {}
    return {
        "pane_id": data.get("pane_id") or "",
        "status": data.get("agent_status") or "",
        "title": data.get("title") or "",
    }


def touch_inbox():
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        try:
            if os.path.getsize(INBOX_PATH) > INBOX_MAX_BYTES:
                # Rotate by truncating: the pet tolerates a fresh empty inbox.
                with open(INBOX_PATH, "w", encoding="utf-8"):
                    pass
        except OSError:
            pass
        fields = event_fields()
        fields["t"] = round(time.time(), 3)
        fields["event"] = os.environ.get("HERDR_PLUGIN_EVENT") or "startup"
        with open(INBOX_PATH, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(fields, ensure_ascii=False) + "\n")
        return True
    except OSError as error:
        log("inbox write failed: %s" % error)
        return False


def pet_alive():
    """True when another process already owns the single-instance mutex."""
    global _mutex_handle
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, 0, MUTEX_NAME)
        if not handle:
            # Cannot tell; spawning is cheaper than a missing pet, and the
            # second instance loses the same mutex race on its side.
            return False
        _mutex_handle = handle
        return ctypes.get_last_error() == ERROR_ALREADY_EXISTS
    except Exception as error:
        log("mutex probe failed: %r" % (error,))
        return False


def spawn_pet():
    pythonw = sys.executable
    sibling = os.path.join(os.path.dirname(pythonw), "pythonw.exe")
    if os.path.basename(pythonw).lower() != "pythonw.exe" and os.path.exists(sibling):
        pythonw = sibling
    try:
        subprocess.Popen(
            [pythonw, os.path.join(PLUGIN_DIR, "main.py")],
            cwd=PLUGIN_DIR,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            # HERDR_SOCKET_PATH and friends flow through via the inherited env.
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        )
        return True
    except OSError as error:
        log("spawn failed: %s" % error)
        return False


def main():
    event = os.environ.get("HERDR_PLUGIN_EVENT") or "startup"
    touched = touch_inbox()
    if pet_alive():
        log("event=%s inbox=%s pet already running" % (event, "ok" if touched else "fail"))
        return
    spawned = spawn_pet()
    log("event=%s inbox=%s spawned=%s" % (event, "ok" if touched else "fail", spawned))


if __name__ == "__main__":
    threading.Timer(WATCHDOG_SECONDS, lambda: quit(1)).start()
    try:
        main()
    except Exception as error:  # never leave noise behind
        log("unexpected error: %r" % (error,))
    quit(0)
