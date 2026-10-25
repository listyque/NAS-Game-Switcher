"""Independent TAR/Zstandard packets, produced without a preliminary inventory."""


def build_archives(source_root, assets, card, launch, helpers, chunk_size=None, cancel=None, progress=lambda *args: None, pipeline=None):
    import hashlib, json, os, shutil, stat, tarfile, tempfile, threading
    from pathlib import Path
    ns = helpers
    chunk_size = chunk_size or ns['PACKAGE_SIZE']
    cancel = cancel or threading.Event()
    source = ns['safe_path'](Path(source_root), card['source_folder'])
    ns['check_pe'](ns['safe_path'](source, card['executable']))
    document = dict(schema=2, format='tar-zstd', game_id=card['id'], source_folder=card['source_folder'],
        executable=card['executable'], arguments=launch.get('arguments', []),
        working_directory=launch.get('working_directory', ''), total_size=card.get('total_size', 0))
    ns['validate_archive_document'](document, card)
    info = source.stat()
    pipeline('plan', dict(document=document, source_fingerprint=hashlib.sha256(json.dumps(
        [str(source), info.st_dev, info.st_ino, card['revision']]).encode()).hexdigest()))
    folder = ns['safe_path'](Path(assets), 'chunks'); folder.mkdir(exist_ok=True)
    if shutil.disk_usage(folder).free < 3*(chunk_size+1024**2)+512*1024**2:
        raise OSError("The NAS does not have enough space for packages and a 512 MiB reserve.")
    chunks, stream, temporary, encoder, archive = [], None, None, None, None
    encoded_digest, raw_digest, raw_size = None, None, 0
    done, files, directories, exe_digest = 0, 0, 0, None
    writeback=None
    if os.name=='posix':
        import ctypes
        writeback=getattr(ctypes.CDLL(None),'sync_file_range',None)
        if writeback is not None:
            writeback.argtypes=[ctypes.c_int,ctypes.c_longlong,ctypes.c_longlong,ctypes.c_uint]
            writeback.restype=ctypes.c_int
    class Output:
        position,flushed=0,0
        def write(self, data):
            nonlocal writeback
            stream.write(data);encoded_digest.update(data);self.position+=len(data)
            if writeback is not None and self.position-self.flushed>=8*1024**2:
                stream.flush()
                if writeback(stream.fileno(),self.flushed,self.position-self.flushed,2):writeback=None
                self.flushed=self.position
    class ArchiveOutput:
        def write(self, data):
            nonlocal raw_size
            ns['check_cancel'](cancel)
            raw_digest.update(data); raw_size += len(data); encoder.write(data)
            return len(data)
    def start():
        nonlocal stream, temporary, encoder, archive, encoded_digest, raw_digest, raw_size
        stream = tempfile.NamedTemporaryFile(dir=folder, prefix='.package-', delete=False)
        temporary = Path(stream.name); encoded_digest, raw_digest, raw_size = hashlib.sha256(), hashlib.sha256(), 0
        encoder = ns['zstd_writer'](Output())
        archive = tarfile.open(fileobj=ArchiveOutput(), mode='w|', format=tarfile.PAX_FORMAT,
            bufsize=64*1024, copybufsize=64*1024)
    def finish():
        nonlocal stream, temporary, encoder, archive
        if len(chunks)>=100_000:raise ValueError('Too many archive packets')
        archive.close(); archive = None
        encoder.finish(); encoder = None
        stream.flush(); os.fsync(stream.fileno()); stream.close(); stream = None
        if raw_size > chunk_size: raise ValueError('Oversized TAR packet')
        digest = encoded_digest.hexdigest(); target = ns['safe_path'](folder, digest+'.bin')
        encoded_size = temporary.stat().st_size
        if pipeline('capacity', dict(index=len(chunks))) is not False:
            try:
                if os.name == 'nt': temporary.rename(target); temporary = None
                else: os.link(temporary, target)
            except FileExistsError:
                if target.stat().st_size != encoded_size or ns['hash_file'](target, cancel) != digest:
                    raise ValueError('Existing archive packet changed')
        if temporary is not None: temporary.unlink(); temporary = None
        chunk = dict(path='/assets/ngs_launcher/package_jobs/'+Path(assets).name+'/chunks/'+digest+'.bin',
            size=encoded_size, sha256=digest, raw_size=raw_size, raw_sha256=raw_digest.hexdigest(), codec='zstd')
        chunks.append(chunk); pipeline('chunk', dict(index=len(chunks)-1, chunk=chunk))
    def walking(relative=''):
        with os.scandir(ns['safe_path'](source, relative)) as entries:
            entries = sorted((e for e in entries if e.name not in ('@eaDir', '#recycle')),
                key=lambda e: e.name+('/' if e.is_dir(follow_symlinks=False) else ''))
        names = set()
        for entry in entries:
            ns['check_cancel'](cancel)
            name = ns['relative_game_path'](relative+'/'+entry.name if relative else entry.name)
            if entry.name.casefold() in names: raise ValueError('Source paths differ only by case')
            names.add(entry.name.casefold())
            info = entry.stat(follow_symlinks=False)
            if ns['is_link'](info): raise ValueError("Links are not allowed in the source game.")
            if stat.S_ISDIR(info.st_mode):
                yield name, info
                yield from walking(name)
            elif stat.S_ISREG(info.st_mode): yield name, info
            else: raise ValueError('Unsupported source entry')
    try:
        for name, info in walking():
            directory = stat.S_ISDIR(info.st_mode)
            if directory:
                directories += 1
                if directories > 200_000: raise ValueError('Too many directories')
                member = tarfile.TarInfo(name); member.type = tarfile.DIRTYPE; member.mode = 0o755
                if archive is None: start()
                if archive.offset+len(member.tobuf(tarfile.PAX_FORMAT))+10240 > chunk_size: finish(); start()
                archive.addfile(member)
                continue
            files += 1
            if files > 200_000: raise ValueError('Too many files')
            digest = hashlib.sha256() if name == card['executable'] else None
            with ns['safe_path'](source, name).open('rb') as incoming:
                before = os.fstat(incoming.fileno())
                if (before.st_size, before.st_mtime_ns) != (info.st_size, info.st_mtime_ns):
                    raise ValueError("The source game changed during packaging.")
                offset, remaining = 0, info.st_size
                class Reader:
                    def read(self, size):
                        nonlocal done
                        ns['check_cancel'](cancel)
                        data = incoming.read(size)
                        if digest is not None: digest.update(data)
                        done += len(data); progress(done, document['total_size'])
                        return data
                while remaining or offset == 0:
                    if archive is None: start()
                    member = tarfile.TarInfo(name); member.mode = 0o644
                    member.pax_headers = {'NGS.offset': str(offset), 'NGS.size': str(info.st_size)}
                    member.size = remaining
                    capacity = ((chunk_size-archive.offset-len(member.tobuf(tarfile.PAX_FORMAT))-10240)//512)*512
                    if capacity <= 0: finish(); continue
                    member.size = min(remaining, capacity)
                    archive.addfile(member, Reader())
                    offset += member.size; remaining -= member.size
                    if not remaining: break
                after = os.fstat(incoming.fileno())
                if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
                    raise ValueError("A source file changed while packaging.")
                if digest is not None: exe_digest = digest.hexdigest()
        if archive is not None: finish()
        if exe_digest is None: raise ValueError('EXE not found in archive')
        document.update(total_size=done, file_count=files, executable_sha256=exe_digest,
            archive_size=sum(chunk['raw_size'] for chunk in chunks))
        ns['validate_archive_document'](document, card, final=True)
        raw = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
        return dict(document=document, transport=dict(schema=2, manifest_sha256=hashlib.sha256(raw).hexdigest(), chunks=chunks))
    finally:
        if encoder: encoder.close()
        if stream: stream.close()
        if temporary: temporary.unlink(missing_ok=True)


def repair_archive_block(folder, number, card, ns):
    import json
    from pathlib import Path
    metadata = ns['safe_path'](folder, 'block-%d.json'%number)
    if metadata.stat().st_size > 8192: raise ValueError('Oversized stream block')
    expected = json.loads(metadata.read_bytes())['chunk']
    target = ns['safe_path'](folder, 'chunks/'+expected['sha256']+'.bin')
    if expected['path'] != '/assets/ngs_launcher/package_jobs/'+folder.name+'/chunks/'+target.name:
        raise ValueError('Foreign archive block')
    if target.exists(): return
    plan_path = ns['safe_path'](folder, 'plan.json')
    if plan_path.stat().st_size > 8192: raise ValueError('Oversized archive plan')
    document = ns['validate_archive_document'](json.loads(plan_path.read_bytes())['document'], card)
    class Finished(Exception): pass
    def pipeline(event, value):
        if event == 'capacity': return value['index'] == number
        if event == 'chunk' and value['index'] == number:
            if value['chunk'] != expected:
                ns['safe_path'](folder, 'chunks/'+value['chunk']['sha256']+'.bin').unlink(missing_ok=True)
                raise ValueError("The source game has changed; the previous package was not restored.")
            raise Finished()
    try:
        ns['build_archives'](Path('/mnt/ngs-installers' if card.get('kind')=='installer' else '/mnt/ngs-games'),
            folder, card, document, ns, pipeline=pipeline)
    except Finished: return
    raise ValueError('Archive block not found')
