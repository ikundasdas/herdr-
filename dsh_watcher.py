"""DSH (DeepSeek Harness) session watcher: exposes the harness's active session
to the desktop pet as a virtual pane, so DSH tasks appear alongside herdr
panes without touching the harness or its zstd-compressed transcripts.

Data source: ~/.dsh/storages/session_projcache/sessions/session-*.json
Plain JSON, atomically replaced by the harness on every step, with a recent
mtime exactly when the harness is doing something. No DSH configuration is
touched and no restart is needed.

Status mapping (record.rows.<row>.val):
    working -> sessionStats.openStep  is a dict (a step is running)
               or turnOutline.draft is a non-empty string (a reply is being
               generated right now)
    idle    -> the session exists but neither is true
    done    -> synthesized by this watcher: a working -> idle transition
               yields two feed batches [done] then [idle], so the existing
               StateMachine sees working -> done (notified, bubble) followed
               by done -> idle (silent). No state machine changes needed.

Freshness: a session is only watched while its projection file was written
within idle_seconds. The harness rewrites the file on every step, so a
session that goes quiet for longer than that is considered gone (even one
whose last snapshot still carried an open step - e.g. after the harness
closed or crashed).

Public contract (what main.py uses):
    DshWatcher(projcache_dir=None, idle_seconds=120.0, clock=time.time)
    watcher.enabled        -> False when the projcache directory is missing
    watcher.tick()         -> list of pane-snapshot batches to feed in order:
                              [] when there is no active DSH session,
                              [[pane]] for a plain snapshot, or
                              [[done_pane], [idle_pane]] right after a task
                              finishes. Pane dicts mimic herdr pane shapes
                              enough for Brain.feed / display_name.
Every parse failure degrades to "no active session"; nothing raises.
"""

import json
import os
import time

DEFAULT_IDLE_SECONDS = 120.0


def _rows_val(record, row_key, default=None):
    """Safe read of record.rows.<row>.val across malformed/missing shapes."""
    try:
        rows = record["record"]["rows"]
        row = rows[row_key]
        return row["val"]
    except (KeyError, TypeError, ValueError):
        return default


def default_projcache_dir():
    home = os.path.expanduser("~")
    return os.path.join(home, ".dsh", "storages", "session_projcache", "sessions")


def _identity_cwd(record):
    try:
        identity = record["record"]["identity"]
        if isinstance(identity, dict):
            return identity.get("cwd") or ""
    except (KeyError, TypeError, ValueError):
        pass
    return ""


def _is_working(session_stats, turn_outline):
    if isinstance(session_stats, dict):
        open_step = session_stats.get("openStep")
        if isinstance(open_step, dict):
            return True
    if isinstance(turn_outline, dict):
        draft = turn_outline.get("draft")
        if isinstance(draft, str) and draft.strip():
            return True
    return False


class DshWatcher:
    """Poll the harness's active session and turn it into virtual panes."""

    def __init__(self, projcache_dir=None, idle_seconds=DEFAULT_IDLE_SECONDS, clock=time.time):
        self.projcache_dir = projcache_dir or default_projcache_dir()
        self.idle_seconds = float(idle_seconds)
        self.clock = clock
        self.prev = {}       # pane_id -> status, for done synthesis
        self.baselined = False

    @property
    def enabled(self):
        return os.path.isdir(self.projcache_dir)

    # -- public -------------------------------------------------------------

    def tick(self):
        """Batches of virtual panes to feed, in order (see module docstring)."""
        if not self.enabled:
            return []
        active = self._active_session()
        if active is None:
            self.prev = {}
            self.baselined = False
            return []
        session_id, snapshot = active
        pane = self._to_pane(session_id, snapshot)
        status = pane["status"]

        batches = []
        if self.baselined:
            previous = self.prev.get(pane["pane_id"])
            if previous == "working" and status == "idle":
                done_pane = dict(pane, status="done")
                batches.append([done_pane])
        self.prev = {pane["pane_id"]: status}
        self.baselined = True
        batches.append([pane])
        return batches

    # -- internals ----------------------------------------------------------

    def _active_session(self):
        """(session_id, snapshot) for the freshest live session, or None."""
        try:
            names = [
                name for name in os.listdir(self.projcache_dir)
                if name.startswith("session-") and name.endswith(".json")
            ]
        except OSError:
            return None
        if not names:
            return None

        now = self.clock()
        candidates = []
        for name in names:
            path = os.path.join(self.projcache_dir, name)
            data = self._read_json(path)
            if data is None:
                continue  # half-written replace: skip, never crash
            snapshot = self._snapshot(data)
            if snapshot is None:
                continue
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                mtime = 0.0
            if (now - mtime) <= self.idle_seconds:
                candidates.append((mtime, name, snapshot))

        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0], reverse=True)
        mtime, name, snapshot = candidates[0]
        session_id = name[len("session-"):-len(".json")]
        return session_id, snapshot

    @staticmethod
    def _read_json(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return None

    def _snapshot(self, data):
        """Extract {title, workspace, active} from a harness projection."""
        if not isinstance(data, dict):
            return None
        session_stats = _rows_val(data, "sessionStats")
        turn_outline = _rows_val(data, "turnOutline")
        title = _rows_val(data, "title")
        if not isinstance(title, str):
            title = ""
        cwd = _identity_cwd(data)
        workspace = os.path.basename(str(cwd).rstrip("\\/")) if cwd else ""
        active = _is_working(session_stats, turn_outline)
        return {"title": title, "workspace": workspace, "active": active}

    def _to_pane(self, session_id, snapshot):
        """Virtual pane shaped like the herdr panes Brain.feed already reads."""
        title = snapshot.get("title") or ("dsh:" + session_id)
        status = "working" if snapshot.get("active") else "idle"
        return {
            "pane_id": "dsh:%s" % session_id,
            "agent": "dsh",
            "agent_status": status,
            "status": status,  # internal alias: tick() reads pane["status"]
            "terminal_title_stripped": title,
            "name": title,
            "workspace_id": snapshot.get("workspace") or "",
        }