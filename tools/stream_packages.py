"""An on-request producer cache owned exclusively by one NGS download."""


def stream_worker_alive(path):
    import fcntl
    if not path.is_file(): return False
    with path.open('rb') as lock:
        try: fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return True
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return False


def stream_worker(folder, card, launch, helpers, private, certificate, lock):
    import base64, hashlib, json, shutil, time
    from pathlib import Path
    ns = helpers
    info = dict(state="running", done=0, total=0, updated=time.time())
    def write(name, data): ns["atomic_write"](ns["safe_path"](folder, name), data)
    def state(**values):
        info.update(values, updated=time.time())
        write("stream.json", json.dumps(info).encode())
    def reference(name, data):
        return dict(path="/assets/ngs_launcher/package_jobs/" + folder.name + "/" + name,
                    size=len(data), sha256=hashlib.sha256(data).hexdigest())
    class Cancel:
        def is_set(self): return (folder / "cancel").exists()
    cancel = Cancel()
    plan_digest = ""
    def pipeline(event, value):
        nonlocal plan_digest
        if event == "plan":
            data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            if len(data) > 32 * 1024 * 1024: raise ValueError("Oversized transfer plan")
            if (folder / "plan.json").exists() and (folder / "plan.json").read_bytes() != data:
                raise ValueError("The source game has changed; create a new download job.")
            write("plan.json", data); plan_digest = hashlib.sha256(data).hexdigest()
            state(plan=reference("plan.json", data))
            return
        number = value["index"]
        if event == "capacity":
            # Pack the requested game continuously; fall back to two blocks near the disk reserve.
            if time.time() - (folder / 'ack').stat().st_mtime > 900:
                raise TimeoutError('TransferIdle')
            started = time.time()
            reserve = 2 * (ns['PACKAGE_SIZE'] + 1024 * 1024) + 512 * 1024 * 1024
            while number - int((folder / "ack").read_text()) > 2 and shutil.disk_usage(folder).free < reserve:
                ns["check_cancel"](cancel)
                if time.time() - (folder / "ack").stat().st_mtime > 900:
                    raise TimeoutError("TransferIdle")
                if time.time() - started > 5:
                    state(); started = time.time()
                time.sleep(.02)
            return number > int((folder / "ack").read_text())
        chunk = value["chunk"]
        message = dict(schema=1, plan_sha256=plan_digest, index=number, chunk=chunk, key=certificate)
        message["signature"] = base64.b64encode(ns["server_sign"](private, ns["transport_payload"](message))).decode()
        write("block-%d.json" % number, json.dumps(message).encode())
    last = 0
    def advanced(done, total):
        nonlocal last
        now = time.time()
        if now - last >= 1:
            state(done=done, total=total); last = now
    try:
        builder = ns['build_archives'] if launch.get('_stream_archive') else ns['build_packages']
        result = builder(Path("/mnt/ngs-installers" if card.get("kind") == "installer" else "/mnt/ngs-games"), folder, card, launch, ns, cancel=cancel,
            progress=lambda done, total: advanced(done, total), pipeline=pipeline)
        document, transport = result["document"], result["transport"]
        transport["key"] = certificate
        transport["signature"] = base64.b64encode(ns["server_sign"](private, ns["transport_payload"](transport))).decode()
        document["http"] = transport
        if document.get('format') == 'tar-zstd': ns['validate_archive_document'](document,card,final=True)
        else: ns["validate_manifest"](document, card)
        data = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        if len(data) > 32 * 1024 * 1024: raise ValueError("Oversized transfer manifest")
        write("manifest.json", data)
        state(state="ready", manifest=reference("manifest.json", data))
        while int((folder / "ack").read_text()) < len(transport["chunks"]) - 1:
            ns["check_cancel"](cancel)
            if time.time() - (folder / "ack").stat().st_mtime > 900:
                raise TimeoutError("TransferIdle")
            time.sleep(.2)
    except Exception as error:
        state(state="ready" if isinstance(error, TimeoutError) and info["state"] == "ready" else "error",
              error=str(error)[:300] if isinstance(error, ValueError) else type(error).__name__)
        for metadata in folder.glob("block-*.json"):
            if metadata.stat().st_size > 8192: continue
            chunk = json.loads(metadata.read_bytes())["chunk"]
            path = ns["safe_path"](folder, "chunks/" + chunk["sha256"] + ".bin")
            if chunk["path"] == "/assets/ngs_launcher/package_jobs/" + folder.name + "/chunks/" + path.name:
                path.unlink(missing_ok=True)
    finally:
        lock.close()


def release_stream(folder, helpers):
    import fcntl, re, shutil
    if (folder.parent.name != 'package_jobs' or not re.fullmatch('package-[0-9a-f]{32}', folder.name)
            or folder.resolve() != folder):
        raise ValueError('Foreign transfer cleanup folder')
    if not folder.exists(): return {'state': 'released'}
    if not shutil.rmtree.avoids_symlink_attacks:
        raise ValueError('Safe transfer cleanup requires file-descriptor traversal')
    helpers['safe_path'](folder, 'cancel').touch(exist_ok=True)
    with helpers['safe_path'](folder, 'worker.lock').open('ab') as lock:
        try: fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return {'state': 'releasing'}
        # Keep the producer excluded until its own folder and links have been removed.
        shutil.rmtree(folder)
    return {'state': 'released'}


def stream_action(job, payload, folder, helpers):
    import json, threading, time
    ns = helpers
    folder.mkdir(exist_ok=True)
    state_path = ns["safe_path"](folder, "stream.json")
    if payload["action"] == "stream_repair":
        ns["repair_stream_block"](folder, payload["index"], json.loads(job.get_value("published_data"))["card"], ns)
        return {"state": "repaired"}
    if payload["action"] == "stream_build":
        import fcntl
        lock = ns["safe_path"](folder, "worker.lock").open("ab")
        try: fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close(); return {"state": "running"}
        except BaseException:
            lock.close(); raise
        try:
            if state_path.exists() and json.loads(state_path.read_bytes())["state"] == "ready":
                lock.close(); return {"state": "ready"}
            chunks = ns["safe_path"](folder, "chunks")
            if chunks.is_dir():
                import re
                for temporary in chunks.glob('.package-*'):
                    if not re.fullmatch(r'\.package-[a-z0-9_]{8}', temporary.name):
                        raise ValueError('Foreign temporary package')
                    path = ns["safe_path"](chunks, temporary.name)
                    if not path.is_file(): raise ValueError('Foreign temporary package')
                    path.unlink()
            values = json.loads(job.get_value("published_data"))
            ack = ns["safe_path"](folder, "ack")
            if not ack.exists(): ns["atomic_write"](ack, b"-1")
            else: ack.touch()
            job.set_value("state", "running"); job.commit()
            ns["atomic_write"](state_path, json.dumps(dict(state="running", done=0, total=0, updated=time.time())).encode())
            threading.Thread(target=ns["stream_worker"], args=(folder, values["card"], values["launch"], ns,
                payload["private"], payload["certificate"], lock), name="ngs-stream-" + job.get_code(), daemon=True).start()
            return {"state": "running"}
        except BaseException:
            lock.close(); raise
    if not state_path.is_file() or state_path.stat().st_size > 8192:
        raise ValueError("Invalid stream state")
    state = json.loads(state_path.read_bytes())
    if state["state"] == "running" and not ns["stream_worker_alive"](ns["safe_path"](folder, "worker.lock")):
        state = json.loads(state_path.read_bytes())
        if state["state"] == "running": state = dict(state="error", error="WorkerStopped")
    if state["state"] in ("ready", "error") and job.get_value("state") != state["state"]:
        job.set_value("state", state["state"]); job.commit()
    ack = payload["ack"]
    ack_path = ns["safe_path"](folder, "ack")
    previous = int(ack_path.read_text())
    if ack > previous:
        if ack != previous + 1: raise ValueError("Acknowledgement skipped a block")
        metadata = ns["safe_path"](folder, "block-%d.json" % ack)
        if not metadata.is_file() or metadata.stat().st_size > 8192: raise ValueError("Unknown stream block")
        chunk = json.loads(metadata.read_bytes())["chunk"]
        path = ns["safe_path"](folder, "chunks/" + chunk["sha256"] + ".bin")
        if chunk["path"] != "/assets/ngs_launcher/package_jobs/" + folder.name + "/chunks/" + path.name:
            raise ValueError("Foreign cached block")
        path.unlink(missing_ok=True)
        ns["atomic_write"](ack_path, str(ack).encode())
    else:
        ack_path.touch()
    block = ns["safe_path"](folder, "block-%d.json" % (ack + 1))
    if block.is_file():
        if block.stat().st_size > 8192: raise ValueError("Oversized stream block")
        state["block"] = json.loads(block.read_bytes())
    state["next_block_path"] = "/assets/ngs_launcher/package_jobs/" + folder.name + "/block-%d.json" % (ack + 2)
    return state


def repair_stream_block(folder, number, card, ns):
    import hashlib, json, os, tempfile, threading
    from pathlib import Path
    plan = ns['safe_path'](folder,'plan.json')
    if plan.stat().st_size <= 8192 and json.loads(plan.read_bytes())['document'].get('format') == 'tar-zstd':
        return ns['repair_archive_block'](folder,number,card,ns)
    metadata = ns["safe_path"](folder, "block-%d.json" % number)
    if not metadata.is_file() or metadata.stat().st_size > 8192: raise ValueError("Unknown stream block")
    chunk = json.loads(metadata.read_bytes())["chunk"]
    target = ns["safe_path"](folder, "chunks/" + chunk["sha256"] + ".bin")
    if chunk["path"] != "/assets/ngs_launcher/package_jobs/" + folder.name + "/chunks/" + target.name:
        raise ValueError("Foreign stream block")
    if target.is_file(): return
    plan_path = ns["safe_path"](folder, "plan.json")
    if plan_path.stat().st_size > 32 * 1024 * 1024: raise ValueError("Oversized stream plan")
    plan = json.loads(plan_path.read_bytes())
    source = ns["safe_path"](Path("/mnt/ngs-installers" if card.get("kind") == "installer" else "/mnt/ngs-games"), card["source_folder"])
    before, directories = ns["inventory"](source, ignore={"@eaDir", "#recycle"})
    if hashlib.sha256(json.dumps([before, directories], sort_keys=True).encode()).hexdigest() != plan["source_fingerprint"]:
        raise ValueError("The source game has changed. Partial files were preserved.")
    temporary, encoder = None, None
    encoded, raw_digest = hashlib.sha256(), hashlib.sha256()
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".repair-", delete=False) as stream:
            temporary = Path(stream.name)
            class Output:
                def write(self, data): stream.write(data); encoded.update(data)
            encoder = ns["zstd_writer"](Output())
            skip, remaining = number * ns["PACKAGE_SIZE"], chunk["raw_size"]
            for item in plan["document"]["files"]:
                if skip >= item["size"]:
                    skip -= item["size"]; continue
                if not remaining: break
                path = ns["safe_path"](source, item["path"])
                with path.open("rb") as incoming:
                    info = os.fstat(incoming.fileno())
                    if (info.st_size, info.st_mtime_ns) != tuple(before[item["path"]]): raise ValueError("Source changed")
                    incoming.seek(skip); count = min(item["size"] - skip, remaining)
                    while count:
                        data = incoming.read(min(count, 4 * 1024 * 1024))
                        if not data: raise ValueError("Source truncated")
                        encoder.write(data); raw_digest.update(data); count -= len(data); remaining -= len(data)
                    after = os.fstat(incoming.fileno())
                    if (after.st_size, after.st_mtime_ns) != (info.st_size, info.st_mtime_ns): raise ValueError("Source changed")
                skip = 0
            encoder.finish(); encoder = None
            stream.flush(); os.fsync(stream.fileno())
        if remaining or raw_digest.hexdigest() != chunk["raw_sha256"] or encoded.hexdigest() != chunk["sha256"] or temporary.stat().st_size != chunk["size"]:
            raise ValueError("The rebuilt package differs from the pinned package.")
        try: os.link(temporary, target)
        except FileExistsError:
            if ns["hash_file"](target, threading.Event()) != chunk["sha256"]: raise ValueError("Cached block changed")
    finally:
        if encoder: encoder.close()
        if temporary: temporary.unlink(missing_ok=True)
