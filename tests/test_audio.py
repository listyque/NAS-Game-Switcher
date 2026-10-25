import ctypes
import unittest
from unittest.mock import patch

from ngs import audio


class MixerTests(unittest.TestCase):
    def setUp(self):
        self.levels = [0.4, 0.7]; self.muted = [False, True]; self.released = []
        def create(clsid, iid, out): out.value = 1
        def text(pointer, slot): return {10:'speakers', 11:'headphones', 40:'music', 41:'game'}[pointer.value]
        def call(pointer, slot, types=(), args=()):
            p = pointer.value
            def put(value): args[-1]._obj.value = value
            if slot == 2: self.released.append(p)
            elif p == 1 and slot == 3: put(2)
            elif p == 2 and slot == 3: put(2)
            elif p == 2 and slot == 4: put(10 + args[0])
            elif p == 1 and slot == 4: put(10)
            elif p == 10 and slot == 3: put(20)
            elif p == 20 and slot == 5: put(21)
            elif p == 21 and slot == 3: put(2)
            elif p == 21 and slot == 4: put(30 + args[0])
            elif p in (30, 31) and slot == 3: put(0)
            elif p in (30, 31) and slot == 0: put(p + (10 if bytes(args[0]) == bytes(audio._guid('bfb7ff88-7239-4fc9-8fa2-07c950be9c6d')) else 20))
            elif p in (40, 41) and slot == 14: put(p + 100)
            elif p in (50, 51):
                i = p - 50
                if slot == 3: self.levels[i] = args[0]
                elif slot == 4: put(self.levels[i])
                elif slot == 5: self.muted[i] = bool(args[0])
                elif slot == 6: put(int(self.muted[i]))
                else: raise AssertionError((p, slot))
            else: raise AssertionError((p, slot))
        for target, value in [('ngs.audio._create', create), ('ngs.audio._call', call), ('ngs.system_status._call', call),
                              ('ngs.audio._text', text), ('ngs.audio._name', lambda p: 'Output ' + str(p.value)),
                              ('ngs.audio._process_name', lambda pid: 'Process ' + str(pid)),
                              ('ngs.audio._volume', lambda: dict(volume=80, muted=False))]:
            context = patch(target, value); context.start(); self.addCleanup(context.stop)

    def test_sessions_keep_identity_and_only_targeted_volume_or_mute_changes(self):
        state = audio.mixer('session_set', dict(endpoint='speakers', session='game', value=25))
        self.assertEqual([s['volume'] for s in state['sessions']], [40, 25])
        self.assertEqual(state['sessions'][1]['id'], 'game')
        self.assertTrue(state['outputs'][0]['selected']); self.assertFalse(state['outputs'][1]['selected'])
        state = audio.mixer('session_mute', dict(endpoint='speakers', session='music', value=True))
        self.assertTrue(state['sessions'][0]['muted']); self.assertTrue(state['sessions'][1]['muted'])
        self.assertEqual(state['volume'], 80)
        for pointer in (1, 2, 10, 11, 20, 21, 30, 31, 40, 41, 50, 51): self.assertIn(pointer, self.released)

    def test_hotplug_or_closed_session_does_not_change_another_application(self):
        for endpoint, session in [('headphones', 'game'), ('speakers', 'closed')]:
            with self.assertRaises(ValueError): audio.mixer('session_set', dict(endpoint=endpoint, session=session, value=10))
        self.assertEqual(self.levels, [0.4, 0.7]); self.assertIn(1, self.released)
        with self.assertRaises(ValueError): audio.mixer('output', 'unplugged')

    def test_invalid_requests_do_not_create_com_objects(self):
        with patch.object(audio, '_create') as create:
            for action, value in [('output', ''), ('session_set', {}), ('session_set', dict(endpoint='a', session='b', value=float('nan'))),
                                  ('session_mute', dict(endpoint='a', session='b', value=1)), ('run', 'x')]:
                with self.assertRaises(ValueError): audio.mixer(action, value)
            create.assert_not_called()

    def test_output_switch_restores_first_role_if_second_fails(self):
        from contextlib import contextmanager
        @contextmanager
        def pointer(): yield ctypes.c_void_p()
        writes = []
        def call(p, slot, types, args):
            if slot == 4: args[-1]._obj.value = 10 + args[1]
            else:
                writes.append(args)
                if args == ('new', 1): raise OSError('endpoint lost')
        with patch.object(audio, '_pointer', pointer), patch.object(audio, '_call', call):
            with self.assertRaisesRegex(OSError, 'endpoint lost'): audio._switch(ctypes.c_void_p(1), 'new')
        self.assertEqual(writes, [('new', 0), ('new', 1), ('speakers', 0)])

    def test_property_store_abi(self):
        self.assertEqual(ctypes.sizeof(audio.PropertyKey), 20)
        self.assertEqual(ctypes.sizeof(audio.PropVariant), 24)
