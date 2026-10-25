"""Prepare games on the NAS; only NGS assets and NGS project records are writable."""

import argparse
import base64
import hashlib
import inspect
import json
import threading
from pathlib import Path

from ngs import install
from ngs.catalog import atomic_write
from ngs.packages import PACKAGE_SIZE, transport_payload
from ngs.portable import DEFAULT_TACTIC_URL, PortableStorage, path_key
from ngs.scanner import check_pe
from ngs.tactic import TacticClient
from ngs.update_crypto import RsaKey
from tools.publish_update import signing_key
from tools.tactic_catalog import contract_source, invoke
from tools.tactic_project import DEFAULT_CREDENTIALS, admin_client, admin_password


def server_source(source):
    return path_key(source) in {path_key(r"\\NAS\Files\games"), path_key(r"\\NAS\Files\games")}


def build_packages(source_root, assets, card, launch, helpers, chunk_size=PACKAGE_SIZE, cancel=None, progress=lambda *args: None, pipeline=None):
    import hashlib, json, os, shutil, tempfile, threading
    from pathlib import Path
    safe_path, inventory = helpers["safe_path"], helpers["inventory"]
    cancel = cancel or threading.Event()
    source = safe_path(Path(source_root), card["source_folder"])
    helpers["check_pe"](safe_path(source, card["executable"]))
    ignored = {"@eaDir", "#recycle"}
    before, directories = inventory(source, cancel, ignore=ignored)
    document = dict(schema=1, game_id=card["id"], source_folder=card["source_folder"], executable=card["executable"],
                    working_directory=launch.get("working_directory", ""), arguments=launch.get("arguments", []),
                    directories=directories, files=[dict(path=p, size=s[0], sha256="0" * 64) for p, s in sorted(before.items())],
                    total_size=sum(s[0] for s in before.values()))
    helpers["validate_manifest"](document)
    if pipeline:
        pipeline("plan", dict(document=document, source_fingerprint=hashlib.sha256(
            json.dumps([before, directories], sort_keys=True).encode()).hexdigest()))
    folder = safe_path(Path(assets), "chunks" if pipeline else "packages")
    folder.mkdir(exist_ok=True)
    if shutil.disk_usage(folder).free < (3 * (chunk_size + 1024 * 1024) if pipeline else document["total_size"]) + 512 * 1024 * 1024:
        raise OSError("The NAS does not have enough space for the game packages and a 512 MiB reserve.")
    chunks, stream, temporary, size, digest, done = [], None, None, 0, hashlib.sha256(), 0
    encoder, encoded_digest = None, hashlib.sha256()
    writeback = None
    if pipeline and os.name == "posix":
        import ctypes
        writeback = getattr(ctypes.CDLL(None), "sync_file_range", None)
        if writeback is not None:
            writeback.argtypes = [ctypes.c_int, ctypes.c_longlong, ctypes.c_longlong, ctypes.c_uint]
            writeback.restype = ctypes.c_int
    class Output:
        position, flushed = 0, 0
        def write(self, data):
            nonlocal writeback
            stream.write(data); encoded_digest.update(data)
            self.position += len(data)
            if writeback is not None and self.position - self.flushed >= 8 * 1024 * 1024:
                stream.flush()
                # Start data writeback while compressing; final fsync still precedes publication.
                if writeback(stream.fileno(), self.flushed, self.position - self.flushed, 2): writeback = None
                self.flushed = self.position

    def finish():
        nonlocal stream, temporary, size, digest, encoder, encoded_digest
        if encoder:
            encoder.finish(); encoder = None
        stream.flush()
        os.fsync(stream.fileno())
        stream.close()
        stream = None
        identity = encoded_digest.hexdigest() if pipeline else digest.hexdigest()
        encoded_size = temporary.stat().st_size
        target = safe_path(folder, identity + ".bin")
        publish = pipeline("capacity", dict(index=len(chunks))) if pipeline else True
        if publish is not False:
            try:
                # Windows rename refuses an existing destination; POSIX needs a hard link for the same guarantee.
                if os.name == "nt":
                    temporary.rename(target)
                    temporary = None
                else:
                    os.link(temporary, target)
            except FileExistsError:
                if target.stat().st_size != encoded_size or helpers["hash_file"](target, threading.Event()) != identity:
                    raise ValueError("The existing package is damaged; overwrite rejected.")
        if temporary is not None:
            temporary.unlink()
            temporary = None
        path = ("/assets/ngs_launcher/package_jobs/" + Path(assets).name + "/chunks/" if pipeline else "/assets/ngs_launcher/packages/")
        chunk = dict(path=path + identity + ".bin", size=encoded_size, sha256=identity)
        if pipeline:
            chunk.update(codec="zstd", raw_size=size, raw_sha256=digest.hexdigest())
        chunks.append(chunk)
        if pipeline:
            pipeline("chunk", dict(index=len(chunks) - 1, chunk=chunk))
        size, digest, encoded_digest = 0, hashlib.sha256(), hashlib.sha256()

    try:
        for item in document["files"]:
            helpers["check_cancel"](cancel)
            file_digest, count = hashlib.sha256(), 0
            with safe_path(source, item["path"]).open("rb") as incoming:
                while True:
                    helpers["check_cancel"](cancel)
                    data = incoming.read(min(4 * 1024 * 1024, chunk_size - size))
                    if not data:
                        break
                    count += len(data)
                    if count > item["size"]:
                        raise ValueError("A source file grew during packaging.")
                    if stream is None:
                        stream = tempfile.NamedTemporaryFile(dir=folder, prefix=".package-", delete=False)
                        temporary = Path(stream.name)
                        if pipeline:
                            encoder = helpers["zstd_writer"](Output())
                    if encoder:
                        encoder.write(data)
                    else:
                        stream.write(data)
                    file_digest.update(data)
                    digest.update(data)
                    size += len(data)
                    done += len(data)
                    progress(done, document["total_size"])
                    if size == chunk_size:
                        finish()
            if count != item["size"]:
                raise ValueError("A source file changed during packaging.")
            item["sha256"] = file_digest.hexdigest()
        if size:
            finish()
        if inventory(source, cancel, ignore=ignored) != (before, directories):
            raise ValueError("The source game changed during packaging; publication stopped.")
        helpers["validate_manifest"](document)
        data = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf8")
        return dict(document=document, transport=dict(schema=2 if pipeline else 1, manifest_sha256=hashlib.sha256(data).hexdigest(), chunks=chunks))
    finally:
        if encoder:
            encoder.close()
        if stream is not None:
            stream.close()
        if temporary is not None:
            temporary.unlink()


def package_action(action, payload, helpers, service=False):
    import json, os, uuid, threading, time
    from pathlib import Path
    from pyasm.biz import Project, Schema
    from pyasm.common import Environment, Container
    from pyasm.search import Search, SearchType, DbContainer
    project = Project.get()
    security = Environment.get_security()
    service_allowed = (service and security.is_logged_in() and not security.is_admin()
                       and security.get_user_name() == "ngs_catalog_worker")
    if ((not security.is_admin() and not service_allowed) or project.get_code() != "ngs_launcher"
            or project.get_database_name() != "ngs_launcher"
            or project.get_value("description") != "NGS handheld game catalog; managed from D:/code/NGS"):
        raise PermissionError("Only NGS administrative preparation can build packages")
    if action not in ("queue", "build", "status", "cancel", "stream_build", "stream_status", "stream_repair", "stream_release"):
        raise ValueError("Unsupported package action")
    sql = DbContainer.get(project.get_project_db_resource())
    sql.do_query("SELECT pg_advisory_xact_lock(1313297221)")
    kind, owner = "ngs_launcher/package_job", "NGS HTTP packages v1"
    query = Search("sthpw/search_object"); query.add_filter("search_type", kind)
    registry = query.get_sobject()
    columns = {row[0] for row in sql.do_query("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name='package_job'")}
    expected = {"id", "code", "game_id", "state", "description", "timestamp", "s_status", "published_data"}
    if bool(registry) != bool(columns) or (registry and (columns != expected or registry.get_value("description") != owner
            or registry.get_value("namespace") != "ngs_launcher" or registry.get_value("table_name") != "package_job")):
        raise ValueError("Conflicting NGS package schema")
    if not registry:
        if service:
            raise ValueError("NGS package schema must be prepared by the administrator")
        registry = SearchType(SearchType.SEARCH_TYPE)
        for key, value in dict(search_type=kind, namespace="ngs_launcher", database="{project}", table_name="package_job",
                               schema="public", class_name="pyasm.search.SObject", title="NGS HTTP packages", description=owner).items():
            registry.set_value(key, value)
        registry.commit()
        sql.do_update("CREATE TABLE package_job (id serial PRIMARY KEY, code varchar(80) UNIQUE NOT NULL, game_id varchar(80), state varchar(16), description text, timestamp timestamp DEFAULT now(), s_status varchar(32), published_data text)")
        cached = Container.get("DatabaseImpl:table_info") or {}
        cached.pop(str(project.get_project_db_resource()), None)
        schema = Schema.get_by_code("ngs_launcher")
        schema.add_search_type(kind); schema.commit()
    namespace = {}
    exec(helpers, namespace)
    if action == "queue":
        card, launch = payload["card"], payload.get("launch", {})
        namespace["game_id"](card["id"])
        namespace["relative_game_path"](card["source_folder"])
        namespace["relative_game_path"](card["executable"])
        root = Path("/mnt/ngs-installers" if card.get("kind") == "installer" else "/mnt/ngs-games")
        if not root.is_dir():
            raise ValueError("The read-only games folder is not mounted")
        with open("/proc/self/mountinfo") as mounts:
            if not any(len(line.split()) > 5 and line.split()[4] == str(root) and 'ro' in line.split()[5].split(',') for line in mounts):
                raise ValueError("The games source must be mounted read-only")
        namespace["check_pe"](namespace["safe_path"](namespace["safe_path"](root, card["source_folder"]), card["executable"]))
        job = SearchType.create(kind)
        for key, value in dict(code="package-" + uuid.uuid4().hex, game_id=card["id"], state="queued",
                               description=owner, published_data=json.dumps(dict(card=card, launch=launch))).items():
            job.set_value(key, value)
        job.commit()
        return {"job": job.get_code(), "state": "queued"}
    query = Search(kind); query.add_filter("code", payload["job"])
    job = query.get_sobject()
    if not job and action == 'stream_release': return {'state': 'released'}
    if not job or job.get_value("description") != owner:
        raise ValueError("Unknown NGS package job")
    if job.get_value("state") == "ready" and not action.startswith("stream_"):
        return {"state": "ready", "result": json.loads(job.get_value("published_data"))}
    assets = namespace["safe_path"](Path(Environment.get_asset_dir()), "ngs_launcher")
    work = namespace["safe_path"](assets, "package_jobs")
    work.mkdir(exist_ok=True)
    folder = namespace["safe_path"](work, job.get_code())
    if action == 'stream_release':
        result = namespace['release_stream'](folder, namespace)
        if result['state'] == 'released':
            if not namespace['re'].fullmatch('package-[0-9a-f]{32}', job.get_code()): raise ValueError('Foreign package code')
            sql.do_update("DELETE FROM package_job WHERE code='%s' AND description='NGS HTTP packages v1'" % job.get_code())
        return result
    if action.startswith("stream_"):
        return namespace["stream_action"](job, dict(payload, action=action), folder, namespace)
    if action == "build":
        values = json.loads(job.get_value("published_data"))
        # A unique directory claims this job across all TACTIC workers, without shared server globals.
        folder.mkdir(exist_ok=False)
        state_path = folder / "state.json"
        namespace["atomic_write"](state_path, json.dumps(dict(state="running", done=0, total=0, updated=time.time())).encode())
        def worker():
            class Cancel:
                def is_set(self):
                    return (folder / "cancel").exists()
            last = 0
            def advanced(done, total):
                nonlocal last
                now = time.time()
                if now - last >= 1:
                    namespace["atomic_write"](state_path, json.dumps(dict(state="running", done=done, total=total, updated=now)).encode())
                    last = now
            try:
                # This worker uses only validated filesystem paths; it never authenticates or accesses TACTIC context.
                result = namespace["build_packages"](Path("/mnt/ngs-games"), assets, values["card"], values["launch"], namespace,
                                                     cancel=Cancel(), progress=advanced)
                raw = json.dumps(result, ensure_ascii=False).encode("utf8")
                if len(raw) > 32 * 1024 * 1024:
                    raise ValueError("Game package description exceeds the client limit")
                namespace["atomic_write"](folder / "result.json", raw)
                state = dict(state="ready", updated=time.time())
            except Exception as error:
                state = dict(state="error", error=type(error).__name__, updated=time.time())
            namespace["atomic_write"](state_path, json.dumps(state).encode())
        job.set_value("state", "running"); job.commit()
        threading.Thread(target=worker, name="ngs-package-" + job.get_code(), daemon=True).start()
        return {"state": "running"}
    if not folder.is_dir():
        raise ValueError("NGS package preparation has not started")
    if action == "cancel":
        namespace["safe_path"](folder, "cancel").touch(exist_ok=True)
    state_path = namespace["safe_path"](folder, "state.json")
    if state_path.stat().st_size > 4096:
        raise ValueError("Invalid package state")
    state = json.loads(state_path.read_bytes())
    if state["state"] == "running" and time.time() - state["updated"] > 180:
        state = {"state": "error", "error": "WorkerStopped"}
    if state["state"] == "ready":
        result_path = namespace["safe_path"](folder, "result.json")
        if result_path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("Oversized package description")
        result = json.loads(result_path.read_bytes())
        job.set_value("state", "ready")
        job.set_value("published_data", json.dumps(result, ensure_ascii=False))
        job.commit()
        return {"state": "ready", "result": result}
    if state["state"] == "error":
        job.set_value("state", "error"); job.commit()
    return state


def helper_source():
    from ngs.compression import zstd_writer
    from ngs.archive import validate_archive_document
    from tools.archive_packages import build_archives, repair_archive_block
    return (contract_source() + "\nimport os, stat, threading, struct, tempfile\nfrom pathlib import Path\nBLOCK = 4 * 1024 * 1024\n"
            + "\n".join(inspect.getsource(fn) for fn in (install.Cancelled, install.is_link, install.safe_path,
                install.check_cancel, install.inventory, install.hash_file, atomic_write, check_pe, zstd_writer,
                validate_archive_document, build_packages, build_archives, repair_archive_block)))


def prepare_remote(client, storage, card, launch=None, cancel=None, progress=lambda *args: None):
    cancel = cancel or threading.Event()
    # Validate the private/public pair before spending time and disk on the NAS.
    private = signing_key(storage)
    install.check_cancel(cancel)
    source = inspect.getsource(package_action) + "\nreturn package_action(action, payload, helpers)"
    helpers = helper_source()
    queued = client.script(source, action="queue", payload=dict(card=card, launch=launch or {}), helpers=helpers)
    progress("manifest", 0, 0, "NAS готовит HTTP-пакеты и проверяет файлы…")
    try:
        client.script(source, action="build", payload=queued, helpers=helpers)
        while True:
            install.check_cancel(cancel)
            state = client.script(source, action="status", payload=queued, helpers=helpers)
            if state["state"] == "ready":
                result = state["result"]
                break
            if state["state"] == "error":
                raise RuntimeError("NAS не подготовил пакеты: " + state.get("error", "UnknownError"))
            progress("manifest", state.get("done", 0), state.get("total", 0), "NAS готовит HTTP-пакеты…")
            cancel.wait(1)
        install.check_cancel(cancel)
        document, transport = result["document"], result["transport"]
        with RsaKey(private.read_bytes(), private=True) as key:
            transport["signature"] = base64.b64encode(key.sign(transport_payload(transport))).decode("ascii")
        document["http"] = transport
        return install.validate_manifest(document)
    except Exception:
        try:
            client.script(source, action="cancel", payload=queued, helpers=helpers)
        except Exception:
            pass  # The persisted server job remains visible; an unavailable server must not mask the original error.
        raise


def publish_existing(client, storage, record, cancel=None, progress=lambda *args: None):
    from ngs.catalog import CatalogCache
    cancel = cancel or threading.Event()
    card = record["published"]
    if (not card.get("available_on_nas", True) or not card.get("executable")
            or (card.get("source_root") and not server_source(card["source_root"]))):
        raise ValueError("Нужна опубликованная игра из подключённой к серверу папки NAS.")
    launch = {}
    if card.get("manifest"):
        previous = install.fetch_manifest(CatalogCache(storage, client.endpoint.split("/ngs_launcher/REST/")[0]), card, cancel)
        launch = {key: previous[key] for key in ("working_directory", "arguments")}
    document = prepare_remote(client, storage, card, launch, cancel, progress)
    data = install.manifest_bytes(document)
    install.check_cancel(cancel)
    result = invoke(client, "manifest", dict(game=card["id"], expected=record["token"],
                    manifest=dict(base64=base64.b64encode(data).decode("ascii"), sha256=hashlib.sha256(data).hexdigest())))
    invoke(client, "activate", invoke(client, "export"))
    return dict(result, transport="http", chunks=len(document["http"]["chunks"]), total_size=document["total_size"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_TACTIC_URL)
    parser.add_argument("--credentials-env", type=Path, default=DEFAULT_CREDENTIALS)
    parser.add_argument("--game", required=True)
    args = parser.parse_args()
    storage = PortableStorage(Path(__file__).resolve().parents[1])
    storage.configure_process()
    client = admin_client(args.base_url, args.credentials_env)
    record = next((row for row in invoke(client, "inspect")["records"] if row["card"]["id"] == args.game), None)
    if not record or not record["published"].get("available_on_nas", True) or not record["published"].get("executable"):
        raise ValueError("Нужна опубликованная игра с подтверждённым EXE.")
    result = publish_existing(client, storage, record)
    print(json.dumps({key: value for key, value in result.items() if key != "card"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
