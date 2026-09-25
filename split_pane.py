"""Zellij split dispatcher, briefly run in place of the source pane (never replacing it)."""
import argparse
import base64
import ctypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import ssh_integration as bridge


def source_candidates(panes, helper_id):
    """In-place Run suppresses the exact source at the helper's geometry in its tab."""
    helper = next((p for p in panes if not p['is_plugin'] and p['id'] == helper_id), None)
    if helper is None:
        raise RuntimeError('拆分窗格尚未就绪，请重试。')
    geometry = ('tab_id', 'pane_x', 'pane_y', 'pane_rows', 'pane_columns')
    candidates = [p for p in panes if not p['is_plugin'] and p.get('is_suppressed')
                  and not p.get('exited') and all(p.get(k) == helper.get(k) for k in geometry)]
    if not candidates:
        raise RuntimeError('无法识别拆分来源，已取消操作。')
    return candidates


def source_pane(panes, helper_id):
    candidates = source_candidates(panes, helper_id)
    if len(candidates) != 1:
        raise RuntimeError('无法唯一识别拆分来源，已取消操作；请先退出嵌套的临时窗格再重试。')
    return candidates[0]


def split(session, helper_id, direction):
    """Capture the source, then defer splitting until the temporary pane has closed."""
    if direction not in ('auto', 'right', 'down', 'stacked'):
        raise ValueError('拆分方向无效。')
    # Wait for Zellij to finish inserting this temporary pane before inspecting it.
    for attempt in range(10):
        try:
            panes = json.loads(bridge.zellij_action(session, 'list-panes', '--json'))
            candidates = source_candidates(panes, helper_id)
            break
        except (ValueError, RuntimeError):
            if attempt == 9:
                raise
            time.sleep(0.1)
    snapshots = []
    for source in candidates:
        connection = None
        if bridge.state_path(session, source['id']).is_file():
            connection = bridge.connection_for_pane(session, source['id'])
        snapshots.append(dict(id=source['id'], connection_name=connection.name if connection else None))
    request = dict(session=session, source=candidates[0]['id'], helper=helper_id, direction=direction,
                   tab=candidates[0]['tab_id'], server=bridge.find_server(session),
                   connection_name=snapshots[0]['connection_name'], candidates=snapshots)
    # Commands launched with --near-current-pane can safely target the restored source.
    # Stack-list members are also marked suppressed, so resolve the active member
    # after the helper closes instead of guessing between identical geometries.
    token = base64.urlsafe_b64encode(json.dumps(request).encode()).decode('ascii')
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--finish', token],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
    return request


def finish_split(request):
    """Wait for source restoration, validate again, and split relative to that pane."""
    session = request['session']
    snapshots = request.get('candidates', [dict(id=request['source'], connection_name=request['connection_name'])])
    candidate_ids = {c['id'] for c in snapshots}
    for attempt in range(40):
        if not bridge.identity_alive(request['server']):
            raise RuntimeError('原 Zellij 会话已结束，已取消拆分。')
        panes = json.loads(bridge.zellij_action(session, 'list-panes', '--json'))
        visible = [p for p in panes if not p['is_plugin'] and p['id'] in candidate_ids
                   and not p.get('is_suppressed')]
        helper = next((p for p in panes if not p['is_plugin'] and p['id'] == request['helper']), None)
        if len(visible) == 1 and helper is None:
            source = visible[0]
            source_id = source['id']
            break
        time.sleep(0.1)
    else:
        raise RuntimeError('原窗格未恢复，已取消拆分。')
    if source.get('exited') or source['tab_id'] != request['tab']:
        raise RuntimeError('原窗格状态已改变，已取消拆分。')
    connection = None
    if bridge.state_path(session, source_id).is_file():
        connection = bridge.connection_for_pane(session, source_id)
    expected_name = next(c['connection_name'] for c in snapshots if c['id'] == source_id)
    if (connection.name if connection else None) != expected_name:
        raise RuntimeError('原窗格的 SSH 连接已改变，已取消拆分。')
    direction = request['direction']
    # --near-current-pane anchors to the source's tab/pane via ZELLIJ_PANE_ID.
    # Zellij rejects combining it with --tab-id.
    arguments = ['new-pane', '--near-current-pane']
    if direction in ('right', 'down'):
        arguments += ['--direction', direction]
    elif direction == 'stacked':
        # Build a normal peer, then group the two explicit pane IDs into a stack.
        arguments += ['--direction', 'down']
    if connection:
        token = base64.urlsafe_b64encode(connection.name.encode('utf-8')).decode('ascii')
        arguments += ['--name', connection.name, '--close-on-exit', '--', sys.executable,
                      str(bridge.ROOT / 'ssh_manager.py'), '--connect-in-pane', token]
    created = bridge.zellij_action(session, *arguments, source_pane_id=source_id).strip()
    if direction == 'stacked':
        bridge.zellij_action(session, 'stack-panes', '--', f'terminal_{source_id}', created)
    return created


def main():
    parser = argparse.ArgumentParser(description='拆分当前 Zellij 窗格，继承 xxx SSH 连接目标')
    parser.add_argument('direction', nargs='?', choices=('auto', 'right', 'down', 'stacked'))
    parser.add_argument('--finish', help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.finish:
            request = json.loads(base64.b64decode(args.finish, altchars=b'-_', validate=True))
            finish_split(request)
            return 0
        if not args.direction:
            raise RuntimeError('未指定拆分方向。')
        if not bridge.in_zellij():
            raise RuntimeError('此命令仅供 Zellij 拆分快捷键使用。')
        split(os.environ['ZELLIJ_SESSION_NAME'], bridge.pane_number(os.environ['ZELLIJ_PANE_ID']), args.direction)
        return 0
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError, bridge.psutil.Error) as error:
        message = str(error) if isinstance(error, RuntimeError) else '拆分失败，请检查会话和连接配置。'
        if args.finish:
            ctypes.windll.user32.MessageBoxW(None, message, 'xxx — 拆分窗格', 0x10)
            return 1
        print(message, flush=True)
        if sys.stdin.isatty():
            try:
                input('按 Enter 返回原窗格。')
            except (EOFError, KeyboardInterrupt):
                pass
        return 1


if __name__ == '__main__':
    sys.exit(main())
