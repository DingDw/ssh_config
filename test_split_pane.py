"""Check source selection without focusing, typing into, or connecting real panes."""
import base64
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import split_pane as split
import ssh_manager as app


class SplitTests(unittest.TestCase):
    def setUp(self):
        self.source = dict(id=3, is_plugin=False, is_suppressed=True, exited=False,
                           tab_id=8, pane_x=0, pane_y=1, pane_rows=30, pane_columns=90)
        self.helper = dict(self.source, id=7, is_suppressed=False)
        self.other = dict(self.source, id=6, tab_id=9)

    def test_source_is_exact_suppressed_pane_in_same_tab_and_geometry(self):
        rows = [self.other, self.helper, self.source, dict(self.source, id=1, is_plugin=True)]
        self.assertEqual(split.source_pane(rows, 7)['id'], 3)
        for rows in ([self.helper], [self.helper, self.source, dict(self.source, id=8)]):
            with self.assertRaises(RuntimeError):
                split.source_pane(rows, 7)

    def test_ssh_directions_use_source_record_and_existing_pane_worker(self):
        connection = app.Connection('SSH 中文 "name"', '192.0.2.1', 22, 'test', 'synthetic-secret')
        for direction in ('right', 'down', 'auto', 'stacked'):
            request = dict(session='session', source=3, helper=7, direction=direction, tab=8, server={}, connection_name=connection.name)
            results = [json.dumps([dict(self.source, is_suppressed=False)]), 'terminal_9'] + ([''] if direction == 'stacked' else [])
            with patch.object(split.bridge, 'zellij_action', side_effect=results) as action, \
                    patch.object(split.bridge, 'identity_alive', return_value=True), \
                    patch.object(split.bridge, 'state_path', return_value=Mock(is_file=Mock(return_value=True))), \
                    patch.object(split.bridge, 'connection_for_pane', return_value=connection) as resolve:
                self.assertEqual(split.finish_split(request), 'terminal_9')
            resolve.assert_called_once_with('session', 3)
            args = action.call_args_list[1].args
            self.assertEqual(action.call_args_list[1].kwargs, {'source_pane_id': 3})
            self.assertIn('--near-current-pane', args)
            self.assertIn('--connect-in-pane', args)
            self.assertEqual(base64.urlsafe_b64decode(args[-1]).decode(), connection.name)
            self.assertNotIn(connection.password, str(args))
            if direction in ('right', 'down'):
                self.assertEqual(args[args.index('--direction') + 1], direction)
            if direction == 'stacked':
                self.assertEqual(action.call_args.args, ('session', 'stack-panes', '--', 'terminal_3', 'terminal_9'))

    def test_local_source_opens_default_shell(self):
        request = dict(session='session', source=3, helper=7, direction='right', tab=8, server={}, connection_name=None)
        with patch.object(split.bridge, 'zellij_action', side_effect=[json.dumps([dict(self.source, is_suppressed=False)]), 'terminal_9']) as action, \
                patch.object(split.bridge, 'identity_alive', return_value=True), \
                patch.object(split.bridge, 'state_path', return_value=Mock(is_file=Mock(return_value=False))), \
                patch.object(split.bridge, 'connection_for_pane') as resolve:
            split.finish_split(request)
        resolve.assert_not_called()
        self.assertNotIn('--connect-in-pane', action.call_args.args)
        self.assertNotIn('--', action.call_args.args)

    def test_stale_connection_never_launches_a_new_pane(self):
        with patch.object(split.bridge, 'zellij_action', return_value=json.dumps([self.source, self.helper])) as action, \
                patch.object(split.bridge, 'state_path', return_value=Mock(is_file=Mock(return_value=True))), \
                patch.object(split.bridge, 'connection_for_pane', side_effect=RuntimeError('stale')):
            with self.assertRaises(RuntimeError):
                split.split('session', 7, 'right')
        self.assertEqual(action.call_count, 1)

    def test_dispatch_defers_until_helper_exits_and_captures_source(self):
        with patch.object(split.bridge, 'zellij_action', return_value=json.dumps([self.source, self.helper])) as action, \
                patch.object(split.bridge, 'state_path', return_value=Mock(is_file=Mock(return_value=False))), \
                patch.object(split.bridge, 'find_server', return_value={'pid': 12, 'created': 123}), \
                patch.object(split.subprocess, 'Popen') as spawn:
            request = split.split('session', 7, 'stacked')
        self.assertEqual(action.call_count, 1)
        self.assertEqual(request['source'], 3)
        self.assertEqual(request['helper'], 7)
        self.assertEqual(request['direction'], 'stacked')
        self.assertEqual(json.loads(base64.urlsafe_b64decode(spawn.call_args.args[0][-1])), request)

    def test_changed_target_cancels_delayed_split(self):
        request = dict(session='session', source=3, helper=7, direction='right', tab=8, server={}, connection_name='old')
        with patch.object(split.bridge, 'zellij_action', return_value=json.dumps([dict(self.source, is_suppressed=False)])) as action, \
                patch.object(split.bridge, 'identity_alive', return_value=True), \
                patch.object(split.bridge, 'state_path', return_value=Mock(is_file=Mock(return_value=False))):
            with self.assertRaises(RuntimeError):
                split.finish_split(request)
        self.assertEqual(action.call_count, 1)

    def test_stack_source_is_resolved_after_helper_exits(self):
        request = dict(session='session', source=3, helper=7, direction='right', tab=8, server={},
                       connection_name='hidden-server', candidates=[dict(id=3, connection_name='hidden-server'),
                                                                   dict(id=8, connection_name='active-server')])
        active = dict(self.source, id=8, is_suppressed=False)
        connection = app.Connection('active-server', '192.0.2.8', 22, 'test')
        with patch.object(split.bridge, 'zellij_action', side_effect=[json.dumps([self.source, active]), 'terminal_9']) as action, \
                patch.object(split.bridge, 'identity_alive', return_value=True), \
                patch.object(split.bridge, 'state_path', return_value=Mock(is_file=Mock(return_value=True))), \
                patch.object(split.bridge, 'connection_for_pane', return_value=connection) as resolve:
            split.finish_split(request)
        resolve.assert_called_once_with('session', 8)
        self.assertEqual(action.call_args.kwargs, {'source_pane_id': 8})


if __name__ == '__main__':
    unittest.main()
