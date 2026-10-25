"""Bounded Zstandard blocks; the portable client carries its own decoder."""

def open_compressed(source):
    try:
        from compression.zstd import ZstdFile
    except ImportError:
        from zstandard import ZstdDecompressor
        return ZstdDecompressor(max_window_size=8 * 1024 * 1024).stream_reader(source.open("rb"), closefd=True)
    else:
        return ZstdFile(source, "rb")


def zstd_writer(stream):
    """Use the NAS's existing libzstd; no server package installation is required."""
    import ctypes as c
    import ctypes.util
    lib = c.CDLL(ctypes.util.find_library("zstd") or "libzstd.so.1")
    class Buffer(c.Structure):
        _fields_ = [("data", c.c_void_p), ("size", c.c_size_t), ("pos", c.c_size_t)]
    lib.ZSTD_createCStream.restype = c.c_void_p
    lib.ZSTD_freeCStream.argtypes = [c.c_void_p]
    lib.ZSTD_initCStream.argtypes = [c.c_void_p, c.c_int]
    lib.ZSTD_initCStream.restype = c.c_size_t
    lib.ZSTD_compressStream2.argtypes = [c.c_void_p, c.POINTER(Buffer), c.POINTER(Buffer), c.c_int]
    lib.ZSTD_compressStream2.restype = c.c_size_t
    lib.ZSTD_isError.argtypes = [c.c_size_t]
    lib.ZSTD_isError.restype = c.c_uint
    context = lib.ZSTD_createCStream()
    if not context:
        raise MemoryError("Zstandard context allocation failed")
    if lib.ZSTD_isError(lib.ZSTD_initCStream(context, 1)):
        lib.ZSTD_freeCStream(context)
        raise ValueError("Zstandard initialization failed")
    output = c.create_string_buffer(1024 * 1024)
    class Writer:
        closed = False
        def write(self, data, end=0):
            incoming = Buffer(c.cast(c.c_char_p(data), c.c_void_p), len(data), 0)
            while True:
                outgoing = Buffer(c.cast(output, c.c_void_p), len(output), 0)
                result = lib.ZSTD_compressStream2(context, c.byref(outgoing), c.byref(incoming), end)
                if lib.ZSTD_isError(result):
                    raise ValueError("Zstandard compression failed")
                if outgoing.pos:
                    stream.write(output[:outgoing.pos])
                if incoming.pos == incoming.size and (not end or result == 0):
                    break
        def finish(self):
            try:
                self.write(b"", 2)
            finally:
                self.close()
        def close(self):
            if not self.closed:
                lib.ZSTD_freeCStream(context)
                self.closed = True
    return Writer()
