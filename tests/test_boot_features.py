import logging,sys,tempfile,time,hashlib,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from PySide6.QtTest import QTest
from tests import test_ui
from ngs import diagnostics,appearance
from ngs.portable import PortableStorage
from ngs.catalog import CatalogCache


class BootFeatureTests(unittest.TestCase):
    def tearDown(self):
        if diagnostics._current is not None: diagnostics._current.finish()
        diagnostics.set_production(False)

    def test_production_has_no_session_files_uploads_or_breadcrumbs_and_restores_events(self):
        from ngs import crash_capture
        with tempfile.TemporaryDirectory() as directory:
            storage=PortableStorage(directory)
            settings=storage.read_settings();settings['production_mode']=True;storage.write_settings(settings)
            with patch('ngs.diagnostics.SessionLog') as session:
                self.assertIsNone(diagnostics.start(storage))
                session.assert_not_called()
            self.assertIsNone(crash_capture.enable(storage))
            diagnostics.event('test');crash_capture.note('test');logging.warning('test')
            self.assertEqual(list(storage.logs.iterdir()),[])
            settings['production_mode']=False;storage.write_settings(settings)
            log=diagnostics.start(storage,'probe')
            diagnostics.event('visible')
            before=log.active.read_bytes()
            diagnostics.set_production(True)
            diagnostics.event('must-not-appear');log.write('direct-must-not-appear')
            with patch.object(log,'snapshot') as snapshot,patch.object(log,'pack') as pack:
                self.assertFalse(log.resumed.is_set())
                snapshot.assert_not_called();pack.assert_not_called()
            self.assertEqual(log.active.read_bytes(),before)
            diagnostics.set_production(False);diagnostics.event('resumed')
            self.assertIn(b'resumed',log.active.read_bytes())

    def test_streamed_image_digest_rejects_corruption_without_trusting_cached_timestamps(self):
        with tempfile.TemporaryDirectory() as directory:
            storage=PortableStorage(directory);cache=CatalogCache(storage,'http://127.0.0.1:9/tactic')
            content=b'x'*1024;digest=hashlib.sha256(content).hexdigest();path=cache.image_path(digest);path.write_bytes(content)
            image=dict(sha256=digest,size=len(content))
            with patch('hashlib.file_digest',wraps=hashlib.file_digest) as hashed:
                self.assertTrue(cache.image_valid(image));self.assertTrue(cache.image_valid(image));self.assertEqual(hashed.call_count,2)
                path.write_bytes(b'y'*1024)
                self.assertFalse(cache.image_valid(image));self.assertEqual(hashed.call_count,3)
                path.unlink();self.assertFalse(cache.image_valid(image))

    def test_palette_cache_is_bounded_and_return_values_are_independent(self):
        appearance._colors.cache_clear()
        a=appearance.colors({});a['text']='wrong'
        self.assertNotEqual(appearance.colors({})['text'],'wrong')
        self.assertEqual(appearance._colors.cache_info().misses,1)
        for light in ('dark','light'):
            for accent in appearance.ACCENTS:appearance.colors(dict(theme=light,accent_color=accent))
        self.assertLessEqual(appearance._colors.cache_info().currsize,10)


class BootUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): test_ui.UiTests.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls): test_ui.UiTests.tearDownClass.__func__(cls)

    def test_local_cards_are_cached_and_invalidated_on_library_change(self):
        bridge=self.engine.bridge
        bridge.invalidate_local_cards()
        with patch.object(bridge,'local_card',wraps=bridge.local_card) as local:
            first=bridge.localGames;second=bridge.localGames
            self.assertEqual(first,second);self.assertEqual(local.call_count,len(bridge._installed))
            bridge.libraryChanged.emit();bridge.localGames
            self.assertEqual(local.call_count,len(bridge._installed)*2)

    def test_startup_screen_waits_for_ready_frame_and_then_destroys_its_animation(self):
        from ngs.startup_screen import StartupScreen
        cover=StartupScreen(self.storage,True)
        self.assertFalse(cover.cancelled)
        self.assertTrue(cover.window.isVisible())
        self.assertTrue(cover.window.grabWindow().save(str(self.repo/'reports/startup-cover.png')))
        self.window.setProperty('bootTestReady',False)
        main=Mock()
        main.property.return_value=False;main.opacity.return_value=0
        cover.attach(main);cover.presented();self.assertFalse(cover.done)
        main.property.return_value=True
        cover.presented();main.setOpacity.assert_any_call(1);self.assertFalse(cover.done)
        main.opacity.return_value=1
        cover.presented();self.assertTrue(cover.done);self.assertFalse(cover.window.isVisible())

    def test_startup_releases_after_real_qquickwindow_frames(self):
        from ngs.startup_screen import StartupScreen
        cover=StartupScreen(self.storage,True)
        cover.attach(self.window)
        for _ in range(100):
            QTest.qWait(20)
            if cover.done: break
        self.assertTrue(cover.done)
        self.assertEqual(self.window.opacity(),1)

    def test_startup_fallback_restores_window_if_driver_never_presents(self):
        from ngs.startup_screen import StartupScreen
        cover=StartupScreen(self.storage,True)
        main=Mock();cover.attach(main);cover.recover()
        main.setOpacity.assert_any_call(1)
        self.assertTrue(cover.done)

    def test_production_toggle_saves_and_existing_fields_remain_stable(self):
        from PySide6.QtCore import QObject,QMetaObject
        bridge=self.engine.bridge
        pane=self.window.findChild(QObject,'settingsPane');QMetaObject.invokeMethod(pane,'open');QTest.qWait(180)
        button=self.window.findChild(QObject,'settingsProductionMode')
        self.assertEqual(button.property('fieldIndex'),38)
        bridge.set_input_device('gamepad')
        pane.setProperty('selectedField',38);QTest.qWait(350)
        for value in (True,False):
            bridge.action.emit('activate')
            self.assertEqual(button.property('checked'),value)
            QMetaObject.invokeMethod(pane,'save')
            self.assertEqual(self.storage.read_settings()['production_mode'],value)
            self.assertEqual(bridge.settingsState['production_mode'],value)
        for value in (True,False):
            button.setProperty('checked',value);QMetaObject.invokeMethod(button,'clicked')
            QMetaObject.invokeMethod(pane,'save')
            self.assertEqual(self.storage.read_settings()['production_mode'],value)
            self.assertEqual(bridge.settingsState['production_mode'],value)
        self.assertFalse(bridge.setQuickOptions({'production_mode':'yes'}))
        QMetaObject.invokeMethod(pane,'close');QTest.qWait(160)
