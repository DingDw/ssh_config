"""Offline checks: never connect to servers or touch the real clipboard."""
import contextlib
import io
import json
import unittest
from unittest.mock import Mock, patch

import ssh_manager as app


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.dev = app.Connection('开发环境 macbs-dev', '192.0.2.10', 2200, 'tester', 'synthetic-secret')
        self.test = app.Connection('macbs-test', '192.0.2.20', 22, 'tester')

    def test_fixed_config_and_secret_preservation(self):
        data = {'version': 1, 'connections': [dict(name=self.dev.name, host=self.dev.host,
                 port=2200, username='tester', password='  synthetic-密码-"-\\  ')]}
        with patch.object(app.Path, 'read_text', return_value=json.dumps(data)):
            result = app.load_connections()
        self.assertEqual(str(app.CONFIG_PATH), r'C:\Users\ghoul\ssh_config\config.json')
        self.assertEqual(result[0].password, data['connections'][0]['password'])
        self.assertNotIn('synthetic-', repr(result))

    def test_bad_config_never_echoes_source(self):
        with patch.object(app.Path, 'read_text', return_value='{"password":"synthetic-secret",BROKEN'):
            with self.assertRaises(ValueError) as error:
                app.load_connections()
        self.assertNotIn('synthetic-secret', str(error.exception))

    def test_invalid_port_and_duplicate_names(self):
        entry = dict(name='dev', host='192.0.2.10', username='tester', port=70000)
        for entries in ([entry], [dict(entry, port=22), dict(entry, port=22)]):
            with patch.object(app.Path, 'read_text', return_value=json.dumps({'version': 1, 'connections': entries})):
                with self.assertRaises(ValueError):
                    app.load_connections()

    def test_fuzzy_names_addresses_and_no_password_search(self):
        rows = [self.dev, self.test]
        self.assertEqual(app.filter_connections(rows, ''), rows)
        self.assertEqual(app.filter_connections(rows, 'MD'), [self.dev])
        self.assertEqual(app.filter_connections(rows, '.20')[0], self.test)
        self.assertEqual(app.filter_connections(rows, 'synthetic-secret'), [])

    def test_clipboard_before_process_and_password_absent_from_arguments(self):
        events = []
        def copy(value):
            self.assertEqual(value, self.dev.password)
            events.append('copy')
        def spawn(arguments):
            events.append('ssh')
            self.assertEqual(arguments, ['ssh.exe', '-F', 'none', '-p', '2200', '-l', 'tester', '--', '192.0.2.10'])
            self.assertNotIn(self.dev.password, arguments)
            return Mock(wait=Mock(return_value=255))
        with patch.object(app, 'copy_password', side_effect=copy), patch.object(app.subprocess, 'Popen', side_effect=spawn):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                code = app.connect(self.dev, 'ssh.exe')
        self.assertEqual(events, ['copy', 'ssh'])
        self.assertEqual(code, 255)
        self.assertNotIn(self.dev.password, output.getvalue())

    def test_copy_failure_stops_connection_and_key_login_skips_copy(self):
        with patch.object(app, 'copy_password', side_effect=RuntimeError('copy failed')), patch.object(app.subprocess, 'Popen') as spawn:
            with self.assertRaises(RuntimeError):
                app.connect(self.dev, 'ssh.exe')
            spawn.assert_not_called()
        with patch.object(app, 'copy_password') as copy, patch.object(app.subprocess, 'Popen', return_value=Mock(wait=Mock(return_value=0))):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(app.connect(self.test, 'ssh.exe'), 0)
            copy.assert_not_called()

    def test_picker_live_filter_selection_clear_and_cancel(self):
        from prompt_toolkit import Application
        from prompt_toolkit.input.defaults import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        for keys, expected, query in [('mts\r', self.test.name, 'mts'), ('garbage\x15\x1b[B\r', self.test.name, ''), ('\x03', None, ''), ('\x1b', None, '')]:
            with create_pipe_input() as pipe:
                def factory(**kwargs):
                    return Application(**kwargs, input=pipe, output=DummyOutput())
                state = app.SelectionState()
                with patch('prompt_toolkit.Application', side_effect=factory):
                    picker = app.make_selector([self.dev, self.test], state, '')
                pipe.send_text(keys)
                self.assertEqual(picker.run(), expected)
                self.assertEqual(state.query, query)

    def test_startup_cleanup_runs_once_and_keeps_picker_available(self):
        import ssh_integration as bridge
        for failure in (False, True):
            picker = Mock()
            picker.run.side_effect = [self.dev.name, None]
            with patch.object(bridge, 'picker_title', side_effect=contextlib.nullcontext), \
                    patch.object(bridge, 'in_zellij', return_value=True), \
                    patch.object(bridge, 'open_ssh_tab') as launch, \
                    patch.object(app, 'load_connections', return_value=[self.dev]), \
                    patch.object(app, 'make_selector', return_value=picker) as selector, \
                    patch.object(bridge, 'cleanup_invalid_records',
                                 return_value={'deleted': 2, 'kept': 1, 'skipped': 0},
                                 side_effect=OSError('denied') if failure else None) as cleanup:
                self.assertEqual(app.run_picker('ssh.exe'), 0)
            cleanup.assert_called_once()
            launch.assert_called_once_with(self.dev.name)
            self.assertEqual(selector.call_count, 2)
            initial_status = selector.call_args_list[0].args[2]
            self.assertIn('自动清理失败' if failure else '自动清理 2，有效 1，跳过 0', initial_status)

    def test_noninteractive_modes_do_not_cleanup(self):
        import base64
        import ssh_integration as bridge
        token = base64.urlsafe_b64encode(self.dev.name.encode()).decode()
        for args in (['--list'], ['--check'], ['--name', self.dev.name], ['--connect-in-pane', token]):
            with patch.object(app, 'load_connections', return_value=[self.dev]), \
                    patch.object(app.shutil, 'which', return_value='ssh.exe'), \
                    patch.object(bridge, 'in_zellij', return_value=False), \
                    patch.object(app, 'connect', return_value=0), \
                    patch.object(bridge, 'cleanup_invalid_records') as cleanup, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(app.main(args), 0)
            cleanup.assert_not_called()

    def test_windows_clipboard_unicode_ownership_and_failure_cleanup(self):
        user, kernel = Mock(), Mock()
        user.CreateWindowExW.return_value = 11
        user.OpenClipboard.return_value = 1
        user.EmptyClipboard.return_value = 1
        user.SetClipboardData.return_value = 22
        kernel.GlobalAlloc.return_value = 22
        kernel.GlobalLock.return_value = 33
        with patch.object(app.ctypes, 'WinDLL', side_effect=[user, kernel]), patch.object(app.ctypes, 'memmove') as write:
            app.copy_password(' synthetic-密码 ')
        content = ' synthetic-密码 '.encode('utf-16-le') + b'\0\0'
        write.assert_called_once_with(33, content, len(content))
        user.OpenClipboard.assert_called_once_with(11)
        user.SetClipboardData.assert_called_once_with(13, 22)
        user.CloseClipboard.assert_called_once()
        user.DestroyWindow.assert_called_once_with(11)
        kernel.GlobalFree.assert_not_called()

        user.reset_mock()
        user.OpenClipboard.return_value = 0
        with patch.object(app.ctypes, 'WinDLL', side_effect=[user, kernel]), patch.object(app.ctypes, 'memmove'), patch.object(app.time, 'sleep'):
            with self.assertRaises(RuntimeError):
                app.copy_password('synthetic-secret')
        kernel.GlobalFree.assert_called_once_with(22)
        user.EmptyClipboard.assert_not_called()
        user.SetClipboardData.assert_not_called()
        user.DestroyWindow.assert_called_once_with(11)


if __name__ == '__main__':
    unittest.main()
