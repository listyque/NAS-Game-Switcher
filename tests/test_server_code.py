import ast
import base64
import textwrap
import unittest
import zlib

from tools.server_code import ngs_compile, packed_script


class ServerCodeTests(unittest.TestCase):
    def test_packed_entry_keeps_request_values_out_of_the_code_cache(self):
        source = 'def action(value):\n    return value, globals().get("previous")\n'
        wrapper = packed_script(source, 'action(value)', 'value')
        namespace = {}
        exec('def run(value):\n' + textwrap.indent(wrapper, '    '), namespace)
        for value in ('first-device-secret', 'second-device-secret'):
            self.assertEqual(namespace['run'](value), (value, None))
            self.assertNotIn(value, wrapper)
        changed = packed_script(source.replace('return value,', 'return value.upper(),'), 'action(value)', 'value')
        exec('def run(value):\n' + textwrap.indent(changed, '    '), namespace)
        self.assertEqual(namespace['run']('changed'), ('CHANGED', None))

    def test_compile_cache_is_bounded_and_exec_uses_fresh_namespaces(self):
        code = ngs_compile('values = []')
        self.assertIs(code, ngs_compile('values = []'))
        first, second = {}, {}
        exec(code, first); first['values'].append('request-data')
        exec(code, second)
        self.assertEqual(second['values'], [])
        for i in range(24):
            ngs_compile('value = ' + str(i))
        import sys
        self.assertLessEqual(sys.modules['_ngs_launcher_compiled_code'].compile.cache_info().currsize, 16)

    def test_all_saved_entries_are_compact_and_contain_reviewed_python(self):
        from tools.metadata_api import api_script_source
        from tools.remote_catalog import remote_source
        for wrapper in (api_script_source(), remote_source()):
            tree = ast.parse('def saved():\n' + textwrap.indent(wrapper, '    '))
            encoded = next(node.args[0].value for node in ast.walk(tree)
                           if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                           and node.func.attr == 'b64decode')
            raw = zlib.decompress(base64.b64decode(encoded)).decode('utf8')
            ast.parse(raw)
            self.assertLess(len(wrapper), len(raw) // 2)
            self.assertIn('ngs_catalog_worker', raw)
            self.assertNotIn('update-signing-key.bin', raw)
