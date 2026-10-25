import time
import unittest
from unittest.mock import Mock, patch
from ngs.input_host import validate_edge_trace
from tests import test_edge_swipe


class EdgeTraceTests(unittest.TestCase):
    def manager(self):
        manager=test_edge_swipe.EdgeSwipeTests().native_edges()
        manager.backings={};manager.tick_count=Mock(return_value=1270)
        manager.user.GetMessageTime.return_value=1234
        manager.trace_until=time.monotonic()+180;manager.trace_count=0;manager.trace_moves=set();manager.trace_contacts=set()
        manager.controller.keyboard.bridge.settings={}
        manager.controller.enabled=True;manager.controller.remoteHost=False
        manager.controller.shown=False;manager.controller.sleeping=False
        manager.handles[17].winId.return_value=17
        manager.user.WindowFromPoint.return_value=17;manager.user.GetForegroundWindow.return_value=55
        manager.user.GetCapture.return_value=0
        manager.user.ClientToScreen.return_value=True
        manager.user.GetWindowThreadProcessId.side_effect=lambda hwnd,pid:setattr(pid._obj,'value',101 if hwnd==17 else 202)
        return manager

    def test_trace_preserves_pointer_delivery_and_records_actual_receiver_and_primary(self):
        manager=self.manager()
        def info(identity,output):
            p=output._obj;p.kind=2;p.identity=identity;p.flags=0x12006;p.pixel.x=-1800;p.pixel.y=6
            return True
        manager.user.GetPointerInfo.side_effect=info
        with patch.object(manager,'message',return_value=(True,73)) as receive:
            self.assertEqual(manager.window_message(17,0x246,7,0,0,0),73)
            receive.assert_called_once_with(17,0x246,7,0)
        data=manager.dispatch.traced.emit.call_args.args[0]
        self.assertEqual(validate_edge_trace(data),data)
        self.assertEqual((data['stage'],data['pointer'],data['flags']),('pointer_down',7,0x12006))
        self.assertEqual((data['target_pid'],data['foreground_pid'],data['target_self']),(101,202,1))
        self.assertEqual(data['x'],80);self.assertEqual(data['y'],4)

    def test_only_touch_promoted_mouse_is_observed_and_never_consumed(self):
        manager=self.manager()
        for message in (0x201,0x200,0x202):manager.trace_native(17,message,0,8<<16|120)
        manager.dispatch.traced.emit.assert_not_called()
        manager.user.GetMessageExtraInfo.return_value=0xff515780
        for message in (0x201,0x200,0x200,0x202):manager.trace_native(17,message,0,8<<16|120)
        self.assertEqual([c.args[0]['stage'] for c in manager.dispatch.traced.emit.call_args_list],['mouse_touch_down','mouse_touch_move','mouse_touch_up'])
        self.assertEqual(manager.message(17,0x201,0,0),(False,0))

    def test_launcher_pointer_below_top_band_records_source_age_and_preserves_delivery(self):
        manager=self.manager();manager.backings=manager.handles;manager.handles={}
        def info(identity,output):
            p=output._obj;p.kind=2;p.identity=identity;p.flags=0x12006;p.pixel.x=-1800;p.pixel.y=900;p.time=1234
            return True
        manager.user.GetPointerInfo.side_effect=info
        for message in (0x246,0x245,0x245,0x247): manager.trace_native(17,message,7,0)
        records=[call.args[0] for call in manager.dispatch.traced.emit.call_args_list]
        self.assertEqual([r['stage'] for r in records],['pointer_down','pointer_move','pointer_up'])
        for data in records:
            self.assertEqual(validate_edge_trace(data),data)
            self.assertEqual((data['clock_ms'],data['age_ms'],data['y']),(1270,36,600))
        self.assertEqual(manager.message(17,0x245,7,0),(False,0))

    def test_legacy_packets_are_sampled_once_and_never_read_or_closed_by_trace(self):
        manager=self.manager()
        for unused in range(300):manager.trace_native(17,0x240,1,123)
        manager.dispatch.traced.emit.assert_called_once()
        manager.user.GetTouchInputInfo.assert_not_called()
        manager.user.CloseTouchInputHandle.assert_not_called()
        self.assertEqual(manager.trace_count,1)

    def test_packet_clock_wrap_and_unavailable_timestamp(self):
        for clock,stamp,age in ((5,0xfffffffa,11),(1270,0,-1),(70000,1,-1)):
            with self.subTest(clock=clock,stamp=stamp):
                manager=self.manager();manager.tick_count.return_value=clock
                manager.trace_input(manager.handles[17],'snapshot',timestamp=stamp)
                data=manager.dispatch.traced.emit.call_args.args[0]
                self.assertEqual(data['age_ms'],age);self.assertEqual(validate_edge_trace(data),data)

    def test_production_timeout_and_budget_stop_all_native_queries(self):
        for stop in ('production','time','budget'):
            with self.subTest(stop=stop):
                manager=self.manager()
                if stop=='production':manager.controller.keyboard.bridge.settings['production_mode']=True
                if stop=='time':manager.trace_until=0
                if stop=='budget':manager.trace_count=256
                manager.trace_native(17,0x246,7,0)
                manager.trace_input(manager.handles[17],'snapshot')
                manager.tick_count.assert_not_called()
                manager.user.GetPointerInfo.assert_not_called()
                manager.user.WindowFromPoint.assert_not_called()
                manager.dispatch.traced.emit.assert_not_called()

    def test_snapshot_failure_cannot_skip_original_window_message(self):
        manager=self.manager();manager.user.WindowFromPoint.side_effect=OSError('fixture')
        with patch.object(manager,'message',return_value=(True,73)) as receive:
            self.assertEqual(manager.window_message(17,0x6,1,0,0,0),73)
            receive.assert_called_once()

    def test_ipc_metadata_rejects_extra_text_invalid_values_and_unknown_types(self):
        manager=self.manager();manager.trace_input(manager.handles[17],'snapshot')
        data=manager.dispatch.traced.emit.call_args.args[0]
        self.assertEqual(validate_edge_trace(data),data)
        for bad in (None,{},dict(data,text='field content'),dict(data,stage='execute'),dict(data,stage=[]),dict(data,decision={}),dict(data,x=float('nan')),dict(data,flags=True),dict(data,target_pid=2**64),dict(data,state=32),dict(data,age_ms=60001),dict(data,clock_ms=-1)):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):validate_edge_trace(bad)

    def test_validated_trace_reaches_launcher_diagnostics(self):
        from ngs.input_host import InputHostClient
        from types import SimpleNamespace
        manager=self.manager();manager.trace_input(manager.handles[17],'snapshot')
        data=manager.dispatch.traced.emit.call_args.args[0]
        client=SimpleNamespace(portable=True,bridge=SimpleNamespace(process=None))
        with patch('ngs.diagnostics.event') as event:
            InputHostClient.receive(client,dict(kind='edgeInput',source='trace',flags=0,contacts=0,enabled=True,trace=data))
            event.assert_called_once_with('input.edge_trace',**data,portable=True,game_running=False)
