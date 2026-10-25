import json
import sqlite3
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from ngs import install
from ngs.catalog import relative_game_path
from ngs.play_time import PlayTime
from tests import test_install


class ReadableGamesTests(unittest.TestCase):
    def setUp(self): test_install.InstallTests.setUp(self)

    def legacy(self, name=None, identity='sample'):
        card = dict(self.card, id=identity, name=name or self.card['name'])
        document = dict(self.manifest, game_id=identity)
        with patch.object(install, 'game_folder_name', side_effect=lambda card: card['id']):
            receipt = install.install_game(self.storage, self.source, card, document)
        return receipt

    def test_names_keep_spaces_unicode_and_remove_windows_reserved_characters(self):
        for title, expected in [('Deliver At All Costs', 'Deliver At All Costs'),
                                ('S.T.A.L.K.E.R. 2: Сердце Чернобыля', 'S.T.A.L.K.E.R. 2 Сердце Чернобыля'),
                                (' ../A\\B: C?* <>|" . ', 'A B C'), ('CON', 'Игра CON'),
                                ('nul.txt', 'Игра nul.txt'), ('LPT¹', 'Игра LPT¹'), ('...', 'Игра')]:
            with self.subTest(title=title):
                name = install.game_folder_name(dict(name=title))
                self.assertEqual(name, expected); relative_game_path(name)
        self.assertLessEqual(len(install.game_folder_name(dict(name='😀'*160)).encode('utf-16-le')) // 2, 240)

    def test_collisions_use_readable_numbers_and_preserve_foreign_files(self):
        for number in range(1, 11):
            name = 'Black Mesa' if number == 1 else 'Black Mesa (%d)' % number
            (self.storage.games / name).write_bytes(b'foreign')
        receipt = install.install_game(self.storage, self.source, self.card, self.manifest)
        self.assertEqual(Path(receipt['location']).name, 'Black Mesa (11)')
        library = install.LocalLibrary(self.storage)
        self.assertEqual(len(library.reconcile()), 1)
        self.assertEqual(library.launch_spec('sample')[0].parent.name, 'Black Mesa (11)')
        library.uninstall('sample')
        self.assertEqual((self.storage.games / 'Black Mesa').read_bytes(), b'foreign')

    def test_two_games_with_the_same_title_keep_separate_ids_and_files(self):
        first = install.install_game(self.storage, self.source, self.card, self.manifest)
        second = install.install_game(self.storage, self.source, dict(self.card, id='second'), dict(self.manifest, game_id='second'))
        self.assertEqual([first['location'], second['location']], ['games/Black Mesa', 'games/Black Mesa (2)'])
        library = install.LocalLibrary(self.storage)
        self.assertEqual({r['card']['id'] for r in library.reconcile()}, {'sample', 'second'})
        with self.assertRaises(FileExistsError): install.install_game(self.storage, self.source, self.card, self.manifest)

    def test_organize_updates_folder_title_and_database_preserving_statistics_arguments_and_source(self):
        self.legacy(name='steam-technical-title')
        library = install.LocalLibrary(self.storage); library.reconcile()
        library.mark_played('sample'); library.set_launch_arguments('sample', '-windowed')
        clock = [0.0]; play = PlayTime(library.path, clock=lambda: clock[0]); play.start('sample')
        clock[0] = 17; play.checkpoint(reason='completed')
        before_stats = play.snapshot()
        source = {p.relative_to(self.source): p.read_bytes() for p in self.source.rglob('*') if p.is_file()}
        (self.storage.games / 'sample/save.bin').write_bytes(b'save')
        self.assertEqual(library.organize([self.card]), dict(changed=1, errors=[]))
        record = json.loads((library.records/'sample.json').read_bytes())
        self.assertEqual(record['card']['name'], 'Black Mesa')
        self.assertEqual(record['location'], 'games/Black Mesa')
        self.assertFalse((self.storage.games/'sample').exists())
        self.assertEqual((self.storage.games/'Black Mesa/save.bin').read_bytes(), b'save')
        self.assertEqual(library.launch_spec('sample')[0], self.storage.games/'Black Mesa/play.exe')
        self.assertEqual(library.launch_spec('sample')[2], ['-windowed'])
        self.assertEqual(play.snapshot(), before_stats)
        self.assertEqual({p.relative_to(self.source): p.read_bytes() for p in self.source.rglob('*') if p.is_file()}, source)
        with sqlite3.connect(library.path) as db:
            saved = json.loads(db.execute('SELECT receipt FROM installed WHERE game_id=?', ('sample',)).fetchone()[0])
        db.close()
        self.assertEqual(saved['location'], record['location']); self.assertEqual(saved['card']['name'], 'Black Mesa')
        self.assertEqual(library.organize([self.card]), dict(changed=0, errors=[]))
        self.assertGreater(library.reconcile()[0]['played_at'], 0)

    def test_organize_leaves_installed_external_games_untouched(self):
        self.legacy()
        external = self.root/'installed'; external.mkdir(); (external/'play.exe').write_bytes(b'already installed')
        library = install.LocalLibrary(self.storage)
        with patch('ngs.scanner.check_pe'):
            receipt = library.register_external(external, 'play.exe', 'Installed title', dict(self.card, id='separate'))
        record = library.records/(receipt['card']['id']+'.json'); before = record.read_bytes()
        self.assertEqual(library.organize([self.card]), dict(changed=1, errors=[]))
        self.assertEqual(record.read_bytes(), before)
        self.assertEqual((external/'play.exe').read_bytes(), b'already installed')

    def test_organize_recovers_interruption_before_and_after_rename(self):
        for after in (False, True):
            identity = 'after' if after else 'before'
            self.legacy(identity=identity)
            library = install.LocalLibrary(self.storage)
            rename = Path.rename
            def interrupted(path, destination):
                if path.name == identity:
                    if after: rename(path, destination)
                    raise SystemExit('simulated process exit')
                return rename(path, destination)
            with patch.object(Path, 'rename', interrupted), self.assertRaises(SystemExit):
                library.organize()
            rows = library.reconcile()
            self.assertIn(identity, [r['card']['id'] for r in rows])
            if not after: library.organize()
            self.assertTrue(library.launch_spec(identity)[0].is_file())
            self.assertFalse((library.records/(identity+'.pending.json')).exists())

    def test_busy_or_replaced_folder_is_preserved_and_can_be_retried(self):
        self.legacy()
        library = install.LocalLibrary(self.storage)
        with patch.object(Path, 'rename', side_effect=PermissionError('busy')):
            result = library.organize()
        self.assertEqual(result['changed'], 0); self.assertEqual(len(result['errors']), 1)
        self.assertTrue(library.launch_spec('sample')[0].is_file())
        self.assertEqual(library.organize(), dict(changed=1, errors=[]))
        record = library.records/'sample.json'; receipt = json.loads(record.read_bytes())
        receipt['directory_identity'] = [-1, -1]
        record.write_text(json.dumps(receipt), encoding='utf8')
        result = library.organize([dict(self.card, name='New title')])
        self.assertEqual(result['changed'], 0); self.assertTrue(result['errors'])
        self.assertTrue((self.storage.games/'Black Mesa/play.exe').is_file())

    def test_custom_install_root_stays_in_place_after_changing_default(self):
        self.legacy(); original = self.storage.games
        settings = self.storage.read_settings(); settings['games_directory'] = str(self.root/'new default')
        self.storage.write_settings(settings)
        library = install.LocalLibrary(type(self.storage)(self.storage.root))
        self.assertEqual(library.organize(), dict(changed=1, errors=[]))
        self.assertEqual(library.launch_spec('sample')[0].parent, original/'Black Mesa')

    def test_case_only_rename_can_retry_after_a_locked_directory(self):
        self.legacy(name='SAMPLE')
        library = install.LocalLibrary(self.storage)
        with patch.object(Path, 'rename', side_effect=PermissionError('busy')):
            self.assertTrue(library.organize()['errors'])
        library.organize()
        self.assertIn('SAMPLE', [p.name for p in self.storage.games.iterdir()])
        self.assertTrue(library.launch_spec('sample')[0].is_file())

    def test_corrupt_receipt_cannot_rename_launcher_or_its_data(self):
        receipt = self.legacy()
        library = install.LocalLibrary(self.storage)
        for folder in (self.storage.root, self.storage.data):
            (folder / 'play.exe').write_bytes(b'fixture')
            info = folder.stat()
            changed = dict(receipt, card=dict(receipt['card'], name=folder.name), location=str(folder),
                           directory_identity=[info.st_dev, info.st_ino])
            (library.records / 'sample.json').write_text(json.dumps(changed), encoding='utf8')
            result = library.organize([dict(self.card, name='Readable title')])
            self.assertEqual(result['changed'], 0); self.assertTrue(result['errors'])
            self.assertTrue(folder.is_dir()); self.assertTrue((folder / 'play.exe').is_file())

    def test_cancel_and_foreign_pending_installation_do_not_change_game(self):
        self.legacy(); library = install.LocalLibrary(self.storage)
        cancel = threading.Event(); cancel.set()
        with self.assertRaises(install.Cancelled): library.organize(cancel=cancel)
        receipt = json.loads((library.records/'sample.json').read_bytes()); receipt['directory_identity'] = [-1, -1]
        pending = library.records/'sample.pending.json'; pending.write_text(json.dumps(receipt), encoding='utf8'); before = pending.read_bytes()
        result = library.organize()
        self.assertEqual(result['changed'], 0); self.assertTrue(result['errors'])
        self.assertTrue((self.storage.games/'sample/play.exe').is_file()); self.assertEqual(pending.read_bytes(), before)
