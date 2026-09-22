# -*- coding: utf-8 -*-
"""One-click control of the local DeepSeek Harness web UI for the desktop pet.

start/stop/open DSH web (default http://127.0.0.1:3080) without touching any
config file: the dsh CLI is resolved from the npx cache the same way
~/.dsh/start-dsh-web.ps1 does, and the port is probed directly.

Public contract:
    DSH_WEB_PORT, DSH_WEB_URL
    find_dsh_bin() -> path to @deepseek-ai/dsh lib/bin.js, or None
    is_running(port=...) -> bool (something listens on 127.0.0.1)
    web_pids()      -> [pid] of node processes serving the dsh bin.js
    start_web()     -> (ok, message)  background start + short wait for port
    stop_web()      -> (ok, message)  taskkill on the dsh node processes
    status_text()   -> short human line for menu labels
Never raises: every failure degrades to a (False, message) or empty result.
"""

import glob
import os
import shutil
import socket
import subprocess
import sys
import time

DSH_WEB_PORT = 3080
DSH_WEB_URL = "http://127.0.0.1:%d" % DSH_WEB_PORT

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
NOWINDOW = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW


def find_dsh_bin():
    """Newest @deepseek-ai/dsh lib/bin.js anywhere under the npx cache."""
    npx_root = os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
        "npm-cache", "_npx",
    )
    pattern = os.path.join(npx_root, "*", "node_modules",
                           "@deepseek-ai", "dsh", "lib", "bin.js")
    try:
        matches = glob.glob(pattern)
    except OSError:
        return None
    if not matches:
        return None
    matches.sort(key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0.0)
    return matches[-1]


def _node_exe():
    node = shutil.which("node")
    if node and os.path.isfile(node):
        return node
    fallback = os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                            "nodejs", "node.exe")
    return fallback if os.path.isfile(fallback) else None


def is_running(port=DSH_WEB_PORT, timeout=0.4):
    """True when something accepts connections on 127.0.0.1:port."""
    try:
        sock = socket.create_connection(("127.0.0.1", int(port)), timeout=timeout)
        sock.close()
        return True
    except OSError:
        return False


def web_pids():
    """PIDs of node.exe processes serving the dsh CLI web command.

    Matched with -like wildcards (case-insensitive) on the command line:
    the process must mention dsh, bin.js and the word web ('...bin.js web'),
    which separates it from vite, MCP servers etc. without regex escaping."""
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" | "
        "Where-Object { $_.CommandLine -like '*dsh*' -and "
        "$_.CommandLine -like '*bin.js*' -and $_.CommandLine -like '* web*' } | "
        "Select-Object -ExpandProperty ProcessId"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=15,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        return []
    pids = []
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
    return pids


def start_web():
    """Spawn `node dshBin web` detached; wait up to ~9s for the port."""
    if is_running():
        return True, "DSH Web 已在运行 → %s" % DSH_WEB_URL
    bin_path = find_dsh_bin()
    node = _node_exe()
    if not bin_path:
        return False, "找不到 DSH 程序（npm 缓存里没有 @deepseek-ai/dsh）"
    if not node:
        return False, "找不到 node.exe（请先安装 Node.js）"
    try:
        subprocess.Popen(
            [node, bin_path, "web"],
            cwd=os.path.expanduser("~"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=NOWINDOW,
        )
    except OSError as error:
        return False, "DSH Web 启动失败: %r" % (error,)
    deadline = time.time() + 9.0
    while time.time() < deadline:
        if is_running():
            return True, "DSH Web 已启动 → %s" % DSH_WEB_URL
        time.sleep(0.3)
    return True, "DSH Web 已后台启动（端口还没就绪，稍等再刷新）"


def stop_web():
    """Kill the node processes serving the dsh bin.js (never blind killers)."""
    pids = web_pids()
    if not pids:
        if is_running():
            return False, "端口 %d 被占用，但不是 DSH Web 进程——没敢乱杀" % DSH_WEB_PORT
        return False, "DSH Web 本来就没在运行"
    killed = []
    for pid in pids:
        try:
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True, timeout=10, creationflags=CREATE_NO_WINDOW,
            )
            killed.append(pid)
        except Exception:
            pass
    if killed:
        return True, "DSH Web 已停止（PID %s）" % ",".join(map(str, killed))
    return False, "DSH Web 停止失败，请手动检查"


def open_web():
    """Open the browser at the DSH web URL (starts the server first if needed)."""
    ok = True
    message = ""
    if not is_running():
        ok, message = start_web()
    if ok:
        try:
            if sys.platform == "win32":
                os.startfile(DSH_WEB_URL)
            else:
                subprocess.Popen(["xdg-open", DSH_WEB_URL])
            return True, "已打开 DSH Web → %s" % DSH_WEB_URL
        except OSError:
            return False, "打不开浏览器，请手动访问 %s" % DSH_WEB_URL
    return ok, message


def status_text():
    pids = web_pids()
    if pids:
        return "运行中 (PID %s)" % pids[0]
    if is_running():
        return "端口被其他程序占用"
    return "未运行"