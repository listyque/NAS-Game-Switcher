"""Windows CNG RSA/SHA256 signatures; no persisted Windows key store."""

import ctypes
import hashlib


class RsaKey:
    def __init__(self, blob=None, private=False):
        self.api = ctypes.WinDLL("bcrypt")
        pointer, ulong, handle = ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p
        definitions = {
            "BCryptOpenAlgorithmProvider": [ctypes.POINTER(handle), ctypes.c_wchar_p, ctypes.c_wchar_p, ulong],
            "BCryptCloseAlgorithmProvider": [handle, ulong],
            "BCryptGenerateKeyPair": [handle, ctypes.POINTER(handle), ulong, ulong],
            "BCryptFinalizeKeyPair": [handle, ulong],
            "BCryptImportKeyPair": [handle, handle, ctypes.c_wchar_p, ctypes.POINTER(handle), pointer, ulong, ulong],
            "BCryptExportKey": [handle, handle, ctypes.c_wchar_p, pointer, ulong, ctypes.POINTER(ulong), ulong],
            "BCryptSignHash": [handle, pointer, pointer, ulong, pointer, ulong, ctypes.POINTER(ulong), ulong],
            "BCryptVerifySignature": [handle, pointer, pointer, ulong, pointer, ulong, ulong],
            "BCryptDestroyKey": [handle],
        }
        for name, arguments in definitions.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = arguments, ctypes.c_long
        self.algorithm, self.key = handle(), handle()
        try:
            self.check(self.api.BCryptOpenAlgorithmProvider(ctypes.byref(self.algorithm), "RSA", None, 0))
            if blob is None:
                self.check(self.api.BCryptGenerateKeyPair(self.algorithm, ctypes.byref(self.key), 2048, 0))
                self.check(self.api.BCryptFinalizeKeyPair(self.key, 0))
            else:
                data = ctypes.create_string_buffer(blob)
                self.check(self.api.BCryptImportKeyPair(self.algorithm, None,
                    "RSAPRIVATEBLOB" if private else "RSAPUBLICBLOB", ctypes.byref(self.key), data, len(blob), 0))
        except Exception:
            self.close()
            raise

    @staticmethod
    def check(status):
        if status < 0:
            raise ValueError("Windows rejected the update signing key or signature (0x%08x)." % (status & 0xffffffff))

    def export(self, private=False):
        kind, length = "RSAPRIVATEBLOB" if private else "RSAPUBLICBLOB", ctypes.c_ulong()
        self.check(self.api.BCryptExportKey(self.key, None, kind, None, 0, ctypes.byref(length), 0))
        output = ctypes.create_string_buffer(length.value)
        self.check(self.api.BCryptExportKey(self.key, None, kind, output, len(output), ctypes.byref(length), 0))
        return output.raw[:length.value]

    def sign(self, payload):
        padding = ctypes.c_wchar_p("SHA256")
        digest = ctypes.create_string_buffer(hashlib.sha256(payload).digest())
        output, length = ctypes.create_string_buffer(256), ctypes.c_ulong()
        self.check(self.api.BCryptSignHash(self.key, ctypes.byref(padding), digest, 32, output, len(output),
                                         ctypes.byref(length), 2))
        return output.raw[:length.value]

    def verify(self, payload, signature):
        if len(signature) != 256:
            raise ValueError("Invalid update signature length")
        padding = ctypes.c_wchar_p("SHA256")
        digest = ctypes.create_string_buffer(hashlib.sha256(payload).digest())
        signature = ctypes.create_string_buffer(signature)
        self.check(self.api.BCryptVerifySignature(self.key, ctypes.byref(padding), digest, 32, signature, 256, 2))

    def close(self):
        if self.key:
            self.api.BCryptDestroyKey(self.key)
            self.key = None
        if self.algorithm:
            self.api.BCryptCloseAlgorithmProvider(self.algorithm, 0)
            self.algorithm = None

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()
