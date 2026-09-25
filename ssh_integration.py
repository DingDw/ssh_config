"""Zellij pane ownership and the Alacritty -> WinSCP shortcut (Windows only)."""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.parse import quote

import psutil

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "runtime" / "connections"
ZELLIJ = Path(r"C:\Users\ghoul\AppData\Local\Zellij\zellij.exe")
WINSCP = Path(r"C:\Users\ghoul\AppData\Local\Programs\WinSCP\WinSCP.exe")


def in_zellij():
    return bool(os.environ.get("ZELLIJ_SESSION_NAME") and os.environ.get("ZELLIJ_PANE_ID") is not None)


@contextmanager
def picker_title():
    """Name the manager pane/tab explicitly; restore them when its picker exits."""
    if not in_zellij():
        yield
        return
    session = os.environ["ZELLIJ_SESSION_NAME"]
    pane = pane_number(os.environ["ZELLIJ_PANE_ID"])
    panes = json.loads(zellij_action(session, "list-panes", "--json"))
    current = next((p for p in panes if not p["is_plugin"] and p["id"] == pane), None)
    if current is None:
        raise RuntimeError("无法定位 xxx 所在的窗格。")
    try:
        zellij_action(session, "rename-pane", "--pane-id", str(pane), "--", "xxx")
        zellij_action(session, "rename-tab-by-id", str(current["tab_id"]), "xxx")
        yield
    finally:
        try:
            zellij_action(session, "rename-pane", "--pane-id", str(pane), "--", current.get("title") or "local")
            zellij_action(session, "rename-tab-by-id", str(current["tab_id"]), "--", current["tab_name"])
        except (OSError, RuntimeError, subprocess.SubprocessError):
            pass


def open_ssh_tab(name):
    """Let Zellij own the new SSH process, independently of the connection picker."""
    if not in_zellij():
        raise RuntimeError("新标签页连接需要在 Zellij 中运行 xxx。")
    # Encode only the name to avoid Windows command quoting problems with spaces/quotes.
    token = base64.urlsafe_b64encode(name.encode("utf-8")).decode("ascii")
    return zellij_action(os.environ["ZELLIJ_SESSION_NAME"], "new-tab", "--name", name,
                         "--close-on-exit", "--", sys.executable,
                         str(ROOT / "ssh_manager.py"), "--connect-in-pane", token).strip()


def zellij_action(session, *arguments, source_pane_id=None):
    """Use explicit session and pane targets; do not inherit a stale pane selection."""
    environment = os.environ.copy()
    environment.pop("ZELLIJ_PANE_ID", None)
    if source_pane_id is not None:
        environment["ZELLIJ_PANE_ID"] = str(pane_number(source_pane_id))
    result = subprocess.run(
        [str(ZELLIJ), "--session", session, "action", *arguments],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=8, creationflags=subprocess.CREATE_NO_WINDOW, env=environment,
    )
    if result.returncode:
        raise RuntimeError("无法查询或更新 Zellij 会话，请确认会话仍在运行。")
    return result.stdout


def identity(pid):
    process = psutil.Process(pid)
    return {"pid": process.pid, "created": process.create_time()}


def identity_alive(value):
    try:
        return identity(int(value["pid"])) == value
    except (psutil.Error, KeyError, TypeError, ValueError):
        return False


def pane_number(value):
    match = re.fullmatch(r"(?:terminal_)?(\d+)", str(value))
    if not match:
        raise RuntimeError("当前焦点不是 SSH 终端窗格。")
    return int(match.group(1))


def state_path(session, pane):
    key = json.dumps([session, pane_number(pane)], ensure_ascii=False).encode("utf-8")
    return STATE_DIR / (hashlib.sha256(key).hexdigest() + ".json")


def endpoint_fingerprint(connection):
    # Detect a name being reassigned to another machine while SSH is still running.
    payload = [connection.host, connection.port, connection.username]
    return hashlib.sha256(json.dumps(payload).encode("utf-8")).hexdigest()


def find_server(session):
    matches = []
    for process in psutil.process_iter(["name"]):
        if (process.info["name"] or "").lower() != "zellij.exe":
            continue
        try:
            args = process.cmdline()
            if "--server" in args and Path(args[args.index("--server") + 1]).name == session:
                matches.append(identity(process.pid))
        except (psutil.Error, IndexError):
            continue
    if len(matches) != 1:
        raise RuntimeError("无法唯一确定当前 Zellij 服务进程。")
    return matches[0]


def register_connection(connection, ssh_pid):
    """Persist one record per pane; no password is copied into the state file."""
    session = os.environ.get("ZELLIJ_SESSION_NAME")
    pane = os.environ.get("ZELLIJ_PANE_ID")
    if not session or pane is None:
        return None
    pane = pane_number(pane)
    record = {
        "version": 1, "token": uuid.uuid4().hex,
        "session": session, "pane": pane, "connection_name": connection.name,
        "endpoint": endpoint_fingerprint(connection),
        "manager": identity(os.getpid()), "ssh": identity(ssh_pid),
        "server": find_server(session),
    }
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    target = state_path(session, pane)
    temporary = target.with_suffix("." + record["token"] + ".tmp")
    try:
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        # Cleanup briefly holds a Windows file handle while deleting an old generation.
        # Retry sharing conflicts so a simultaneous new SSH registration can complete.
        for attempt in range(20):
            try:
                os.replace(temporary, target)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.05)
        # Explicit names take precedence over remote shells' OSC title updates.
        zellij_action(session, "rename-pane", "--pane-id", str(pane), "--", connection.name)
    except Exception:
        temporary.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
        raise
    return record


def unregister_connection(record):
    """Only remove our own generation, even if the pane has already reconnected."""
    if not record:
        return
    target = state_path(record["session"], record["pane"])
    try:
        current = json.loads(target.read_text(encoding="utf-8"))
        if current.get("token") != record["token"]:
            return
        target.unlink(missing_ok=True)
        if identity_alive(record["server"]):
            zellij_action(record["session"], "undo-rename-pane", "--pane-id", str(record["pane"]))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        pass  # A closed pane/server does not prevent returning to the picker.


def record_is_valid(record, connections):
    """Check saved ownership; ambiguous process access raises instead of deleting."""
    if not isinstance(record, dict):
        return False
    if record.get("version") != 1:
        raise RuntimeError("无法识别关联记录版本。")
    if (not isinstance(record.get("session"), str) or not record["session"]
            or type(record.get("pane")) is not int or record["pane"] < 0
            or not isinstance(record.get("token"), str) or not record["token"]):
        return False
    for key in ("manager", "ssh", "server"):
        saved = record.get(key)
        if (not isinstance(saved, dict) or type(saved.get("pid")) is not int
                or saved["pid"] <= 0 or not isinstance(saved.get("created"), (int, float))):
            return False
        try:
            if identity(saved["pid"]) != saved:
                return False
        except psutil.NoSuchProcess:
            return False
        # AccessDenied and other uncertain errors must be handled as skipped files.
    connection = next((c for c in connections if c.name == record.get("connection_name")), None)
    if connection is None or endpoint_fingerprint(connection) != record.get("endpoint"):
        return False
    return record["server"] == find_server(record["session"])


def delete_unchanged_record(path, original):
    """Delete the checked open file, never a replacement registered at the same path."""
    import msvcrt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = [wintypes.HANDLE], wintypes.BOOL
    kernel.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                 ctypes.c_void_p, wintypes.DWORD]
    kernel.SetFileInformationByHandle.restype = wintypes.BOOL
    # GENERIC_READ | DELETE, FILE_SHARE_READ, OPEN_EXISTING.
    # Deny write/replacement until the checked generation is deleted and closed.
    handle = kernel.CreateFileW(str(path), 0x80010000, 1, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    with os.fdopen(descriptor, "rb") as stream:
        if stream.read() != original:
            return False
        # FileDispositionInfo removes only this open file when its handle closes.
        delete = wintypes.BOOL(True)
        if not kernel.SetFileInformationByHandle(handle, 4, ctypes.byref(delete), ctypes.sizeof(delete)):
            raise ctypes.WinError(ctypes.get_last_error())
    return True


def cleanup_invalid_records():
    """Remove only invalid association JSON files, without touching sessions or SSH."""
    from ssh_manager import load_connections

    connections = load_connections()
    counts = {"deleted": 0, "kept": 0, "skipped": 0}
    if not STATE_DIR.exists():
        return counts
    for path in STATE_DIR.iterdir():
        # Only xxx's hashed JSON records are owned here; ignore temporary/foreign files.
        if not re.fullmatch(r"[0-9a-f]{64}\.json", path.name):
            continue
        try:
            if path.is_symlink() or not path.is_file():
                counts["skipped"] += 1
                continue
            original = path.read_bytes()
            try:
                record = json.loads(original)
            except (ValueError, UnicodeError):
                valid = False
            else:
                valid = record_is_valid(record, connections)
                if valid and state_path(record["session"], record["pane"]) != path:
                    valid = False
            if valid:
                counts["kept"] += 1
            elif delete_unchanged_record(path, original):
                counts["deleted"] += 1
            else:
                counts["skipped"] += 1
        except (OSError, ValueError, RuntimeError, psutil.Error):
            counts["skipped"] += 1
    return counts


def alacritty_process():
    for process in psutil.Process().parents():
        if process.name().lower() == "alacritty.exe":
            return process
    raise RuntimeError("请在 Alacritty 中按 Ctrl+Shift+Alt+E 使用此功能。")


def window_session(window_pid):
    """Read the live Zellij client's environment, including after session switches."""
    window = psutil.Process(window_pid)
    if window.name().lower() != "alacritty.exe":
        raise RuntimeError("快捷键来源不是 Alacritty 窗口。")
    sessions = set()
    for process in window.children(recursive=True):
        try:
            if process.name().lower() != "zellij.exe" or "--server" in process.cmdline():
                continue
            ancestors = process.parents()
            # Exclude nested Zellij instances inside panes of the outer client.
            before_window = ancestors[:next(i for i, p in enumerate(ancestors) if p.pid == window.pid)]
            if any(p.name().lower() == "zellij.exe" for p in before_window):
                continue
            session = process.environ().get("ZELLIJ_SESSION_NAME")
            if session:
                sessions.add(session)
        except (psutil.Error, StopIteration):
            continue
    if len(sessions) != 1:
        raise RuntimeError("无法唯一确定当前窗口的 Zellij 会话。")
    return sessions.pop()


def focused_pane(session):
    # is_focused in list-panes is per tab, so use the actual attached clients instead.
    clients = []
    for line in zellij_action(session, "list-clients").splitlines():
        match = re.match(r"^\s*\d+\s+((?:terminal|plugin)_\d+)(?:\s|$)", line)
        if match:
            clients.append(match.group(1))
    if not clients or len(set(clients)) != 1:
        raise RuntimeError("该会话有多个客户端且焦点不同，无法确定当前 SSH；请使用独立会话。")
    return pane_number(clients[0])


def resolve_connection(window_pid):
    session = window_session(window_pid)
    pane = focused_pane(session)
    return session, pane, connection_for_pane(session, pane)


def connection_for_pane(session, pane):
    """Resolve a specified pane without relying on whichever pane is now focused."""
    from ssh_manager import load_connections

    pane = pane_number(pane)
    try:
        record = json.loads(state_path(session, pane).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise RuntimeError("当前窗格没有 xxx 的 SSH 连接记录，请退出旧版 xxx 并重新运行、连接。") from None
    if (not isinstance(record, dict) or record.get("version") != 1
            or record.get("session") != session or record.get("pane") != pane
            or not all(identity_alive(record.get(key)) for key in ("manager", "ssh", "server"))
            or record.get("server") != find_server(session)):
        raise RuntimeError("当前窗格的 SSH 连接已结束，请通过 xxx 重新连接。")
    connection = next((c for c in load_connections() if c.name == record.get("connection_name")), None)
    if not connection or endpoint_fingerprint(connection) != record.get("endpoint"):
        raise RuntimeError("当前连接已删除或目标地址、端口、用户名已改变，请重新连接。")
    return connection


def read_clipboard():
    """Read CF_UNICODETEXT without modifying the clipboard or copying a password."""
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    user.OpenClipboard.argtypes, user.OpenClipboard.restype = [wintypes.HWND], wintypes.BOOL
    user.CloseClipboard.argtypes, user.CloseClipboard.restype = [], wintypes.BOOL
    user.GetClipboardData.argtypes, user.GetClipboardData.restype = [wintypes.UINT], wintypes.HANDLE
    kernel.GlobalLock.argtypes, kernel.GlobalLock.restype = [wintypes.HGLOBAL], wintypes.LPVOID
    kernel.GlobalUnlock.argtypes, kernel.GlobalUnlock.restype = [wintypes.HGLOBAL], wintypes.BOOL
    for _ in range(20):
        if user.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        raise RuntimeError("无法读取剪贴板，请稍后重试。")
    try:
        handle = user.GetClipboardData(13)
        pointer = kernel.GlobalLock(handle) if handle else None
        if not pointer:
            raise RuntimeError("剪贴板中没有文本，请先复制远程路径。")
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel.GlobalUnlock(handle)
    finally:
        user.CloseClipboard()


def remote_path(text):
    """Require an explicit remote absolute path; never interpret copied text as code."""
    value = text.strip()
    if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
        value = value[1:-1]
    if (not value.startswith("/") or value.startswith("//") or len(value) > 32760
            or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value)):
        raise RuntimeError("请先复制单行远程绝对路径（例如 /home/ddw），再按快捷键。")
    return value


def winscp_arguments(connection, path, password_file=None):
    host = f"[{connection.host}]" if ":" in connection.host else connection.host
    # RemoteDirectory is a literal directory, not a shell command or a download URL.
    args = [str(WINSCP), f"sftp://{quote(connection.username, safe='')}@{host}:{connection.port}/",
            "/newinstance", f"/sessionname={connection.name}"]
    if password_file:
        args += ["/passwordsfromfiles", f"/password={password_file}"]
    args += ["/rawsettings", f"RemoteDirectory={path}"]
    return args


def winscp_commandline(arguments):
    # WinSCP documents doubled double-quotes, unlike the C runtime's \" escape.
    return " ".join('"' + argument.replace('"', '""') + '"' for argument in arguments)


def launch_winscp(connection, path):
    """Keep credentials out of argv; remove the temporary password on process exit."""
    if not WINSCP.is_file():
        raise RuntimeError("未找到本机 WinSCP，请检查安装路径。")
    password_file = None
    try:
        if connection.password:
            if "\n" in connection.password or "\r" in connection.password:
                raise RuntimeError("WinSCP 密码文件不支持含换行的密码。")
            fd, name = tempfile.mkstemp(prefix="xxx-winscp-", suffix=".tmp")
            password_file = Path(name)
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                stream.write(connection.password)
        arguments = winscp_arguments(connection, path, password_file)
        process = subprocess.Popen(winscp_commandline(arguments), executable=str(WINSCP),
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        # The shortcut runs under pythonw, so waiting leaves no terminal window.
        process.wait()
    finally:
        if password_file:
            password_file.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="Alacritty 当前 SSH 窗格的 WinSCP 快捷键")
    parser.add_argument("--inspect", action="store_true", help="仅检查会话和连接映射，不读密码到输出、不启动 WinSCP")
    parser.add_argument("--window-pid", type=int, help="诊断时指定 Alacritty 进程")
    args = parser.parse_args()
    try:
        window_pid = args.window_pid or alacritty_process().pid
        session, pane, connection = resolve_connection(window_pid)
        if args.inspect:
            print(json.dumps({"session": session, "pane": pane, "name": connection.name}, ensure_ascii=False))
            return 0
        path = remote_path(read_clipboard())
        launch_winscp(connection, path)
        return 0
    except (RuntimeError, OSError, ValueError, psutil.Error, subprocess.SubprocessError):
        # Avoid printing subprocess commands, clipboard contents or credentials.
        error = sys.exc_info()[1]
        message = str(error) if isinstance(error, RuntimeError) else "快捷键执行失败，请检查 Zellij、配置文件及 WinSCP 安装。"
        if args.inspect:
            print(message, file=sys.stderr)
        else:
            ctypes.windll.user32.MessageBoxW(None, message, "xxx — WinSCP", 0x10)
        return 1


if __name__ == "__main__":
    sys.exit(main())
