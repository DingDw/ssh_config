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

    def test_cleanup_key_keeps_picker_query_and_selection(self):
        import asyncio
        import ssh_integration as bridge
        from prompt_toolkit import Application
        from prompt_toolkit.input.defaults import create_pipe_input
        from prompt_toolkit.output import DummyOutput

        for failure in (False, True):
            with create_pipe_input() as pipe:
                def factory(**kwargs):
                    return Application(**kwargs, input=pipe, output=DummyOutput())
                state = app.SelectionState(query='macbs', index=1)
                expected = app.filter_connections([self.dev, self.test], state.query)[state.index].name
                with patch('prompt_toolkit.Application', side_effect=factory):
                    picker = app.make_selector([self.dev, self.test], state, '')
                async def keys():
                    pipe.send_text('\x1b[19~')  # F8
                    for _ in range(100):
                        await asyncio.sleep(0.01)
                        if cleanup.called:
                            await asyncio.sleep(0.05)
                            break
                    pipe.send_text('\r')
                with patch.object(bridge, 'cleanup_invalid_records',
                                  return_value={'deleted': 2, 'kept': 1, 'skipped': 0},
                                  side_effect=OSError('denied') if failure else None) as cleanup:
                    result = picker.run(pre_run=lambda: picker.create_background_task(keys()))
                    cleanup.assert_called_once()
                self.assertEqual(result, expected)
                self.assertEqual(state.query, 'macbs')
                self.assertEqual(state.index, 1)

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
