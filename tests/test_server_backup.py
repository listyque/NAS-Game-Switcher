"""Exercise the shell backup lifecycle with a local fake Docker executable."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest


SOURCE = Path(__file__).resolve().parents[1]
FAKE_DOCKER = r'''
import json
import os
from pathlib import Path
import sys
import time

root = Path(os.environ['NGS_BACKUP_FIXTURE'])
options = json.loads((root / 'fixture.json').read_text())
args = sys.argv[1:]
with (root / 'calls.jsonl').open('a') as stream:
    stream.write(json.dumps(args) + '\n')
if args == ['compose', 'version']:
    raise SystemExit(0)
if args[0] == 'inspect':
    if options.get('inspect_fail'):
        raise SystemExit(1)
    if 'working_dir' in args[2]:
        print(options.get('owner', str(root)))
    elif '.State.Running' in args[2]:
        print((root / 'app-running').read_text())
    else:
        raise SystemExit('Unexpected inspect query')
    raise SystemExit(0)
if args[:5] != ['compose', '-p', 'ngs-server', '-f', 'compose.yaml']:
    raise SystemExit('Unexpected Docker invocation')
command = args[5]

def hold(phase):
    if options.get('hold') != phase:
        return
    (root / 'entered').touch()
    deadline = time.monotonic() + 8
    while not (root / 'release').exists():
        if time.monotonic() > deadline:
            raise SystemExit('Fixture timed out')
        time.sleep(0.01)

if command == 'stop':
    (root / 'app-running').write_text('false')
    hold('stop')
elif command == 'start':
    (root / 'app-running').write_text('true')
elif command == 'exec':
    if args[-1] == 'sthpw':
        hold('dump')
    print('fixture dump: ' + args[-1])
elif command == 'run':
    name = args[-1]
    assert name.isalnum(), 'backup_assets.py requires an alphanumeric name'
    with (root / 'backups' / name / 'assets-and-config.tar.gz').open('xb') as stream:
        stream.write(b'fixture assets')
else:
    raise SystemExit('Unexpected Compose command')
'''


@unittest.skipUnless(os.name == 'posix' and shutil.which('sh'), 'POSIX shell fixture')
class ServerBackupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='backup-fixture-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        for name in ('backup.sh', 'compose.sh'):
            shutil.copyfile(SOURCE / 'server' / name, self.root / name)
        docker = self.bin / 'docker'
        docker.write_text('#!' + sys.executable + '\n' + FAKE_DOCKER)
        docker.chmod(0o700)
        # Force successive backups into the same timestamp to test uniqueness.
        date = self.bin / 'date'
        date.write_text('#!/bin/sh\nprintf "20261021T120000Z\\n"\n')
        date.chmod(0o700)
        (self.root / 'secrets').mkdir()
        (self.root / 'secrets' / 'fixture').write_text('dummy fixture')
        (self.root / 'bootstrap').mkdir()
        (self.root / 'bootstrap' / 'manifest.json').write_text('{}')
        (self.root / '.env').write_text('NGS_PORT=8890\n')
        (self.root / 'app-running').write_text('true')
        self.options()
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                        NGS_BACKUP_FIXTURE=str(self.root))

    def options(self, **values):
        (self.root / 'fixture.json').write_text(json.dumps(values))

    def start(self):
        process = subprocess.Popen(['sh', str(self.root / 'backup.sh')], cwd=self.root,
                                   env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, start_new_session=True)
        self.addCleanup(self.finish, process)
        return process

    def finish(self, process):
        if process.poll() is None:
            (self.root / 'release').touch()
            process.terminate()
            try:
                process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=3)

    def completed(self, process):
        stdout, stderr = process.communicate(timeout=8)
        return process.returncode, stdout + stderr

    def commands(self):
        path = self.root / 'calls.jsonl'
        calls = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [args[5:] for args in calls if args[:5] ==
                ['compose', '-p', 'ngs-server', '-f', 'compose.yaml']]

    def wait_for_hold(self, process):
        deadline = time.monotonic() + 5
        while not (self.root / 'entered').exists():
            self.assertIsNone(process.poll(), 'backup exited before fixture barrier')
            self.assertLess(time.monotonic(), deadline, 'fixture barrier timed out')
            time.sleep(0.01)

    def test_overlapping_backup_is_rejected_and_directories_never_reused(self):
        self.options(hold='dump')
        first = self.start()
        self.wait_for_hold(first)
        code, output = self.completed(self.start())
        self.assertNotEqual(code, 0, output)
        self.assertIn('Another backup', output)
        self.assertEqual(sum(command[0] == 'stop' for command in self.commands()), 1)
        self.assertFalse(any(command[0] == 'start' for command in self.commands()))
        (self.root / 'release').touch()
        self.assertEqual(self.completed(first)[0], 0)
        self.options()
        self.assertEqual(self.completed(self.start())[0], 0)
        copies = list((self.root / 'backups').glob('*/COMPLETE'))
        self.assertEqual(len(copies), 2)
        for complete in copies:
            self.assertEqual((complete.parent / 'sthpw.dump').read_text(), 'fixture dump: sthpw\n')
        self.assertEqual(sum(command[0] == 'start' for command in self.commands()), 2)
        self.assertFalse((self.root / 'backups' / '.backup.lock').exists())

    def test_foreign_or_uninspectable_stack_is_rejected_before_backup_writes(self):
        for options in ({'owner': '/another/installation'}, {'inspect_fail': True}):
            with self.subTest(options=options):
                self.options(**options)
                code, output = self.completed(self.start())
                self.assertNotEqual(code, 0, output)
                self.assertIn('backup refused', output)
                self.assertEqual(self.commands(), [])
                self.assertFalse((self.root / 'backups').exists())
                self.assertEqual((self.root / 'app-running').read_text(), 'true')

    def test_term_exits_backup_and_restarts_once_even_during_stop(self):
        for phase in ('stop', 'dump'):
            with self.subTest(phase=phase):
                for name in ('entered', 'release', 'calls.jsonl'):
                    (self.root / name).unlink(missing_ok=True)
                self.options(hold=phase)
                process = self.start()
                self.wait_for_hold(process)
                process.terminate()  # Signal only the shell, not the fake child command.
                (self.root / 'release').touch()
                code, output = self.completed(process)
                self.assertEqual(code, 143, output)
                commands = self.commands()
                self.assertEqual(sum(command[0] == 'start' for command in commands), 1)
                self.assertFalse(any(command[0] == 'run' for command in commands))
                self.assertFalse(any(command[0] == 'exec' and command[-1] == 'ngs_launcher'
                                     for command in commands))
                self.assertEqual(list((self.root / 'backups').glob('*/COMPLETE')), [])
                self.assertFalse((self.root / 'backups' / '.backup.lock').exists())
                self.assertEqual((self.root / 'app-running').read_text(), 'true')

    def test_initially_stopped_app_stays_stopped(self):
        (self.root / 'app-running').write_text('false')
        code, output = self.completed(self.start())
        self.assertEqual(code, 0, output)
        self.assertFalse(any(command[0] in ('start', 'stop') for command in self.commands()))
        self.assertEqual((self.root / 'app-running').read_text(), 'false')
        self.assertEqual(len(list((self.root / 'backups').glob('*/COMPLETE'))), 1)


if __name__ == '__main__':
    unittest.main()
