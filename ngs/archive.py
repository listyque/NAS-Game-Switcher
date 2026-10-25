"""Receive self-describing TAR packets; no game inventory or file rehash pass."""

from ngs.install import validate_manifest


def validate_archive_document(document, card=None, final=False):
    import re
    fields = {'schema','format','game_id','source_folder','executable','working_directory','arguments','total_size'}
    if final: fields |= {'archive_size','file_count','executable_sha256'}
    if (not isinstance(document, dict) or set(document)-{'http'} != fields
            or type(document.get('schema')) is not int or document['schema'] != 2 or document.get('format') != 'tar-zstd'
            or type(document.get('total_size')) is not int or not 0 <= document['total_size'] <= 2**50):
        raise ValueError('Invalid streaming archive description')
    probe = {key:document[key] for key in ('game_id','source_folder','executable','working_directory','arguments')}
    probe.update(schema=1, total_size=0, files=[dict(path=document['executable'],size=0,sha256='0'*64)],
                 directories=[document['working_directory']] if document['working_directory'] else [])
    validate_manifest(probe, card)
    if final and (type(document['archive_size']) is not int or not 0 < document['archive_size'] <= 2**50
            or type(document['file_count']) is not int or not 1 <= document['file_count'] <= 200_000
            or not isinstance(document['executable_sha256'], str) or not re.fullmatch('[0-9a-f]{64}', document['executable_sha256'])):
        raise ValueError('Invalid final streaming archive description')
    return document


def unpack_archive(part, chunk, payload, cancel, progress, done, total, replay=False):
    import hashlib, os, tarfile, time
    from ngs.compression import open_compressed
    from ngs.install import BLOCK, RECEIPT, check_cancel, safe_path
    digest, raw_bytes, added, files, entries = hashlib.sha256(), 0, 0, 0, 0
    heartbeat=time.monotonic()
    with open_compressed(part) as incoming:
        class Reader:
            def read(self, size=-1):
                nonlocal raw_bytes
                check_cancel(cancel)
                data = incoming.read(size)
                raw_bytes += len(data)
                if raw_bytes > chunk['raw_size']: raise ValueError('Oversized unpacked archive')
                digest.update(data)
                return data
        reader = Reader()
        with tarfile.open(fileobj=reader, mode='r|', bufsize=64*1024) as archive:
            for member in archive:
                check_cancel(cancel)
                if time.monotonic()-heartbeat>=1:
                    progress('unpack',done+added,total,member.name);heartbeat=time.monotonic()
                entries += 1
                if entries > 400_000 or member.issparse() or not (member.isdir() or member.isfile()):
                    raise ValueError('Unsupported TAR entry')
                name = member.name.rstrip('/') if member.isdir() else member.name
                if name.split('/')[0].casefold() == RECEIPT: raise ValueError('Reserved archive path')
                target = safe_path(payload, name)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                headers = member.pax_headers
                if any(key not in {'path','NGS.offset','NGS.size'} for key in headers):
                    raise ValueError('Unsupported TAR metadata')
                offset, size = headers.get('NGS.offset'), headers.get('NGS.size')
                if (not isinstance(offset,str) or not offset.isascii() or not offset.isdecimal() or len(offset)>16
                        or not isinstance(size,str) or not size.isascii() or not size.isdecimal() or len(size)>16):
                    raise ValueError('Invalid archive segment')
                offset, size = int(offset), int(size)
                if not 0 <= offset <= offset+member.size <= size <= 2**50:
                    raise ValueError('Archive segment outside file')
                target.parent.mkdir(parents=True, exist_ok=True)
                exists = target.exists()
                if exists and (not target.is_file() or target.stat().st_nlink != 1): raise ValueError('Unsafe archive target')
                if offset:
                    if not exists or not offset <= target.stat().st_size <= size: raise ValueError('Missing preceding archive segment')
                elif exists and not replay:
                    raise ValueError('Duplicate archive file')
                with archive.extractfile(member) as source, target.open('r+b' if exists else 'xb') as output:
                    output.seek(offset)
                    remaining = member.size
                    try:
                        while remaining:
                            check_cancel(cancel)
                            data = source.read(min(BLOCK,remaining))
                            if not data: raise ValueError('Truncated TAR file')
                            output.write(data); remaining -= len(data); added += len(data)
                            progress('io',0,len(data),name)
                            progress('unpack',done+added,total,name)
                        if offset+member.size == size: output.truncate(size)
                    finally:
                        output.flush(); os.fsync(output.fileno())
                if offset == 0: files += 1
        while reader.read(BLOCK): pass
    if raw_bytes != chunk['raw_size'] or digest.hexdigest() != chunk['raw_sha256']:
        raise ValueError("Extracted HTTP package integrity check failed.")
    return added, files


def install_archive(storage, card, document, stage, cancel):
    import hashlib, json
    from ngs.install import check_cancel, commit_install, install_destination, installation_lock, safe_path
    from ngs.packages import verify_transport
    validate_archive_document(document,card,final=True)
    from ngs.server_trust import server_key_path
    verify_transport(document,server_key_path(storage).read_bytes())
    with installation_lock(storage):
        check_cancel(cancel)
        stage=safe_path(storage.game_staging,stage.name);payload=safe_path(stage,'payload')
        checkpoint=safe_path(stage,'archive-state.json')
        if checkpoint.stat().st_size>8192:raise ValueError('Oversized archive checkpoint')
        state=json.loads(checkpoint.read_bytes())
        if (state.get('index')!=len(document['http']['chunks'])-1
                or state.get('completed_bytes')!=document['total_size'] or state.get('file_count')!=document['file_count']
                or state.get('archive_bytes')!=document['archive_size'] or (stage/'archive-extracting.json').exists()):
            raise ValueError('Archive extraction is incomplete')
        destination=install_destination(storage,card)
        if destination.exists():raise FileExistsError("The local game folder already exists; overwriting is not allowed.")
        if not safe_path(payload,document['executable']).is_file():raise ValueError('EXE missing after archive extraction')
        raw=json.dumps(document,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
        return commit_install(storage,card,document,payload,destination,document['executable_sha256'],
                              document['file_count'],hashlib.sha256(raw).hexdigest(),cancel)


def receive_archive(storage, cache, card, stage, token, session, plan, reference, pin, journal, cancel, progress, pool, call, check_server):
    import json, queue, shutil, threading, time
    from ngs.catalog import atomic_write, asset_path
    from ngs.install import INSTALL_SPACE_RESERVE, MAX_MANIFEST, check_cancel, safe_path
    from ngs.on_demand import PREFETCH_BLOCKS, WORKER_POLL_SECONDS, fetch_reference
    from ngs.packages import PACKAGE_SIZE, download_chunk, verify_stream_block, verify_transport
    document = validate_archive_document(plan['document'],card)
    from ngs.server_trust import server_key_path
    public = server_key_path(storage, cache.origin).read_bytes()
    atomic_write(pin,json.dumps(dict(origin=cache.origin,card=card,plan=reference)).encode())
    payload = safe_path(stage,'payload'); folder = safe_path(stage,'http'); folder.mkdir(exist_ok=True)
    checkpoint = safe_path(stage,'archive-state.json'); extracting = safe_path(stage,'archive-extracting.json')
    state = dict(plan=reference['sha256'],index=-1,completed_bytes=0,file_count=0,archive_bytes=0)
    if checkpoint.exists():
        if checkpoint.stat().st_size>8192: raise ValueError('Oversized archive checkpoint')
        saved=json.loads(checkpoint.read_bytes())
        if (set(saved)!=set(state) or saved['plan']!=state['plan'] or type(saved['index']) is not int
                or not -1<=saved['index']<100_000 or any(type(saved[k]) is not int or not 0<=saved[k]<=2**50
                    for k in ('completed_bytes','file_count','archive_bytes')) or saved['file_count']>200_000):
            raise ValueError('Invalid archive checkpoint')
        state=saved
    ack=state['index']; total=document['total_size']
    workspace=(PREFETCH_BLOCKS+1)*(PACKAGE_SIZE+1024**2)
    if shutil.disk_usage(payload).free < max(0,total-state['completed_bytes'])+workspace+INSTALL_SPACE_RESERVE:
        raise OSError("Not enough space for the game, HTTP packages and a 512 MiB reserve.")
    progress('download_size',-1,total,'')
    progress('files',state['completed_bytes'],total,'')
    messages=[]
    if journal.exists():
        if journal.stat().st_size>MAX_MANIFEST: raise ValueError('Oversized block journal')
        messages=json.loads(journal.read_bytes())
        if not isinstance(messages,list) or len(messages)>100_000: raise ValueError('Invalid block journal')
        for index,message in enumerate(messages):verify_stream_block(message,reference['sha256'],index,public)
    if ack>=len(messages): raise ValueError('Archive checkpoint exceeds signed journal')
    if extracting.exists():
        if extracting.stat().st_size>8192:raise ValueError('Oversized extraction marker')
        marker=json.loads(extracting.read_bytes())
        if marker.get('index')==ack and ack>=0:
            if marker!={'plan':reference['sha256'],'index':ack,'sha256':messages[ack]['chunk']['sha256']}:
                raise ValueError('Invalid completed extraction marker')
            extracting.unlink()
            safe_path(folder,messages[ack]['chunk']['sha256']+'.part').unlink(missing_ok=True)
    allowed={m['chunk']['sha256']+'.part' for m in messages}
    for path in folder.iterdir():
        safe_path(folder,path.name)
        if path.name not in allowed or not path.is_file():raise ValueError('Foreign archive download file')
    records=threading.Lock(); ready=queue.Queue(); slots=threading.BoundedSemaphore(PREFETCH_BLOCKS)
    final=None; pending=None
    def label(index):return "Package %d%s"%(index+1," of %d"%len(final['http']['chunks']) if final else " · total size pending")
    def remember(message,index):
        with records:
            if index<len(messages):
                if messages[index]!=message:raise ValueError("The saved HTTP package has changed.")
            elif index==len(messages):messages.append(message);atomic_write(journal,json.dumps(messages).encode())
            else:raise ValueError('Skipped archive block')
    def download(message,index):
        chunk=verify_stream_block(message,reference['sha256'],index,public);remember(message,index)
        part=safe_path(folder,chunk['sha256']+'.part')
        def report(phase,done,total,name):
            if phase=='waiting':progress(phase,done,total,label(index)+' · '+name)
        try:download_chunk(cache,chunk,part,cancel,report,received=lambda n:progress('io',n,0,label(index)))
        except OSError as error:
            if '404' not in str(error):raise
            call(dict(action='repair',preview=token,index=index))
            download_chunk(cache,chunk,part,cancel,report,received=lambda n:progress('io',n,0,label(index)))
        info=part.stat()
        return message,(info.st_size,info.st_mtime_ns)
    def ahead(path,index):
        while True:
            while not slots.acquire(timeout=.1):check_cancel(cancel)
            deadline=time.monotonic()+300;delay=.02
            while True:
                check_cancel(cancel)
                raw=cache.fetch(path,8192,cancel,deadline,missing_ok=True)
                if raw is not None:break
                if time.monotonic()>deadline:raise TimeoutError("Waiting for the HTTP package exceeded 5 minutes.")
                time.sleep(delay);delay=min(.2,delay*2)
            ready.put(download(json.loads(raw),index))
            index+=1;path=path.rsplit('/',1)[0]+'/block-%d.json'%index
    if ack>=0:session=call(dict(action='transfer',preview=token,ack=ack))
    last=time.monotonic()
    while True:
        check_cancel(cancel);check_server(session)
        if final is None and session.get('manifest'):
            final=validate_archive_document(fetch_reference(cache,session['manifest'],cancel),card,final=True)
            verify_transport(final,public)
            if any(final[k]!=document[k] for k in document if k!='total_size'):
                raise ValueError('Archive identity changed')
            total=final['total_size'];progress('download_size',-1,total,'')
            progress('files',state['completed_bytes'],total,'')
        if final is not None and ack+1==len(final['http']['chunks']):
            if (state['completed_bytes']!=final['total_size'] or state['file_count']!=final['file_count']
                    or state['archive_bytes']!=final['archive_size']
                    or final['http']['chunks']!=[m['chunk'] for m in messages]):
                raise ValueError("Received HTTP packages do not match the completed transfer.")
            return final
        verified=None;message=session.get('block')
        if pending is not None:
            poll=time.monotonic()+WORKER_POLL_SECONDS
            while True:
                check_cancel(cancel)
                try:message,verified=ready.get(timeout=.1);slots.release();break
                except queue.Empty:
                    if pending.done():pending.result()
                progress('prepare',state['completed_bytes'],total,"Waiting for HTTP package")
                if time.monotonic()>=poll:
                    session=call(dict(action='transfer',preview=token,ack=ack));check_server(session)
                    poll=time.monotonic()+WORKER_POLL_SECONDS
        if not message:
            if time.monotonic()-last>300:raise TimeoutError("The NAS did not prepare the package within 5 minutes.")
            time.sleep(.1);session=call(dict(action='transfer',preview=token,ack=ack));continue
        chunk=verify_stream_block(message,reference['sha256'],ack+1,public)
        part=safe_path(folder,chunk['sha256']+'.part')
        info=part.stat() if part.exists() else None
        if verified is None or info is None or (info.st_size,info.st_mtime_ns)!=verified:download(message,ack+1)
        if pending is None:
            path=session.get('next_block_path')
            expected=chunk['path'].rsplit('/chunks/',1)[0]+'/block-%d.json'%(ack+2)
            if path is not None:
                if path!=expected:raise ValueError('Invalid next archive path')
                asset_path(path);pending=pool.submit(ahead,path,ack+2)
        marker=dict(plan=reference['sha256'],index=ack+1,sha256=chunk['sha256'])
        replay=False
        if extracting.exists():
            if extracting.stat().st_size>8192 or json.loads(extracting.read_bytes())!=marker:
                raise ValueError('Another archive packet was being extracted')
            replay=True
        atomic_write(extracting,json.dumps(marker).encode())
        added,files=unpack_archive(part,chunk,payload,cancel,progress,state['completed_bytes'],total,replay)
        state.update(index=ack+1,completed_bytes=state['completed_bytes']+added,
            file_count=state['file_count']+files,archive_bytes=state['archive_bytes']+chunk['raw_size'])
        atomic_write(checkpoint,json.dumps(state).encode())
        extracting.unlink();part.unlink(missing_ok=True)
        ack+=1;last=time.monotonic()
        progress('files',state['completed_bytes'],total,label(ack))
        progress('copy',state['completed_bytes'],total,label(ack))
        session=call(dict(action='transfer',preview=token,ack=ack))
