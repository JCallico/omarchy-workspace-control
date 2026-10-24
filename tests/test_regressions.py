"""Backend regression tests. Never dispatch to the real desktop.

Run with: python3 -B -m unittest discover -s tests -v
"""
import copy
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import common
import restore
import save


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state_dir = Path(self.temp.name)
        for module in (common, restore, save):
            for name, value in vars(module).copy().items():
                if name in ("STATE_DIR", "STATE_FILE", "CONFIG_FILE", "LOG_FILE", "RESTORE_LOCK",
                            "OPERATION_LOCK", "RECOVERY_FILE"):
                    replacement = self.state_dir if name == "STATE_DIR" else self.state_dir / value.name
                    p = patch.object(module, name, replacement)
                    p.start()
                    self.addCleanup(p.stop)
        self.addCleanup(patch.stopall)
        patch.object(restore, "DRY_RUN", False).start()
        patch.object(restore.time, "sleep").start()
        # Default IPC is a mock: each test must provide its own live clients.
        self.dispatch = patch.object(restore, "dispatch").start()
        self.ipc = patch.object(restore, "hyprctl_json", return_value=[]).start()
        patch.object(restore, "wait_for_hyprctl", return_value=True).start()
        patch.object(save, "hyprctl_json", side_effect=lambda arg: [] if arg == "clients" else {"name": "1"}).start()
        common.write_state({"workspaces": {"1": {"windows": [self.window()]}}})

    def window(self, **kwargs):
        return {"class": "app", "title": "app", "at": [0, 0], "size": [100, 100],
                "floating": False, "pinned": False, "fullscreen": 0,
                "cmdline": ["app"], **kwargs}

    def client(self, address="0x1", ws="1", **kwargs):
        return {"address": address, "workspace": {"name": ws}, **self.window(), **kwargs}

    def test_failed_restore_blocks_automatic_saves_and_preserves_backup_on_retry(self):
        original = common.STATE_FILE.read_bytes()
        with patch.object(restore, "run", return_value=False):
            self.assertFalse(restore.main())
            self.assertTrue(common.RECOVERY_FILE.exists())
            self.assertFalse(save.snapshot(None))
            self.assertEqual(common.STATE_FILE.read_bytes(), original)
            backup = self.state_dir / "session.pre-restore.json"
            self.assertEqual(backup.read_bytes(), original)
            common.write_state({"workspaces": {}})
            self.assertFalse(restore.main())
            self.assertEqual(backup.read_bytes(), original)

    def test_crash_keeps_recovery_guard_and_releases_locks(self):
        with patch.object(restore, "run", side_effect=RuntimeError("IPC failure")):
            with self.assertRaises(RuntimeError):
                restore.main()
        self.assertTrue(common.RECOVERY_FILE.exists())
        self.assertFalse(common.restore_in_progress())
        with common.file_lock(common.OPERATION_LOCK, blocking=False) as acquired:
            self.assertTrue(acquired)

    def test_successful_retry_unblocks_saving(self):
        common.RECOVERY_FILE.touch()
        with patch.object(restore, "run", return_value=True):
            self.assertTrue(restore.main())
        self.assertFalse(common.RECOVERY_FILE.exists())
        self.assertTrue(save.snapshot(None))

    def test_explicit_save_replaces_recovery_layout(self):
        common.RECOVERY_FILE.touch()
        self.assertTrue(save.snapshot(None, force=True))
        self.assertFalse(common.RECOVERY_FILE.exists())
        self.assertEqual(common.load_state()["workspaces"], {})

    def test_clear_removes_recovery_and_saved_state(self):
        common.RECOVERY_FILE.touch()
        backup = self.state_dir / "session.pre-restore.json"
        backup.touch()
        save.clear_session()
        self.assertFalse(common.STATE_FILE.exists())
        self.assertFalse(common.RECOVERY_FILE.exists())
        self.assertFalse(backup.exists())

    def test_second_restore_is_rejected_without_touching_backup(self):
        with common.file_lock(common.RESTORE_LOCK):
            with patch.object(restore, "run") as run:
                self.assertFalse(restore.main())
                run.assert_not_called()
            self.assertTrue(common.restore_in_progress())
        self.assertFalse((self.state_dir / "session.pre-restore.json").exists())

    def test_save_and_clear_cannot_override_active_restore(self):
        with common.file_lock(common.RESTORE_LOCK):
            self.assertFalse(save.snapshot(None, force=True))
            with self.assertRaises(RuntimeError):
                save.clear_session()
        self.assertTrue(common.STATE_FILE.exists())

    def test_kernel_lock_does_not_expire_and_is_released_after_process_death(self):
        code = "import common,time; lock=common.file_lock(common.RESTORE_LOCK); lock.__enter__(); print('locked', flush=True); time.sleep(30)"
        env = {**os.environ, "WORKSPACE_CONTROL_STATE_DIR": str(self.state_dir),
               "PYTHONPATH": str(Path(common.__file__).parent)}
        proc = subprocess.Popen([sys.executable, "-B", "-c", code], env=env, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(proc.stdout.readline().strip(), "locked")
            os.utime(common.RESTORE_LOCK, (1, 1))
            self.assertTrue(common.restore_in_progress())
            proc.kill()
            proc.wait(timeout=5)
            self.assertFalse(common.restore_in_progress())
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()

    def test_partial_launch_returns_failure(self):
        with patch.object(restore.Restorer, "open_window", return_value=None):
            self.assertFalse(restore.run())

    def test_reused_window_is_normalized_before_moving_into_tree(self):
        c = self.client(ws=restore.STAGING_WORKSPACE, floating=True, pinned=True, fullscreen=2)
        c["_orig_ws"] = "1"
        self.ipc.return_value = [c]
        r = restore.Restorer([c])
        self.assertEqual(r.open_window(self.window(), "1", "0x2", "r"), "0x1")
        cmds = [call.args[0] for call in self.dispatch.call_args_list]
        self.assertIn('internal = 0, client = 0', cmds[0])
        self.assertIn('pin({ action = "off"', cmds[1])
        self.assertIn('float({ action = "off"', cmds[2])
        self.assertIn('focus', cmds[3])
        self.assertIn('preselect r', cmds[4])
        self.assertIn("workspace = '1'", cmds[5])

    def test_tiled_window_is_not_sent_invalid_unpin_command(self):
        self.ipc.return_value = [self.client()]
        restore.Restorer([]).normalize("0x1", False)
        self.assertFalse(any('window.pin' in call.args[0] for call in self.dispatch.call_args_list))

    def test_exception_after_staging_returns_all_windows(self):
        clients = [self.client(), self.client(address="0x2")]
        self.ipc.side_effect = lambda *_: copy.deepcopy(clients)
        def dispatch(code):
            for c in clients:
                if f"address:{c['address']}" in code and 'window.move' in code:
                    c['workspace']['name'] = restore.STAGING_WORKSPACE if restore.STAGING_WORKSPACE in code else '1'
        self.dispatch.side_effect = dispatch
        with patch.object(restore, "build_tree", side_effect=RuntimeError("invalid record")):
            with self.assertRaises(RuntimeError):
                restore.run()
        self.assertEqual([c['workspace']['name'] for c in clients], ['1', '1'])

    def test_cleanup_includes_claimed_window_and_continues_after_one_error(self):
        clients = [self.client(), self.client(address="0x2")]
        for c in clients:
            c['_orig_ws'] = '1'
            c['workspace']['name'] = restore.STAGING_WORKSPACE
        r = restore.Restorer(clients)
        r.parked = {'0x1', '0x2'}
        r.claim_existing(self.window(), '1')
        self.ipc.return_value = clients
        attempted = []
        def dispatch(code):
            if 'window.move' in code:
                attempted.append(code)
                if 'address:0x1' in code:
                    raise RuntimeError('window disappeared')
        self.dispatch.side_effect = dispatch
        self.assertFalse(r.recover_staging())
        self.assertEqual(len(attempted), 2)

    def test_successful_restore_normalizes_reused_state_and_returns_extras(self):
        clients = [self.client(floating=True, pinned=True, fullscreen=2),
                   self.client(address="0x2", ws="2")]
        self.ipc.side_effect = lambda *_: copy.deepcopy(clients)
        def dispatch(code):
            for c in clients:
                if f"address:{c['address']}" not in code:
                    continue
                if "window.move" in code:
                    if restore.STAGING_WORKSPACE in code:
                        c["workspace"]["name"] = restore.STAGING_WORKSPACE
                    else:
                        c["workspace"]["name"] = re.search(r"workspace = '([^']+)'", code).group(1)
                if "fullscreen_state" in code:
                    c["fullscreen"] = 0
                if "window.pin" in code:
                    c["pinned"] = 'action = "on"' in code
                if "window.float" in code:
                    c["floating"] = 'action = "on"' in code
        self.dispatch.side_effect = dispatch
        self.assertTrue(restore.run())
        self.assertEqual(clients[0]["workspace"]["name"], "1")
        self.assertFalse(clients[0]["floating"])
        self.assertFalse(clients[0]["pinned"])
        self.assertEqual(clients[0]["fullscreen"], 0)
        self.assertEqual(clients[1]["workspace"]["name"], "2")

    def test_missing_live_window_is_not_reported_as_success(self):
        with patch.object(restore.Restorer, "open_window", autospec=True) as opened:
            def disappearing(r, w, *_):
                r.opened[id(w)] = "0x1"
                return "0x1"
            opened.side_effect = disappearing
            self.assertFalse(restore.run())

    def test_lua_failure_with_zero_process_status_is_detected(self):
        with patch.object(common.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'error: IPC failed', '')):
            with self.assertRaises(RuntimeError):
                common.hyprctl_repl('hl.dispatch(test)')

    def test_concurrent_workspace_saves_are_serialized(self):
        # First process pauses after reading state. Second must not even read
        # until it commits; otherwise one of the per-workspace updates is lost.
        common.write_state({'workspaces': {'1': {'windows': []}, '2': {'windows': []}}})
        code = '''
import sys,time
from pathlib import Path
import common,save
ws=sys.argv[1]
save.hyprctl_json=lambda arg: ([{'pid':int(ws),'class':'app','workspace':{'name':ws}}] if arg=='clients' else {'name':ws})
save.read_cmdline=lambda pid:['app']
save.window_record=lambda c:{'title':'new'+ws}
original=save.load_state
def read():
    result=original()
    (common.STATE_DIR/('read'+ws)).touch()
    deadline=time.monotonic()+5
    if ws=='1':
        while not (common.STATE_DIR/'release').exists():
            if time.monotonic()>deadline: raise RuntimeError('test timeout')
            time.sleep(.01)
    return result
save.load_state=read
save.snapshot({ws})
'''
        env = {**os.environ, 'WORKSPACE_CONTROL_STATE_DIR': str(self.state_dir),
               'PYTHONPATH': str(Path(common.__file__).parent)}
        procs = []
        try:
            procs.append(subprocess.Popen([sys.executable, '-B', '-c', code, '1'], env=env))
            deadline = time.monotonic() + 5
            while not (self.state_dir/'read1').exists():
                if time.monotonic() > deadline:
                    self.fail('first save did not read state')
                time.sleep(.01)
            procs.append(subprocess.Popen([sys.executable, '-B', '-c', code, '2'], env=env))
            # A nonblocking probe also verifies the operation is locked.
            with common.file_lock(common.OPERATION_LOCK, blocking=False) as acquired:
                self.assertFalse(acquired)
            (self.state_dir/'release').touch()
            for proc in procs:
                self.assertEqual(proc.wait(timeout=5), 0)
            state = common.load_state()['workspaces']
            self.assertEqual(state['1']['windows'][0]['title'], 'new1')
            self.assertEqual(state['2']['windows'][0]['title'], 'new2')
        finally:
            for proc in procs:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()


if __name__ == '__main__':
    unittest.main()
