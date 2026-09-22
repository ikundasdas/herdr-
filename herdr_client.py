"""Small client for the herdr socket API (newline-delimited JSON).

Transport facts (verified against the herdr 0.9.0 docs on this machine):
- HERDR_SOCKET_PATH holds a path *string* such as
  `%APPDATA%\\herdr\\herdr.sock`. The actual Windows named pipe is
  `\\\\.\\pipe\\` + that string verbatim. The `.sock` file itself is not an
  AF_UNIX socket, so there is no filesystem-socket fallback.
- Discovery order when HERDR_SOCKET_PATH is missing (standalone runs):
  1. `\\\\.\\pipe\\` + %APPDATA%\\herdr\\herdr.sock
  2. `\\\\.\\pipe\\` + every %APPDATA%\\herdr\\sessions\\*\\herdr.sock
  3. `herdr.exe api snapshot` as a subprocess (read-only last resort,
     pane.list only - no focus through this path)

Protocol: one request line `{"id": ..., "method": ..., "params": {...}}\n`,
one reply line `{"id": ..., "result": {...}}` or `{"id": ..., "error": {...}}`.

Importable with no side effects. Nothing here ever raises at import time;
request() returns None when no transport answered.
"""

import json
import os
import shutil
import subprocess
import time

SOURCE = "user:desktop-pet"

PIPE_PREFIX = "\\\\.\\pipe\\"

# A named pipe open has no timeout in the standard library. Callers that live
# long enough to care pass their own deadline; the hook bootstrap runs under a
# watchdog instead.
RETRY_SLEEP_SECONDS = 0.12

# Do not let a console window flash when we shell out to herdr.exe.
CREATE_NO_WINDOW = 0x08000000


def data_dirs():
    """Return (config_dir, state_dir) — one fixed directory, always the same.

    These used to honor herdr's hook environment (HERDR_PLUGIN_CONFIG_DIR /
    HERDR_PLUGIN_STATE_DIR) and fall back to %LOCALAPPDATA%\\herdr-desktop-pet
    for standalone runs. That silently split the pet's state in two: a pet
    launched by a herdr hook read the hook directory, a hand-launched pet read
    the fallback, and editing either file had no effect on the other one. In
    practice the pet changed size on its own whenever a hook restarted it after
    a manual run (config.json said scale=m54 in one file, native in the other).

    One fixed path for every launcher is the only arrangement that cannot
    surprise, so the hook environment is deliberately ignored. ensure_pet.py
    and main.py must agree on these paths, so the logic lives in one place.
    """
    base = os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
        "herdr-desktop-pet",
    )
    return base, base


def herdr_base_dir():
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return os.path.join(appdata, "herdr")


def default_socket_strings():
    """Path strings herdr uses when no hook environment was passed along."""
    base = herdr_base_dir()
    if not base:
        return []
    strings = [os.path.join(base, "herdr.sock")]
    sessions = os.path.join(base, "sessions")
    try:
        entries = sorted(os.scandir(sessions), key=lambda entry: entry.name)
        for entry in entries:
            try:
                if entry.is_dir():
                    strings.append(os.path.join(entry.path, "herdr.sock"))
            except OSError:
                continue
    except OSError:
        pass
    return strings


def pipe_target(socket_string):
    """`\\ .\\pipe\\` + the socket path string exactly as herdr reports it."""
    if socket_string.startswith(PIPE_PREFIX):
        return socket_string
    return PIPE_PREFIX + socket_string


def candidates():
    """Windows pipe names to try, in the documented discovery order."""
    strings = []
    env_value = os.environ.get("HERDR_SOCKET_PATH")
    if env_value:
        strings.append(env_value)
    strings.extend(default_socket_strings())

    targets, seen = [], set()
    for socket_string in strings:
        target = pipe_target(socket_string)
        if target not in seen:
            seen.add(target)
            targets.append(target)
    return targets


def _find_panes(value):
    """Dig a list of pane dicts out of an arbitrary JSON snapshot shape."""
    if isinstance(value, dict):
        panes = value.get("panes")
        if _looks_like_panes(panes):
            return panes
        for item in value.values():
            found = _find_panes(item)
            if found is not None:
                return found
    elif isinstance(value, list):
        if _looks_like_panes(value):
            return value
        for item in value:
            found = _find_panes(item)
            if found is not None:
                return found
    return None


def _looks_like_panes(value):
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(pane, dict) and pane.get("pane_id") for pane in value)
    )


class HerdrClient:
    """Blocking, connection-per-request herdr client. Every method is None-safe."""

    #: Description of the transport that last answered ("pipe:..." or
    #: "snapshot-subprocess"), or None while nothing worked yet.
    transport = None

    def __init__(self, socket_path=None):
        if socket_path:
            self._targets = [pipe_target(socket_path)]
        else:
            self._targets = candidates()

    # -- plumbing -------------------------------------------------------

    def request(self, method, params, attempts=3):
        """Send one request; return the parsed reply dict or None.

        Windows occasionally rejects a pipe open with EINVAL when several
        hooks fire for the same moment, so each candidate gets `attempts`
        tries with a short sleep. Once a candidate answers it is pinned to
        the front of the list.
        """
        payload = json.dumps(
            {
                "id": "%s:%d" % (SOURCE, int(time.time() * 1000)),
                "method": method,
                "params": params,
            }
        ) + "\n"
        for target in list(self._targets):
            reply = self._request_target(target, payload, attempts)
            if reply is not None:
                self.transport = "pipe:%s" % target
                self._targets.remove(target)
                self._targets.insert(0, target)
                return reply
        return None

    def _request_target(self, target, payload, attempts):
        for attempt in range(attempts):
            reply = self._request_once(target, payload)
            if reply is not None:
                return reply
            if attempt + 1 < attempts:
                time.sleep(RETRY_SLEEP_SECONDS)
        return None

    @staticmethod
    def _request_once(target, payload):
        try:
            with open(target, "r+b", buffering=0) as pipe:
                pipe.write(payload.encode("utf-8"))
                line = pipe.readline()
        except OSError:
            return None
        if not line:
            return None
        try:
            return json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            return None

    # -- API helpers ------------------------------------------------------

    def pane_list(self):
        """Tracked panes as plain dicts, or None when nothing answered.

        An empty list is a valid answer (no panes open); None means failure,
        which the caller counts for the offline policy.
        """
        reply = self.request("pane.list", {})
        if isinstance(reply, dict) and isinstance(reply.get("result"), dict):
            panes = reply["result"].get("panes")
            if isinstance(panes, list):
                return panes
        return self._snapshot_fallback()

    def pane_focus(self, pane_id):
        if not self._targets or self.transport == "snapshot-subprocess":
            # No write-capable transport: the snapshot fallback is read-only.
            return False
        reply = self.request("pane.focus", {"pane_id": pane_id})
        return isinstance(reply, dict) and "result" in reply

    def close(self):
        """No persistent connection to drop; kept for interface symmetry."""
        self.transport = None

    # -- last resort --------------------------------------------------------

    def _snapshot_fallback(self):
        """`herdr.exe api snapshot`: works even with a weird pipe setup."""
        exe = shutil.which("herdr.exe") or shutil.which("herdr")
        if not exe:
            return None
        try:
            completed = subprocess.run(
                [exe, "api", "snapshot"],
                capture_output=True,
                timeout=5,
                creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode != 0:
            return None
        try:
            data = json.loads(completed.stdout.decode("utf-8", "replace"))
        except ValueError:
            return None
        panes = _find_panes(data)
        if panes is None:
            return None
        self.transport = "snapshot-subprocess"
        return panes
