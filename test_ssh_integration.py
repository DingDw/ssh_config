"""Synthetic tests: no real credentials, clipboard changes, or remote connections."""
import contextlib
import base64
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import ssh_integration as bridge
import ssh_manager as app


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state_patch = patch.object(bridge, 'STATE_DIR', Path(self.directory.name))
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        self.connection = app.Connection('测试 "alpha"', '192.0.2.10', 2202, 'user@example', 'synthetic 密码@"&% ')
        self.live = bridge.identity(os.getpid())

    def register(self, pane='3', name=None):
        connection = self.connection if name is None else app.Connection(name, '192.0.2.10', 2202, 'user@example')
        with patch.dict(os.environ, {'ZELLIJ_SESSION_NAME': 'session-test', 'ZELLIJ_PANE_ID': pane}), \
                patch.object(bridge, 'find_server', return_value=self.live), \
                patch.object(bridge, 'zellij_action') as action:
            record = bridge.register_connection(connection, os.getpid())
        action.assert_called_once_with('session-test', 'rename-pane', '--pane-id', pane, '--', connection.name)
        return record

    def test_state_is_per_pane_atomic_and_contains_no_password(self):
        a = self.register('3')
        b = self.register('4', 'second')
        self.assertNotEqual(bridge.state_path(a['session'], a['pane']), bridge.state_path(b['session'], b['pane']))
        data = bridge.state_path(a['session'], a['pane']).read_text(encoding='utf-8')
        self.assertNotIn('password', data)
        self.assertNotIn(self.connection.password, data)
        with patch.object(bridge, 'zellij_action') as action:
            bridge.unregister_connection(a)
        action.assert_called_once_with('session-test', 'undo-rename-pane', '--pane-id', '3')
        self.assertFalse(bridge.state_path('session-test', 3).exists())
        self.assertTrue(bridge.state_path('session-test', 4).exists())

    def test_old_cleanup_cannot_remove_new_connection(self):
        old = self.register()
        new = self.register(name='new')
        with patch.object(bridge, 'zellij_action') as action:
            bridge.unregister_connection(old)
        action.assert_not_called()
        self.assertEqual(json.loads(bridge.state_path('session-test', 3).read_text(encoding='utf-8'))['token'], new['token'])

    def test_cleanup_keeps_live_and_removes_dead_changed_and_corrupt_records(self):
        records = [self.register(str(pane)) for pane in range(3, 7)]
        dead = records[1]
        dead['ssh']['created'] -= 10
        bridge.state_path('session-test', 4).write_text(json.dumps(dead), encoding='utf-8')
        changed = records[2]
        changed['endpoint'] = 'outdated'
        bridge.state_path('session-test', 5).write_text(json.dumps(changed), encoding='utf-8')
        bridge.state_path('session-test', 6).write_bytes(b'{broken')
        foreign = bridge.STATE_DIR / 'notes.json'
        foreign.write_text('keep', encoding='utf-8')
        temporary = bridge.STATE_DIR / ('a' * 64 + '.tmp')
        temporary.write_text('in progress', encoding='utf-8')
        with patch.object(app, 'load_connections', return_value=[self.connection]), \
                patch.object(bridge, 'find_server', return_value=self.live), \
                patch.object(bridge, 'zellij_action') as action:
            self.assertEqual(bridge.cleanup_invalid_records(), {'deleted': 3, 'kept': 1, 'skipped': 0})
        self.assertTrue(bridge.state_path('session-test', 3).exists())
        self.assertTrue(foreign.exists())
        self.assertTrue(temporary.exists())
        action.assert_not_called()

    def test_cleanup_skips_access_denied_and_unknown_version(self):
        self.register('3')
        future = self.register('4')
        future['version'] = 2
        bridge.state_path('session-test', 4).write_text(json.dumps(future), encoding='utf-8')
        with patch.object(app, 'load_connections', return_value=[self.connection]), \
                patch.object(bridge, 'identity', side_effect=bridge.psutil.AccessDenied(123)):
            self.assertEqual(bridge.cleanup_invalid_records(), {'deleted': 0, 'kept': 0, 'skipped': 2})
        self.assertEqual(len(list(bridge.STATE_DIR.glob('*.json'))), 2)

    def test_cleanup_skips_generation_replaced_after_validation(self):
        self.register()
        target = bridge.state_path('session-test', 3)
        delete = bridge.delete_unchanged_record
        def replace_then_delete(path, original):
            self.register(name='new')
            return delete(path, original)
        with patch.object(app, 'load_connections', return_value=[]), \
                patch.object(bridge, 'delete_unchanged_record', side_effect=replace_then_delete):
            self.assertEqual(bridge.cleanup_invalid_records(), {'deleted': 0, 'kept': 0, 'skipped': 1})
        self.assertEqual(json.loads(target.read_text(encoding='utf-8'))['connection_name'], 'new')

    def test_cleanup_handle_blocks_replacement_until_delete_finishes(self):
        target = bridge.state_path('session-test', 3)
        target.write_bytes(b'old')
        replacement = bridge.STATE_DIR / 'new.tmp'
        replacement.write_bytes(b'new')
        fdopen = os.fdopen
        @contextlib.contextmanager
        def replace_after_read(fd, mode):
            with fdopen(fd, mode) as stream:
                def read():
                    data = stream.read()
                    with self.assertRaises(PermissionError):
                        os.replace(replacement, target)
                    return data
                yield Mock(read=read)
        with patch.object(bridge.os, 'fdopen', side_effect=replace_after_read):
            self.assertTrue(bridge.delete_unchanged_record(target, b'old'))
        self.assertFalse(target.exists())
        os.replace(replacement, target)
        self.assertEqual(target.read_bytes(), b'new')

    def test_registration_retries_cleanup_sharing_conflict(self):
        replace = os.replace
        calls = []
        def transient(source, target):
            calls.append(target)
            if len(calls) == 1:
                raise PermissionError('sharing conflict')
            return replace(source, target)
        with patch.object(bridge.os, 'replace', side_effect=transient), \
                patch.object(bridge.time, 'sleep'):
            record = self.register()
        self.assertEqual(len(calls), 2)
        self.assertEqual(json.loads(calls[-1].read_text(encoding='utf-8'))['token'], record['token'])

    def test_cleanup_bad_config_aborts_without_deleting(self):
        self.register()
        with patch.object(app, 'load_connections', side_effect=ValueError('bad config')):
            with self.assertRaises(ValueError):
                bridge.cleanup_invalid_records()
        self.assertTrue(bridge.state_path('session-test', 3).exists())

    def test_title_failure_removes_record(self):
        with patch.dict(os.environ, {'ZELLIJ_SESSION_NAME': 's', 'ZELLIJ_PANE_ID': '3'}), \
                patch.object(bridge, 'find_server', return_value=self.live), \
                patch.object(bridge, 'zellij_action', side_effect=RuntimeError('unavailable')):
            with self.assertRaises(RuntimeError):
                bridge.register_connection(self.connection, os.getpid())
        self.assertEqual(list(bridge.STATE_DIR.iterdir()), [])

    def test_focus_uses_client_not_per_tab_focus_and_rejects_ambiguity(self):
        for output, expected in [('CLIENT_ID ZELLIJ_PANE_ID RUNNING_COMMAND\n1 terminal_4 ssh\n', 4),
                                 ('1 terminal_3 ssh\n2 terminal_3 ssh\n', 3)]:
            with patch.object(bridge, 'zellij_action', return_value=output):
                self.assertEqual(bridge.focused_pane('s'), expected)
        for output in ('1 terminal_3 ssh\n2 terminal_4 ssh', '1 plugin_0 plugin', ''):
            with patch.object(bridge, 'zellij_action', return_value=output):
                with self.assertRaises(RuntimeError):
                    bridge.focused_pane('s')

    def test_resolve_refreshes_password_but_rejects_changed_endpoint_and_stale_pid(self):
        record = self.register()
        updated = app.Connection(self.connection.name, self.connection.host, self.connection.port,
                                 self.connection.username, 'new synthetic password')
        with patch.object(bridge, 'window_session', return_value='session-test'), \
                patch.object(bridge, 'focused_pane', return_value=3), \
                patch.object(bridge, 'find_server', return_value=self.live), \
                patch.object(app, 'load_connections', return_value=[updated]):
            self.assertEqual(bridge.resolve_connection(123)[2].password, updated.password)
            with patch.object(app, 'load_connections', return_value=[app.Connection(updated.name, '192.0.2.11', 22, 'other')]):
                with self.assertRaises(RuntimeError):
                    bridge.resolve_connection(123)
            record['ssh']['created'] -= 10
            bridge.state_path('session-test', 3).write_text(json.dumps(record), encoding='utf-8')
            with self.assertRaises(RuntimeError):
                bridge.resolve_connection(123)

    def test_paths_and_url_credentials_are_literal(self):
        path = '/home/中文 space/$(literal); % # "quote"'
        self.assertEqual(bridge.remote_path('"' + path + '"'), path)
        for bad in ('password', '', '//host/path', 'C:\\file', '/one\n/two', '/path\x1b[31m', '~/relative'):
            with self.assertRaises(RuntimeError):
                bridge.remote_path(bad)
        args = bridge.winscp_arguments(self.connection, path, 'C:\\temp file.txt')
        self.assertIn('sftp://user%40example@192.0.2.10:2202/', args)
        self.assertIn('RemoteDirectory=' + path, args)
        self.assertNotIn(self.connection.password, str(args))
        self.assertIn('""quote""', bridge.winscp_commandline(args))
        self.assertNotIn('/hostkey=*', args)

    def test_password_file_lifetime_and_cleanup_on_spawn_failure(self):
        observed = []
        def spawn(command, **kwargs):
            self.assertNotIn(self.connection.password, command)
            import re
            name = re.search(r'"/password=([^"]+)"', command).group(1)
            file = Path(name)
            observed.append(file)
            self.assertEqual(file.read_text(encoding='utf-8'), self.connection.password)
            return Mock(wait=Mock(side_effect=lambda: self.assertTrue(file.exists())))
        with patch.object(bridge.subprocess, 'Popen', side_effect=spawn):
            bridge.launch_winscp(self.connection, '/home/中文 space')
        self.assertFalse(observed[0].exists())
        original = bridge.tempfile.mkstemp
        created = []
        def capture(**kwargs):
            fd, name = original(**kwargs)
            created.append(Path(name))
            return fd, name
        with patch.object(bridge.tempfile, 'mkstemp', side_effect=capture), \
                patch.object(bridge.subprocess, 'Popen', side_effect=OSError('failed')):
            with self.assertRaises(OSError):
                bridge.launch_winscp(self.connection, '/home')
        self.assertFalse(created[0].exists())

    def test_connect_cleanup_after_ssh_exit(self):
        process = Mock(pid=os.getpid(), wait=Mock(return_value=255))
        with patch.object(app, 'copy_password'), patch.object(app.subprocess, 'Popen', return_value=process), \
                patch.object(bridge, 'register_connection', return_value={'token': 'test'}) as register, \
                patch.object(bridge, 'unregister_connection') as unregister, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(app.connect(self.connection, 'ssh.exe'), 255)
        register.assert_called_once_with(self.connection, os.getpid())
        unregister.assert_called_once_with({'token': 'test'})

    def test_new_tab_contains_only_encoded_name_not_credentials(self):
        with patch.dict(os.environ, {'ZELLIJ_SESSION_NAME': 's', 'ZELLIJ_PANE_ID': '3'}), \
                patch.object(bridge, 'zellij_action', return_value='7\n') as action:
            self.assertEqual(bridge.open_ssh_tab(self.connection.name), '7')
        args = action.call_args.args
        self.assertEqual(args[:5], ('s', 'new-tab', '--name', self.connection.name, '--close-on-exit'))
        self.assertEqual(base64.urlsafe_b64decode(args[-1]).decode(), self.connection.name)
        self.assertNotIn(self.connection.password, repr(args))

    def test_picker_remains_available_after_opening_tab_and_never_connects_inline(self):
        picker = Mock()
        picker.run.side_effect = [self.connection.name, None]
        with patch.object(bridge, 'picker_title', side_effect=contextlib.nullcontext), \
                patch.object(bridge, 'in_zellij', return_value=True), \
                patch.object(bridge, 'open_ssh_tab') as launch, \
                patch.object(app, 'load_connections', return_value=[self.connection]), \
                patch.object(app, 'make_selector', return_value=picker), patch.object(app, 'connect') as inline:
            self.assertEqual(app.run_picker('ssh.exe'), 0)
        self.assertEqual(picker.run.call_count, 2)
        launch.assert_called_once_with(self.connection.name)
        inline.assert_not_called()

    def test_child_flag_connects_inline_without_recursive_tabs(self):
        token = base64.urlsafe_b64encode(self.connection.name.encode()).decode()
        with patch.object(app, 'load_connections', return_value=[self.connection]), \
                patch.object(app.shutil, 'which', return_value='ssh.exe'), \
                patch.object(bridge, 'open_ssh_tab') as launch, \
                patch.object(app, 'connect', return_value=0) as inline:
            self.assertEqual(app.main(['--connect-in-pane', token]), 0)
        inline.assert_called_once_with(self.connection, 'ssh.exe')
        launch.assert_not_called()

    def test_manager_titles_restore_original_pane_and_tab(self):
        pane = {'id': 3, 'is_plugin': False, 'title': 'local', 'tab_id': 8, 'tab_name': 'Local tools'}
        with patch.dict(os.environ, {'ZELLIJ_SESSION_NAME': 's', 'ZELLIJ_PANE_ID': '3'}), \
                patch.object(bridge, 'zellij_action', return_value=json.dumps([pane])) as action:
            with bridge.picker_title():
                self.assertEqual(action.call_args.args, ('s', 'rename-tab-by-id', '8', 'xxx'))
        self.assertEqual(action.call_args.args, ('s', 'rename-tab-by-id', '8', '--', 'Local tools'))


if __name__ == '__main__':
    unittest.main()
