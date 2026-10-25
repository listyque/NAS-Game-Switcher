import importlib.util
import json
from pathlib import Path
import types
import unittest
from unittest.mock import MagicMock,patch
from ngs.player_protocol import password_hash


class FirstRunOwnerTests(unittest.TestCase):
    def test_owner_is_provisioned_with_a_salted_password_before_registration_and_never_reset(self):
        path=Path(__file__).resolve().parents[1]/'server/container/first_run.py'
        spec=importlib.util.spec_from_file_location('ngs_first_run',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        backend=types.ModuleType('ngs_launcher_plugin.backend_players');backend.password_hash=password_hash
        package=types.ModuleType('ngs_launcher_plugin')
        db=MagicMock();cursor=db.cursor.return_value.__enter__.return_value
        with patch.dict('sys.modules',{'ngs_launcher_plugin':package,'ngs_launcher_plugin.backend_players':backend}):
            cursor.fetchone.return_value=(0,)
            self.assertTrue(module.create_owner(db,'a-private-nas-password'))
            command,parameters=cursor.execute.call_args.args
            self.assertIn('VALUES (%s,%s)',command);self.assertEqual(parameters[0],'admin')
            record=json.loads(parameters[1]);self.assertEqual(record['role'],'owner')
            self.assertNotIn('a-private-nas-password',parameters[1])
            self.assertEqual(record['password'],password_hash('a-private-nas-password',record['salt']))
            cursor.reset_mock();cursor.fetchone.return_value=(1,)
            self.assertFalse(module.create_owner(db,'a-different-password'))
            self.assertFalse(any('INSERT' in call.args[0] for call in cursor.execute.call_args_list))
