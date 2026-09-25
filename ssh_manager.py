"""Windows SSH picker. Its only configuration source is CONFIG_PATH."""
from __future__ import annotations

import argparse
import base64
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

CONFIG_PATH = Path(r"C:\Users\ghoul\ssh_config\config.json")


@dataclass(frozen=True)
class Connection:
    name: str
    host: str
    port: int
    username: str
    password: str | None = field(default=None, repr=False)


def load_connections() -> list[Connection]:
    """Validate the fixed JSON file without including credentials in errors."""
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, UnicodeError):
        raise ValueError(f"无法读取配置，请检查 JSON 格式及文件权限：{CONFIG_PATH}") from None
    if (not isinstance(data, dict) or type(data.get("version")) is not int
            or data["version"] != 1 or not isinstance(data.get("connections"), list)):
        raise ValueError("配置必须包含 version: 1 和 connections 数组。")
    connections, seen = [], set()
    for index, entry in enumerate(data["connections"], 1):
        if not isinstance(entry, dict):
            raise ValueError(f"第 {index} 条连接必须是对象。")
        for key in ("name", "host", "username"):
            value = entry.get(key)
            if (not isinstance(value, str) or not value.strip()
                    or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value)):
                raise ValueError(f"第 {index} 条连接的 {key} 无效。")
        if (not re.fullmatch(r"[^\W_][\w.:%-]*", entry["host"])
                or any(c.isspace() for c in entry["username"])):
            raise ValueError(f"第 {index} 条连接的地址或用户名无效。")
        if entry["name"] in seen:
            raise ValueError(f"第 {index} 条连接的名称重复。")
        port = entry.get("port", 22)
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError(f"第 {index} 条连接的端口必须是 1 到 65535 的整数。")
        password = entry.get("password")
        if password is not None and (not isinstance(password, str) or "\0" in password):
            raise ValueError(f"第 {index} 条连接的密码必须为不含 NUL 的字符串或 null。")
        seen.add(entry["name"])
        connections.append(Connection(entry["name"], entry["host"], port, entry["username"], password))
    return connections


def fuzzy_score(text: str, query: str) -> int | None:
    """Match ordered characters; prefer consecutive matches and word starts."""
    text, needle = text.casefold(), "".join(query.split()).casefold()
    previous, score = -1, 0
    for character in needle:
        position = text.find(character, previous + 1)
        if position < 0:
            return None
        score += 10 + (12 if position == previous + 1 else 0)
        if position == 0 or text[position - 1] in "-_.: @":
            score += 8
        score -= position - previous - 1
        previous = position
    return score


def filter_connections(connections: list[Connection], query: str) -> list[Connection]:
    """Search names, addresses and users, never the password field."""
    if not query.strip():
        return list(connections)
    ranked = []
    for index, connection in enumerate(connections):
        name_score = fuzzy_score(connection.name, query)
        other_score = fuzzy_score(f"{connection.username}@{connection.host}:{connection.port}", query)
        scores = ([name_score + 100] if name_score is not None else [])
        if other_score is not None:
            scores.append(other_score)
        if scores:
            ranked.append((-max(scores), len(connection.name), index, connection))
    return [item[-1] for item in sorted(ranked)]


def copy_password(password: str) -> None:
    """Write Unicode text with a hidden clipboard-owner window and transfer ownership."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                      wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                      ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                      wintypes.HINSTANCE, wintypes.LPVOID]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.DestroyWindow.restype = wintypes.BOOL
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL

    # A real owner handle is required for EmptyClipboard + SetClipboardData.
    window = user32.CreateWindowExW(0, "STATIC", "xxx clipboard", 0, 0, 0, 0, 0,
                                   None, None, None, None)
    memory, opened = None, False
    try:
        if not window:
            raise OSError("clipboard owner unavailable")
        content = password.encode("utf-16-le") + b"\0\0"
        memory = kernel32.GlobalAlloc(0x0002, len(content))  # GMEM_MOVEABLE
        pointer = kernel32.GlobalLock(memory) if memory else None
        if not pointer:
            raise OSError("clipboard allocation failed")
        try:
            ctypes.memmove(pointer, content, len(content))
        finally:
            kernel32.GlobalUnlock(memory)
        # Other applications may briefly hold the clipboard; retry for one second.
        for _ in range(20):
            if user32.OpenClipboard(window):
                opened = True
                break
            time.sleep(0.05)
        if not opened or not user32.EmptyClipboard():
            raise OSError("clipboard unavailable")
        if not user32.SetClipboardData(13, memory):  # CF_UNICODETEXT
            raise OSError("clipboard write failed")
        memory = None  # The system now owns this allocation.
    except (OSError, UnicodeError):
        raise RuntimeError("密码复制到系统剪贴板失败，未启动 SSH。") from None
    finally:
        if opened:
            user32.CloseClipboard()
        if memory:
            kernel32.GlobalFree(memory)
        if window:
            user32.DestroyWindow(window)


def ssh_arguments(connection: Connection, executable: str) -> list[str]:
    """Build argument boundaries directly; passwords never reach a shell or argv."""
    return [executable, "-F", "none", "-p", str(connection.port),
            "-l", connection.username, "--", connection.host]


def connect(connection: Connection, executable: str) -> int:
    """Copy before spawning SSH, preserving terminal input/output for authentication."""
    if connection.password:
        copy_password(connection.password)
        print(f"[{connection.name}] 密码已复制，请在 SSH 密码提示处粘贴。", flush=True)
    else:
        print(f"[{connection.name}] 未保存密码；使用密钥或手动输入，剪贴板未更新。", flush=True)
    process = subprocess.Popen(ssh_arguments(connection, executable))
    record = None
    try:
        from ssh_integration import register_connection, unregister_connection
        try:
            record = register_connection(connection, process.pid)
        except Exception:
            print("提示：Zellij 窗格关联失败，本次连接无法使用 WinSCP 快捷键。", flush=True)
        while True:
            try:
                return process.wait()
            except KeyboardInterrupt:
                # The child shares the console and handles Ctrl+C; keep the picker alive.
                continue
    finally:
        if record is not None:
            unregister_connection(record)


@dataclass
class SelectionState:
    query: str = ""
    index: int = 0


def make_selector(connections: list[Connection], state: SelectionState, status: str):
    """Render a responsive connection browser without ever rendering credentials."""
    try:
        from prompt_toolkit import Application
        from prompt_toolkit.application.current import get_app
        from prompt_toolkit.data_structures import Point
        from prompt_toolkit.filters import Condition
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, VSplit, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.layout.dimension import Dimension
        from prompt_toolkit.layout.margins import ScrollbarMargin
        from prompt_toolkit.styles import Style
        from prompt_toolkit.utils import get_cwidth
        from prompt_toolkit.widgets import Frame, TextArea
    except ImportError:
        raise RuntimeError("缺少 prompt_toolkit，请运行 C:\\Python312\\python.exe -m pip install prompt_toolkit") from None

    matches = filter_connections(connections, state.query)
    state.index = min(state.index, max(0, len(matches) - 1))
    search = TextArea(text=state.query, height=1, prompt=[("class:accent", " / ")],
                      multiline=False, wrap_lines=False, style="class:search")
    bindings = KeyBindings()

    def terminal_size():
        return get_app().output.get_size()

    def fit(text, width):
        """Pad by terminal cells so CJK names align with ASCII addresses."""
        if get_cwidth(text) <= width:
            return text + " " * (width - get_cwidth(text))
        result, used = "", 0
        for character in text:
            size = get_cwidth(character)
            if used + size > width - 1:
                break
            result += character
            used += size
        return result + "…" + " " * max(0, width - used - 1)

    def content_width():
        return max(1, min(144, terminal_size().columns - 2))

    def show_details():
        return terminal_size().columns >= 112 and terminal_size().rows >= 26

    def dimensions():
        # Account for borders, scrollbar and the optional fixed-width sidebar
        # before the first render, including immediately after terminal resize.
        width = max(1, content_width() - 3 - (36 if show_details() else 0))
        name_width = max(10, int((width - 5) * 0.46)) if width >= 52 else width - 3
        return width, name_width, max(0, width - name_width - 5)

    def matched(text, base):
        remaining = "".join(state.query.split()).casefold()
        fragments = []
        for character in text:
            hit = bool(remaining) and character.casefold() == remaining[0]
            fragments.append((base + (" class:match" if hit else ""), character))
            if hit:
                remaining = remaining[1:]
        return fragments

    def refresh(_buffer):
        nonlocal matches
        state.query, state.index = search.text, 0
        matches = filter_connections(connections, state.query)

    search.buffer.on_text_changed += refresh

    def rows():
        if not matches:
            return [("class:muted", "\n  没有匹配的连接\n\n  试试更短的名称、地址或用户名。\n  Ctrl+U 清空搜索。")]
        fragments = []
        width, name_width, target_width = dimensions()
        for index, connection in enumerate(matches):
            selected = index == state.index
            style = "class:selected" if selected else "class:row"
            fragments.append((style + (" class:accent" if selected else ""), " ▸ " if selected else "   "))
            fragments.extend(matched(fit(connection.name, name_width), style))
            if target_width:
                fragments.append((style, "  "))
                target = f"{connection.username}@{connection.host}:{connection.port}"
                fragments.extend(matched(fit(target, target_width), style + ("" if selected else " class:muted")))
            else:
                fragments.append((style, " " * max(0, width - name_width - 3)))
            if index < len(matches) - 1:
                fragments.append(("", "\n"))
        return fragments

    control = FormattedTextControl(rows, get_cursor_position=lambda: Point(0, state.index))
    list_window = Window(control, wrap_lines=False, always_hide_cursor=True,
                         right_margins=[ScrollbarMargin(display_arrows=True)], style="class:panel")

    def heading():
        _, name_width, target_width = dimensions()
        text = "   " + fit("连接名称", name_width)
        return [("class:column", text + ("  远程地址" if target_width else ""))]

    def detail():
        if not matches:
            return [("class:muted", "\n  暂无选中连接")]
        connection = matches[state.index]
        return [
            ("class:eyebrow", "\n  当前选择\n\n"),
            ("class:detail-name", f"  {connection.name}\n\n"),
            ("class:muted", "  主机\n"), ("class:detail-value", f"  {connection.host}\n\n"),
            ("class:muted", "  用户\n"), ("class:detail-value", f"  {connection.username}\n\n"),
            ("class:muted", "  端口\n"), ("class:detail-value", f"  {connection.port}\n\n"),
            ("class:muted", "  认证\n"),
            ("class:accent" if connection.password else "class:warning",
             "  已保存密码\n" if connection.password else "  未保存密码\n"),
            ("class:muted", "\n  " + ("Enter 在新标签页连接" if os.environ.get("ZELLIJ_SESSION_NAME") else "Enter 在当前终端连接")),
        ]

    def summary():
        return [("class:muted", "  CONNECTIONS  "), ("class:accent", f"{len(matches):02d}"),
                ("class:muted", f" / {len(connections):02d}"),
                ("class:muted", f"    {state.index + 1:02d}" if matches else "    00")]

    def footer():
        compact = content_width() < 80
        entries = [("Enter", "连接"), ("↑↓", "选择"), ("Esc", "退出")]
        if not compact:
            entries = [("Enter", "连接"), ("↑↓", "选择"), ("PgUp/PgDn", "翻页"), ("Ctrl+U", "清空"), ("Esc", "退出")]
        fragments = [("", "  ")]
        for key, label in entries:
            fragments.extend([("class:key", f" {key} "), ("class:muted", f" {label}   ")])
        return fragments

    def message():
        if status:
            return [("class:notice", "  " + status)]
        if matches:
            connection = matches[state.index]
            return [("class:muted", f"  {connection.username}@{connection.host}:{connection.port}")]
        return [("class:muted", "  输入名称、地址或用户名进行模糊搜索")]

    def move(offset):
        state.index = max(0, min(state.index + offset, len(matches) - 1))

    @bindings.add("up")
    def up(event):
        move(-1)

    @bindings.add("down")
    def down(event):
        move(1)

    @bindings.add("pageup")
    def pageup(event):
        move(-(list_window.render_info.window_height if list_window.render_info else 10))

    @bindings.add("pagedown")
    def pagedown(event):
        move(list_window.render_info.window_height if list_window.render_info else 10)

    @bindings.add("c-u")
    def clear(event):
        search.text = ""

    @bindings.add("enter")
    def select(event):
        if matches:
            event.app.exit(result=matches[state.index].name)

    @bindings.add("escape", eager=True)
    @bindings.add("c-c")
    def quit_picker(event):
        event.app.exit(result=None)

    body = VSplit([
        Frame(HSplit([Window(FormattedTextControl(heading), height=1), list_window]),
              title=" 连接列表 ", style="class:frame"),
        ConditionalContainer(VSplit([
            Window(width=2),
            Frame(Window(FormattedTextControl(detail), wrap_lines=True, width=32),
                  title=" 连接信息 ", style="class:frame"),
        ]), filter=Condition(show_details)),
    ])
    content = HSplit([
        ConditionalContainer(Window(height=1), filter=Condition(lambda: terminal_size().rows >= 24)),
        Window(FormattedTextControl([("class:brand", "  xxx "), ("class:header", " / SSH CONNECTIONS")]), height=1),
        ConditionalContainer(Window(FormattedTextControl("  选择连接，在独立标签页中打开" if os.environ.get("ZELLIJ_SESSION_NAME")
                                                        else "  选择连接，在当前终端中打开"), height=1, style="class:muted"),
                             filter=Condition(lambda: terminal_size().rows >= 20)),
        Frame(search, title=" 搜索 · 名称 / 地址 / 用户 ", style="class:search-frame"),
        Window(FormattedTextControl(summary), height=1),
        body,
        Window(FormattedTextControl(message), height=Dimension(min=1, max=2), wrap_lines=True),
        Window(FormattedTextControl(footer), height=1),
        ConditionalContainer(Window(height=1), filter=Condition(lambda: terminal_size().rows >= 24)),
    ], width=lambda: Dimension.exact(content_width()))
    layout = VSplit([Window(width=Dimension(min=0, preferred=1, weight=1)), content,
                     Window(width=Dimension(min=0, preferred=1, weight=1))])
    return Application(layout=Layout(layout, focused_element=search), key_bindings=bindings,
                       full_screen=True, mouse_support=False,
                       style=Style.from_dict({
                           "": "bg:#171d2b #d5deec", "brand": "bg:#72dfc6 #101b23 bold",
                           "header": "#e9eef7 bold", "muted": "#8292ab", "accent": "#72dfc6",
                           "panel": "bg:#1c2433", "frame.border": "#34445e", "frame.label": "#a9b9d1",
                           "search": "bg:#222e40 #f1f5fb", "search-frame.border": "#4f948f",
                           "search-frame.label": "#72dfc6", "column": "bg:#222e40 #91a4be",
                           "row": "bg:#1c2433 #d5deec", "selected": "bg:#2c4654 #f1fcf9 bold",
                           "match": "#f5d58d bold", "eyebrow": "#8292ab",
                           "detail-name": "#72dfc6 bold", "detail-value": "#e2eaf7",
                           "warning": "#e8bd7f", "key": "bg:#29374b #ccd9ea bold", "notice": "#e8bd7f",
                           "scrollbar.background": "bg:#222e40", "scrollbar.button": "bg:#53708c",
                           "scrollbar.arrow": "#8292ab",
                       }))


def run_picker(executable: str) -> int:
    """Keep the manager alive while Zellij starts independent SSH tabs."""
    from ssh_integration import cleanup_invalid_records, in_zellij, open_ssh_tab, picker_title

    with picker_title():
        state, status = SelectionState(), ""
        # Clean once per interactive startup, never on each return to the selector
        # or in SSH child workers. A maintenance failure must not block connections.
        try:
            result = cleanup_invalid_records()
            status = f"自动清理 {result['deleted']}，有效 {result['kept']}，跳过 {result['skipped']}"
        except (OSError, ValueError, RuntimeError):
            status = "自动清理失败，请检查配置文件和关联目录的访问权限。"
        while True:
            connections = load_connections()
            name = make_selector(connections, state, status).run()
            if name is None:
                return 0
            try:
                connection = next((c for c in load_connections() if c.name == name), None)
                if connection is None:
                    raise ValueError("选中的连接已从配置删除。")
                if in_zellij():
                    open_ssh_tab(connection.name)
                    status = f"已在新标签页打开：{connection.name}"
                else:
                    status = f"上次 SSH 退出码: {connect(connection, executable)}"
            except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
                status = str(error)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=f"SSH 模糊选择器，固定读取 {CONFIG_PATH}")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--list", action="store_true", help="仅列出连接，不显示密码")
    mode.add_argument("--name", help="按配置中的完整名称单次连接")
    mode.add_argument("--check", action="store_true", help="仅检查固定 JSON 配置，不建立连接")
    mode.add_argument("--connect-in-pane", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        connections = load_connections()
        if args.check:
            print(f"配置有效：{len(connections)} 个连接。")
            return 0
        if args.list:
            for connection in connections:
                print(f"{connection.name}\t{connection.username}@{connection.host}:{connection.port}")
            return 0
        executable = shutil.which("ssh.exe")
        if not executable:
            raise RuntimeError("未找到 OpenSSH 客户端 ssh.exe，请检查 PATH。")
        if args.name is not None or args.connect_in_pane is not None:
            name = args.name
            if args.connect_in_pane is not None:
                try:
                    name = base64.b64decode(args.connect_in_pane, altchars=b"-_", validate=True).decode("utf-8")
                except (ValueError, UnicodeError):
                    raise ValueError("SSH 标签页的连接名称参数无效。") from None
            connection = next((c for c in connections if c.name == name), None)
            if connection is None:
                raise ValueError("配置中没有该名称的连接。")
            from ssh_integration import in_zellij, open_ssh_tab
            if args.connect_in_pane is None and in_zellij():
                open_ssh_tab(connection.name)
                return 0
            code = connect(connection, executable)
            if code and args.connect_in_pane is not None and sys.stdin.isatty():
                input(f"SSH 已退出（退出码 {code}），按 Enter 关闭此标签页。")
            return code
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise RuntimeError("交互模式需要真实终端；请使用 xxx --list 或 xxx --check 查看配置。")
        return run_picker(executable)
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        return 0


if __name__ == "__main__":
    if os.name != "nt":
        print("本工具仅支持 Windows。", file=sys.stderr)
        sys.exit(1)
    sys.exit(main())
