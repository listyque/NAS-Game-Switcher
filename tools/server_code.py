"""Keep TACTIC's template parser away from large, trusted NGS Python sources."""
import base64
import inspect
import zlib


def ngs_compile(source):
    import functools, sys, types
    name = "_ngs_launcher_compiled_code"
    module = sys.modules.get(name)
    if module is None:
        candidate = types.ModuleType(name)
        candidate.compile = functools.lru_cache(maxsize=16)(compile)
        module = sys.modules.setdefault(name, candidate)
    # Cache only code, never request data, credentials, security objects or query results.
    return module.compile(source, "<ngs_launcher>", "exec")


def packed_script(source, expression, parameters=""):
    program = (inspect.getsource(ngs_compile) + "\n" + source + "\ndef ngs_entry(" + parameters + "):\n    return " + expression + "\n")
    encoded = base64.b64encode(zlib.compress(program.encode("utf8"))).decode("ascii")
    return (inspect.getsource(ngs_compile) + "\nimport base64,zlib\nnamespace={}\n"
            + "exec(ngs_compile(zlib.decompress(base64.b64decode(" + repr(encoded) + ")).decode('utf8')),namespace)\n"
            + "return namespace['ngs_entry'](" + parameters + ")")
