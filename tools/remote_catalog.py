"""Install only NGS's restricted remote catalogizer; never change shared TACTIC routes."""
import argparse
import base64
import inspect
import json
import struct
from pathlib import Path

from ngs.catalog import atomic_write
from ngs.packages import package_key_payload, transport_payload
from ngs.portable import PortableStorage
from ngs.remote_catalog import validate_catalog_request
from ngs.scanner import local_folder, scan_executables, UTILITY, MAX_DEPTH, MAX_ENTRIES, MAX_EXES
from ngs.tactic import TacticClient
from ngs.update_crypto import RsaKey
from tools.catalogizer import prepare_import, scan_folders
from tools.http_packages import helper_source, package_action
from tools.metadata_api import update_scripts
from tools.tactic_catalog import catalog_action
from tools.tactic_project import DEFAULT_CREDENTIALS, admin_client, admin_password, project_snapshot
from tools.stream_packages import stream_action, stream_worker, stream_worker_alive, repair_stream_block, release_stream
from tools.server_code import packed_script
from ngs.bulk_scan import title_key


OWNER = "NGS remote catalogizer v1"


def private_pem(blob):
    magic, bits, ne, nn, np, nq = struct.unpack_from("<6I", blob)
    if magic != 0x32415352 or bits != 2048:
        raise ValueError("Expected a private RSA2048 blob")
    offset, values = 24, []
    for size in (ne, nn, np, nq):
        values.append(int.from_bytes(blob[offset:offset + size], "big")); offset += size
    e, n, p, q = values
    d = pow(e, -1, (p - 1) * (q - 1))
    def der(tag, data):
        size = len(data)
        length = bytes([size]) if size < 128 else bytes([0x80 + (size.bit_length()+7)//8]) + size.to_bytes((size.bit_length()+7)//8, "big")
        return bytes([tag]) + length + data
    def integer(value):
        data = value.to_bytes(max(1, (value.bit_length()+7)//8), "big")
        return der(2, (b"\0" if data[0] & 128 else b"") + data)
    encoded = base64.b64encode(der(48, b"".join(integer(v) for v in (0, n, e, d, p, q, d % (p-1), d % (q-1), pow(q, -1, p))))).decode()
    return "-----BEGIN RSA PRIVATE KEY-----\n" + "\n".join(encoded[i:i+64] for i in range(0, len(encoded), 64)) + "\n-----END RSA PRIVATE KEY-----\n"


def server_sign(private, payload):
    import os, subprocess
    # Feed the package-only key through a pipe; never put a private key in public assets or argv.
    read, write = os.pipe()
    try:
        os.write(write, private.encode("ascii")); os.close(write); write = None
        result = subprocess.run(["openssl", "dgst", "-sha256", "-sign", "/proc/self/fd/" + str(read)],
            input=payload, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, pass_fds=(read,), timeout=15, check=True)
        if len(result.stdout) != 256: raise ValueError("Invalid server package signature")
        return result.stdout
    finally:
        os.close(read)
        if write is not None: os.close(write)


def review_artwork(prepared):
    import base64, hashlib, io
    from pathlib import Path
    from PIL import Image
    result = {}
    for context, bounds in (("cover", (240, 360)), ("background", (640, 360))):
        current = prepared.get("current")
        if (current and current.get("artwork", {}).get(context)
                and current.get("import_state", {}).get("artwork", {}).get(context) != current["artwork"][context]):
            from pyasm.biz import Snapshot
            from pyasm.common import Environment
            snapshot = Snapshot.get_by_code(current["artwork"][context])
            if (not snapshot or snapshot.get_value("project_code") != "ngs_launcher"
                    or snapshot.get_value("search_type").split("?")[0] != "ngs_launcher/game"
                    or snapshot.get_value("search_code") != prepared["proposed"]["id"]
                    or snapshot.get_value("context") != context):
                raise ValueError("Invalid preserved NGS artwork")
            assets = Path(Environment.get_asset_dir()).resolve() / "ngs_launcher"
            path = Path(snapshot.get_lib_path_by_type("main")).resolve()
            if not path.is_relative_to(assets) or not 0 < path.stat().st_size <= 8 * 1024 * 1024:
                raise ValueError("Invalid preserved NGS artwork path")
            raw = path.read_bytes()
        else:
            image = prepared["details"][context]
            if not isinstance(image.get("base64"), str) or len(image["base64"]) > 12 * 1024 * 1024:
                raise ValueError("Oversized review artwork")
            raw = base64.b64decode(image["base64"], validate=True)
            if hashlib.sha256(raw).hexdigest() != image.get("sha256"):
                raise ValueError("Invalid review artwork digest")
        if not 0 < len(raw) <= 8 * 1024 * 1024:
            raise ValueError("Invalid review artwork size")
        with Image.open(io.BytesIO(raw)) as picture:
            if picture.format not in ("PNG", "JPEG", "WEBP") or max(picture.size) > 8192 or picture.width * picture.height > 16_000_000:
                raise ValueError("Invalid review artwork dimensions")
            picture.thumbnail(bounds)
            thumb = picture.convert("RGB")
        while True:
            stream = io.BytesIO()
            thumb.save(stream, format="JPEG", quality=65, optimize=True)
            if len(stream.getvalue()) <= 12 * 1024:
                break
            thumb.thumbnail((max(1, thumb.width * 3 // 4), max(1, thumb.height * 3 // 4)))
        result[context] = "data:image/jpeg;base64," + base64.b64encode(stream.getvalue()).decode("ascii")
    return result


def custom_card_chunk(values, payload):
    import base64, hashlib, io, json
    if values.get('state') != 'review' or values['prepared']['details']['provider_id'] != 'manual':
        raise ValueError("Manual artwork can only be added before publishing an entry without artwork.")
    kind, digest = payload['kind'], payload['sha256']
    if values.get('custom_done', {}).get(kind) == digest: return True
    uploads = values.setdefault('custom_uploads', {})
    upload = uploads.get(kind, {})
    if payload['offset'] == 0:
        upload = dict(sha256=digest, total=payload['total'], data='')
    raw = base64.b64decode(upload.get('data', ''), validate=True)
    chunk = base64.b64decode(payload['data'], validate=True)
    if upload.get('sha256') != digest or upload.get('total') != payload['total'] or len(raw) != payload['offset']:
        raise ValueError("Invalid artwork upload order. Save the entry again.")
    raw += chunk
    if len(raw) < payload['total']:
        upload['data'] = base64.b64encode(raw).decode('ascii'); uploads[kind] = upload
        return False
    if len(raw) != payload['total'] or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("The entry artwork is damaged.")
    prepared = values['prepared']
    if kind == 'metadata':
        edits = json.loads(raw)
        if not isinstance(edits, dict) or set(edits) != {'name', 'description'}:
            raise ValueError("Invalid manual entry fields.")
        for field, limit in (('name', 160), ('description', 3000)):
            if (not isinstance(edits[field], str) or len(edits[field]) > limit
                    or any(ord(c) < 32 and c != '\n' for c in edits[field])):
                raise ValueError("Invalid field: " + field)
        if not edits['name'].strip(): raise ValueError("Enter a game title.")
        prepared['edits'].update(edits); prepared['merged'].update(edits)
    else:
        from PIL import Image
        with Image.open(io.BytesIO(raw)) as picture:
            if picture.format != 'JPEG' or max(picture.size) > 1280:
                raise ValueError("Invalid entry image.")
            picture.verify()
        current = prepared.get('current')
        if (current and current.get('artwork', {}).get(kind)
                and current.get('import_state', {}).get('artwork', {}).get(kind) != current['artwork'][kind]):
            raise ValueError("This image was manually edited in TACTIC. Update it there first.")
        prepared['details'][kind] = dict(base64=base64.b64encode(raw).decode('ascii'), sha256=digest)
    values.setdefault('custom_done', {})[kind] = digest
    uploads.pop(kind, None)
    return True


def remote_action(action, payload, device, helpers):
    import base64, copy, hashlib, io, json, time, uuid
    from pathlib import Path
    from pyasm.biz import Project
    from pyasm.common import Environment
    from pyasm.search import Search, SearchType, DbContainer
    project, security = Project.get(), Environment.get_security()
    if (project.get_code() != "ngs_launcher" or project.get_database_name() != "ngs_launcher"
            or project.get_value("description") != "NGS handheld game catalog; managed from D:/code/NGS"
            or not security.is_logged_in() or security.is_admin() or security.get_user_name() != "ngs_catalog_worker"):
        raise PermissionError("Only the authenticated NGS catalog worker is allowed")
    ns = {}; exec(helpers, ns)
    ns["validate_catalog_request"](payload)
    if action != payload["action"] or not isinstance(device, str) or not ns["re"].fullmatch("[0-9a-f]{32}", device):
        raise ValueError("Invalid catalogizer identity")
    installers = action.startswith("installer_")
    if installers:
        action = action.removeprefix("installer_")
        payload = dict(payload, action=action)
    sql = DbContainer.get(project.get_project_db_resource())
    # Network reads and new UUID previews must not hold up other devices' catalog work.
    if action not in ("list", "folders", "exes", "search", "preview", "estimate"):
        sql.do_query("SELECT pg_advisory_xact_lock(1313297223)")
    def find(code):
        query = Search("ngs_launcher/catalog_config"); query.add_filter("code", code)
        obj = query.get_sobject()
        if obj and obj.get_value("description") != "NGS remote catalogizer v1":
            raise ValueError("Foreign catalogizer record")
        return obj
    config = find("service")
    settings = json.loads(config.get_value("data")) if config else {}
    access = Search('ngs_launcher/api_device'); access.add_filter('code', device); access = access.get_sobject()
    if (not access or access.get_value('enabled') is not True
            or not json.loads(access.get_value('state') or '{}').get('account')):
        raise PermissionError('NGS account access required')
    def catalog(op, values=None):
        values = copy.deepcopy(values or {})
        if installers and op in ("export", "activate"):
            values["kind"] = "installer"
        result = ns["catalog_action"](op, values, helpers, service=True)
        if op == "inspect":
            result["records"] = [r for r in result["records"] if (r["card"].get("kind") == "installer") == installers]
        return result
    def packages(op, values): return ns["package_action"](op, values, helpers, service=True)
    def save(obj, values):
        values["installers"] = installers
        # Session checkpoints stay durable, without undo history of megabytes of preview images.
        obj.set_value("data", json.dumps(values, ensure_ascii=False)); obj.commit(triggers="none", log_transaction=False)
    if action == 'release':
        obj = find(payload['preview'])
        if not obj: return {'state': 'released'}
        values = json.loads(obj.get_value('data'))
        if (values.get('device') != device or values.get('installers', False) != installers
                or values.get('state') != 'transfer'):
            raise PermissionError('Foreign transfer cleanup')
        result = packages('stream_release', dict(job=values['job']))
        if result['state'] == 'released':
            sql.do_update("DELETE FROM catalog_config WHERE code='%s' AND description='NGS remote catalogizer v1'" % payload['preview'])
        return result
    root = Path(ns.get("INSTALLERS_ROOT", "/mnt/ngs-installers") if installers else ns["GAMES_ROOT"])
    source = r"\\NAS\Files\games_installs" if installers else r"\\NAS\Files\games"
    source = settings.get('source_roots', {}).get('installers' if installers else 'games', source)
    if not root.is_dir():
        if installers and action in ("list", "folders"):
            return dict(stage="folders", total=0, offset=0, rows=[], connected=False,
                        message="The installers folder is not mounted on the server yet.")
        raise ValueError("The installers folder is not mounted on the NGS server" if installers else "The games folder is not mounted on the NGS server")
    with open("/proc/self/mountinfo") as stream:
        if not any(len(parts := line.split()) > 5 and parts[4] == str(root) and "ro" in parts[5].split(",") for line in stream):
            raise ValueError("The games folder must be mounted read-only")
    def sized(card):
        if "total_size" not in card:
            files, unused = ns["inventory"](ns["safe_path"](root, card["source_folder"]), ignore={"@eaDir", "#recycle"})
            card["total_size"] = sum(item[0] for item in files.values())
        return card
    def prepare(rows, folder, details, exe):
        original_id = details["card"]["id"]
        if installers:
            details = copy.deepcopy(details)
            identity = "\0".join((original_id, source.casefold(), folder.casefold(), exe.casefold()))
            details["card"]["id"] = "setup-" + hashlib.sha256(identity.encode()).hexdigest()[:32]
            details["card"].update(kind="installer", game_id=original_id)
        prepared = ns["prepare_import"](rows, folder, details, exe, source=source, legacy_source=source)
        if installers:
            if details["provider_id"] == "manual" and prepared["current"]:
                original_id = prepared["current"]["card"].get("game_id", original_id)
            prepared["proposed"].update(kind="installer", game_id=original_id)
            prepared["merged"].update(kind="installer", game_id=original_id)
        return prepared
    def fully_published(folder, rows):
        bound = [r for r in rows if r["card"]["source_folder"].casefold() == folder.casefold() and r["published"]]
        if not bound or not installers:
            return bool(bound)
        paths = {r["card"]["executable"].casefold() for r in bound}
        try:
            found = ns["scan_executables"](ns["safe_path"](root, folder))
        except OSError:
            return False
        candidates = {entry["path"].casefold() for entry in found["candidates"] if
                      Path(entry["path"]).name.casefold().startswith(("setup", "install")) or entry["path"].casefold() in paths}
        return bool(candidates and not found["limited"] and not found["skipped"] and candidates <= paths)
    if action in ("list", "folders"):
        folders = [f for f in ns["scan_folders"](root) if f not in ("@eaDir", "#recycle")]
        rows = catalog("inspect")["records"]
        bound = {folder.casefold() for folder in folders if fully_published(folder, rows)}
        if payload.get("hide_published"):
            folders = [name for name in folders if name.casefold() not in bound]
        offset = payload["offset"]
        return dict(stage="folders", total=len(folders), offset=offset,
            rows=[dict(name=name, folder=name, published=name.casefold() in bound) for name in folders[offset:offset+20]])
    if action == "batch":
        folders = [f for f in ns["scan_folders"](root) if f not in ("@eaDir", "#recycle")]
        custom_known_url = settings.get("known_games_url", "").strip()
        if not installers and custom_known_url and "fetch_remote_known_games" in ns:
            known_obj = find("known_games")
            known_doc = json.loads(known_obj.get_value("data") or "{}") if known_obj else {}
            if time.time() - known_doc.get("synced_at", 0) > 3600:
                remote_entries = ns["fetch_remote_known_games"](custom_known_url)
                if remote_entries:
                    existing_list = known_doc.get("games", [])
                    by_n = {(g.get("n") or "").casefold(): g for g in existing_list if isinstance(g, dict)}
                    for item in remote_entries:
                        by_n[item["n"].casefold()] = item
                    if not known_obj:
                        known_obj = SearchType.create("ngs_launcher/catalog_config")
                        known_obj.set_value("code", "known_games"); known_obj.set_value("description", "NGS remote catalogizer v1")
                    save(known_obj, dict(schema=1, synced_at=time.time(), games=list(by_n.values())[:5000]))
        code = uuid.uuid4().hex
        obj = SearchType.create("ngs_launcher/catalog_config")
        obj.set_value("code", code); obj.set_value("description", "NGS remote catalogizer v1")
        save(obj, dict(device=device, created=time.time(), state="batch", folders=folders, rows=[], done=0))
        return dict(stage="batch", state="batch", preview=code, rows=[], done=0, total=len(folders))
    if action == "exes":
        result = ns["scan_executables"](ns["safe_path"](root, payload["folder"]))
        if installers:
            rows = catalog("inspect")["records"]
            for entry in result["candidates"]:
                names = [r["card"]["name"] for r in rows if r["published"] and
                         r["card"]["source_folder"].casefold() == payload["folder"].casefold() and
                         r["card"]["executable"].casefold() == entry["path"].casefold()]
                if names:
                    entry.update(published=True, status="Already added: " + ", ".join(names))
        elif "match_known_game" in ns:
            known_obj = find("known_games")
            extra = json.loads(known_obj.get_value("data") or "{}").get("games", []) if known_obj else []
            matched = ns["match_known_game"](payload["folder"], result["candidates"], extra_entries=extra, source=root)
            best = matched.get("executable")
            if best:
                result["candidates"].sort(key=lambda item: 0 if item["path"].casefold() == best.casefold() else (2 if item.get("utility") else 1))
        return dict(stage="exes", folder=payload["folder"], rows=result["candidates"][:100],
                    limited=result["limited"] or len(result["candidates"]) > 100)
    if action == "search":
        try:
            return dict(stage="matches", rows=catalog("metadata_search", {"value": payload["query"]}))
        except (RuntimeError, OSError):
            raise ValueError("Metadata sources are temporarily unavailable. Search again or add the game without artwork.") from None
    if action == "preview":
        folder, exe = payload["folder"], payload["executable"]
        ns["check_pe"](ns["safe_path"](ns["safe_path"](root, folder), exe))
        rows = catalog("inspect")["records"]
        if payload["provider"] == "manual":
            from PIL import Image
            stream = io.BytesIO(); Image.new("RGB", (4, 4), "#142c36").save(stream, format="PNG")
            art = dict(base64=base64.b64encode(stream.getvalue()).decode(), sha256=hashlib.sha256(stream.getvalue()).hexdigest())
            identity = folder.casefold() + ("\0" + exe.casefold() if installers else "")
            card = dict(id="nas-" + hashlib.sha256(identity.encode()).hexdigest()[:24], name=ns["search_title"](folder)[:160],
                        subtitle="", description="", genre="", accent="#d9ff82", metadata_url="")
            details = dict(provider_id="manual", card=card, cover=art, background=art)
        else:
            details = catalog("metadata_details", {"value": payload["provider"]})
        prepared = prepare(rows, folder, details, exe)
        prepared["proposed"]["delivery"] = "on_request"
        sized(prepared["proposed"])
        prepared["merged"]["total_size"] = prepared["proposed"]["total_size"]
        code = uuid.uuid4().hex
        obj = SearchType.create("ngs_launcher/catalog_config")
        obj.set_value("code", code); obj.set_value("description", "NGS remote catalogizer v1")
        save(obj, dict(device=device, created=time.time(), state="review", prepared=prepared))
        return dict(stage="review", preview=code, card=prepared["merged"], preserved=prepared["preserved"], artwork=ns["review_artwork"](prepared))
    if action in ("estimate", "download"):
        existing = find(payload["preview"]) if action == "download" else None
        previous = json.loads(existing.get_value("data")) if existing else {}
        if existing and (previous.get("device") != device or previous.get("state") != "transfer" or previous.get("installers", False) != installers):
            raise PermissionError("Download identity changed")
        rows = catalog("inspect")["records"] if not existing else []
        card = previous["card"] if existing else next((r["published"] for r in rows if r["card"]["id"] == payload["game"]), None)
        if not card or (card.get("kind") == "installer") != installers or card.get("id") != payload["game"] or card.get("revision") != payload["revision"] or card.get("delivery") != "on_request":
            raise ValueError("The game entry has changed. Refresh the catalog and create a new job.")
        if card.get("source_root") and ns["path_key"](card["source_root"]) not in {
                ns["path_key"](source), ns["path_key"](source.replace("NAS", "NAS"))}:
            raise ValueError("This game's source is not mounted on the NGS server.")
        if action == "estimate":
            return dict(total_size=sized(card)["total_size"], workspace=5 * (ns["PACKAGE_SIZE"] + 1024 * 1024))
        obj = find(payload["preview"])
        if obj:
            values = json.loads(obj.get_value("data"))
            if values.get("device") != device or values.get("card") != card or values.get("state") != "transfer":
                raise PermissionError("Download identity changed")
        else:
            launch = {}
            if payload.get('archive') == 'tar-zstd': launch['_stream_archive'] = True
            if card.get("manifest"):
                from pyasm.common import Environment
                reference = card["manifest"]
                raw = ns["safe_path"](Path(Environment.get_asset_dir()), reference["path"].removeprefix("/assets/")).read_bytes()
                if len(raw) != reference["size"] or hashlib.sha256(raw).hexdigest() != reference["sha256"]:
                    raise ValueError("Previous launch settings are unavailable")
                old = ns["validate_manifest"](json.loads(raw), card)
                launch.update(arguments=old["arguments"], working_directory=old["working_directory"])
            queued = packages("queue", dict(card=card, launch=launch))
            obj = SearchType.create("ngs_launcher/catalog_config")
            obj.set_value("code", payload["preview"]); obj.set_value("description", "NGS remote catalogizer v1")
            values = dict(device=device, created=time.time(), state="transfer", card=card, job=queued["job"])
            save(obj, values)
        if values.pop('worker_restarts', None) is not None: save(obj, values)
        packages("stream_build", dict(job=values["job"], private=settings["private"], certificate=settings["certificate"]))
        return packages("stream_status", dict(job=values["job"], ack=-1))
    obj = find(payload["preview"])
    values = json.loads(obj.get_value("data")) if obj else {}
    if not obj or values.get("device") != device or values.get("installers", False) != installers: raise PermissionError("Unknown catalogizer preview")
    state = values["state"]
    if action == 'custom':
        complete = ns['custom_card_chunk'](values, payload)
        save(obj, values)
        if not complete: return dict(complete=False)
        return dict(complete=True, stage='review', preview=obj.get_code(), card=values['prepared']['merged'],
                    artwork=ns['review_artwork'](values['prepared']))
    def results(offset=0, hidden=True):
        rows = values["rows"]
        summary = {name: sum(row["state"] == name for row in rows) for name in ("added", "already", "manual", "error")}
        visible = [row for row in rows if not hidden or not row.get("published")]
        return dict(stage="done", state="done", preview=obj.get_code(), rows=visible[offset:offset+20],
            selected_id=values.get("selected_id", ""),
            result_offset=offset, result_total=len(visible), hide_published=hidden, summary=summary, cancelled=values.get("cancelled", False))
    if action == "results":
        if state != "done" or "rows" not in values: raise ValueError("Batch is not finished")
        return results(payload["offset"], payload["hide_published"])
    if action in ("transfer", "repair"):
        if state != "transfer": raise ValueError("Not a download session")
        if action == "repair": return packages("stream_repair", dict(job=values["job"], index=payload["index"]))
        result = packages("stream_status", dict(job=values["job"], ack=payload["ack"]))
        if result.get('state') == 'error' and result.get('error') == 'WorkerStopped' and values.get('worker_restarts', 0) < 3:
            values['worker_restarts'] = values.get('worker_restarts', 0) + 1
            save(obj, values)
            packages('stream_build', dict(job=values['job'], private=settings['private'], certificate=settings['certificate']))
            result = packages('stream_status', dict(job=values['job'], ack=payload['ack']))
        return result
    if action == "status" and state == "batch":
        if values["done"] < len(values["folders"]):
            folder = values["folders"][values["done"]]
            row = dict(name=folder, folder=folder, state="manual", status="Manual review needed")
            try:
                records = catalog("inspect")["records"]
                if fully_published(folder, records):
                    row.update(state="already", status="Already in the catalog", published=True)
                else:
                    found = ns["scan_executables"](ns["safe_path"](root, folder))
                    if installers:
                        candidates = [entry for entry in found["candidates"] if Path(entry["path"]).name.casefold().startswith(("setup", "install"))]
                        chosen_exe = candidates[0]["path"] if len(candidates) == 1 else None
                        matched_info = {"name": ns.get("search_title", lambda v: v)(folder), "provider_id": "", "matched": False}
                    elif "match_known_game" in ns:
                        known_obj = find("known_games")
                        extra = json.loads(known_obj.get_value("data") or "{}").get("games", []) if known_obj else []
                        for rec in records:
                            c = rec["card"]
                            if c.get("kind") != "installer" and c.get("name") and c.get("executable"):
                                extra.append({"n": c["name"], "e": [c["executable"]], "a": [c["source_folder"]] if c.get("source_folder") else [], "p": rec.get("import_state", {}).get("provider_id", "")})
                        matched_info = ns["match_known_game"](folder, found["candidates"], extra_entries=extra, source=root)
                        chosen_exe = matched_info["executable"]
                    else:
                        candidates = [entry for entry in found["candidates"] if not entry["utility"]]
                        chosen_exe = candidates[0]["path"] if len(candidates) == 1 else None
                        matched_info = {"name": ns.get("search_title", lambda v: v)(folder), "provider_id": "", "matched": False}
                    if found["limited"] or found["skipped"] or not chosen_exe:
                        row["status"] = "Choose an executable manually"
                    else:
                        details = None
                        prov = matched_info.get("provider_id") or ""
                        if prov and prov != "manual":
                            try:
                                details = catalog("metadata_details", {"value": prov})
                            except Exception:
                                details = None
                        if details is None:
                            query_title = matched_info.get("name") or ns["search_title"](folder)
                            matches = catalog("metadata_search", {"value": query_title})
                            allowed_keys = {ns["title_key"](folder), ns["title_key"](query_title)}
                            exact = [match for match in matches if ns["title_key"](match["name"]) in allowed_keys]
                            if not exact and len(matches) == 1 and matched_info.get("matched"):
                                exact = matches
                            if len(exact) == 1:
                                details = catalog("metadata_details", {"value": exact[0]["app_id"]})
                        if details is None:
                            row["status"] = "Confirm artwork manually"
                        else:
                            if matched_info.get("matched") and matched_info.get("name") and ns["title_key"](details["card"].get("name", "")) == ns["title_key"](folder) and details["card"].get("name") == folder:
                                details["card"]["name"] = matched_info["name"][:160]
                            ns["check_pe"](ns["safe_path"](ns["safe_path"](root, folder), chosen_exe))
                            prepared = prepare(records, folder, details, chosen_exe)
                            prepared["proposed"]["delivery"] = "on_request"
                            sized(prepared["proposed"])
                            imported = catalog("import", dict(details, card=prepared["proposed"],
                                expected=prepared["current"]["token"] if prepared["current"] else None, edits=prepared["edits"], confirmed=True))
                            row.update(state="added", status="Added", published=True, name=imported["card"]["name"])
                            values["selected_id"] = imported["card"]["id"]
            except (ValueError, OSError, RuntimeError) as error:
                row.update(state="error", status=str(error)[:200])
            values["rows"].append(row); values["done"] += 1; save(obj, values)
        else:
            values.update(state="export"); save(obj, values)
        return dict(stage="batch", state=values["state"], preview=obj.get_code(), rows=values["rows"][-20:], done=values["done"], total=len(values["folders"]))
    if action == "status" and state == "review":
        return dict(stage="review", preview=obj.get_code(), card=values["prepared"]["merged"], artwork=ns["review_artwork"](values["prepared"]))
    if action == "publish":
        if state != "review": return dict(stage="done" if state in ("done", "cancelled", "error") else "working", preview=obj.get_code(), state=state)
        if time.time() - values["created"] > 900: raise ValueError("The confirmation has expired. Prepare the entry again.")
        prepared = values["prepared"]
        current = next((r for r in catalog("inspect")["records"] if r["card"]["id"] == prepared["proposed"]["id"]), None)
        if (current["token"] if current else None) != (prepared["current"]["token"] if prepared["current"] else None):
            raise ValueError("The entry has changed. Prepare and confirm it again.")
        prepared["proposed"]["delivery"] = "on_request"
        sized(prepared["proposed"])
        imported = catalog("import", dict(prepared["details"], card=prepared["proposed"],
            expected=prepared["current"]["token"] if prepared["current"] else None, edits=prepared["edits"], confirmed=True))
        if not installers and imported["card"].get("executable") and imported["card"].get("name"):
            known_obj = find("known_games")
            known_doc = json.loads(known_obj.get_value("data") or "{}") if known_obj else {}
            existing_list = known_doc.get("games", [])
            by_n = {(g.get("n") or "").casefold(): g for g in existing_list if isinstance(g, dict)}
            prov = prepared["details"].get("provider_id", "")
            by_n[imported["card"]["name"].casefold()] = {"n": imported["card"]["name"], "e": [imported["card"]["executable"]], "a": [imported["card"]["source_folder"]] if imported["card"].get("source_folder") else [], "p": prov if prov != "manual" else ""}
            if not known_obj:
                known_obj = SearchType.create("ngs_launcher/catalog_config")
                known_obj.set_value("code", "known_games"); known_obj.set_value("description", "NGS remote catalogizer v1")
            save(known_obj, dict(schema=1, synced_at=known_doc.get("synced_at", 0), games=list(by_n.values())[:5000]))
        values.update(state="export", card=imported["card"]); values.pop("prepared", None); save(obj, values)
        return dict(stage="working", preview=obj.get_code(), state="export")
    if action == "cancel":
        if state == "working": packages("cancel", {"job": values["job"]})
        if state == "batch":
            values.update(state="export", cancelled=True); save(obj, values)
            return dict(stage="working", state="export", preview=obj.get_code())
        if state not in ("done", "export", "activate"):
            values["state"] = "cancelled"; values.pop("prepared", None); save(obj, values)
        return dict(stage="done", state=values["state"], cancelled=values["state"] == "cancelled")
    if state == "working":
        result = packages("status", {"job": values["job"]})
        if result["state"] == "error":
            values.update(state="error", error=result.get("error", "PackageError")); save(obj, values)
        elif result["state"] == "ready":
            document, transport = result["result"]["document"], result["result"]["transport"]
            transport["key"] = settings["certificate"]
            transport["signature"] = base64.b64encode(ns["server_sign"](settings["private"], ns["transport_payload"](transport))).decode()
            document["http"] = transport
            ns["validate_manifest"](document)
            data = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            prepared = values["prepared"]
            digest = hashlib.sha256(data).hexdigest()
            current = next((r for r in catalog("inspect")["records"] if r["card"]["id"] == prepared["proposed"]["id"]), None)
            published = current["published"] if current else {}
            if (published.get("manifest", {}).get("sha256") == digest
                    and all(published.get(key) == prepared["proposed"].get(key) for key in ("id", "source_folder", "executable", "source_root"))):
                imported = {"card": published}
            else:
                imported = catalog("import", dict(prepared["details"], card=prepared["proposed"],
                    expected=prepared["current"]["token"] if prepared["current"] else None, edits=prepared["edits"],
                    confirmed=True, manifest=dict(base64=base64.b64encode(data).decode(), sha256=digest)))
            values.update(state="export", card=imported["card"]); values.pop("prepared", None); save(obj, values)
        else:
            return dict(stage="working", state="working", preview=obj.get_code(), done=result.get("done", 0), total=result.get("total", 0))
    elif state == "export":
        values.update(state="activate", export=catalog("export")); save(obj, values)
    elif state == "activate":
        try:
            catalog("activate", values["export"])
            values.update(state="done"); values.pop("prepared", None)
        except ValueError as error:
            if "newer export" not in str(error): raise
            values.update(state="export")
        save(obj, values)
    if values["state"] == "done" and "rows" in values:
        return results()
    return dict(stage="done" if values["state"] in ("done", "cancelled", "error") else "working",
                state=values["state"], preview=obj.get_code(), card=values.get("card", {}), error=values.get("error", ""),
                rows=values.get("rows", [])[-20:], cancelled=values.get("cancelled", False))


def remote_source():
    helpers = (helper_source() + "\nimport ctypes,re\nGAMES_ROOT='/mnt/ngs-games'\n"
        + "UTILITY=re.compile(" + repr(UTILITY.pattern) + ",re.I)\nMAX_DEPTH=" + str(MAX_DEPTH)
        + "\nMAX_ENTRIES=200000\nMAX_EXES=" + str(MAX_EXES) + "\n"
        + "\n".join(inspect.getsource(fn) for fn in (local_folder, scan_executables, scan_folders, prepare_import, title_key,
            validate_catalog_request, transport_payload, server_sign, review_artwork, custom_card_chunk, catalog_action, package_action, stream_action, stream_worker, stream_worker_alive, repair_stream_block, release_stream)))
    return packed_script(inspect.getsource(remote_action),
                         "remote_action(action,payload,device,ngs_compile(" + repr(helpers) + "))", "action,payload,device")


def enable_on_request(client):
    from tools.tactic_catalog import contract_source, invoke
    result = client.script(r'''
import json
from pyasm.biz import Project
from pyasm.common import Environment
from pyasm.search import Search,DbContainer
p=Project.get()
if (not Environment.get_security().is_admin() or p.get_code()!='ngs_launcher' or p.get_database_name()!='ngs_launcher'
    or p.get_value('description')!='NGS handheld game catalog; managed from D:/code/NGS'):raise PermissionError('NGS only')
ns={};exec(contract,ns)
sql=DbContainer.get(p.get_project_db_resource());sql.do_query('SELECT pg_advisory_xact_lock(1313297219)')
changed=[]
for obj in Search('ngs_launcher/game').get_sobjects():
    raw=obj.get_value('published_data')
    if not raw or obj.get_value('status')!='published':continue
    card=ns['validate_card'](json.loads(raw))
    if not card.get('available_on_nas',True) or not card['executable'] or card.get('delivery')=='on_request':continue
    if card.get('source_root') and ns['path_key'](card['source_root']) not in {ns['path_key'](r'\\NAS\Files\games'),ns['path_key'](r'\\NAS\Files\games')}:continue
    card['delivery']='on_request';card['revision']=int(obj.get_value('revision'))+1
    ns['validate_card'](card)
    obj.set_value('revision',card['revision']);obj.set_value('published_data',json.dumps(card,ensure_ascii=False));obj.commit()
    changed.append(card['id'])
return dict(changed=changed,legacy_manifests_preserved=True)
''', contract=contract_source())
    exported = invoke(client, "export")
    invoke(client, "activate", exported)
    return result


def provision(client, certificate, private):
    return client.script('''
import json,secrets,shutil
from pyasm.biz import Project,Schema
from pyasm.common import Environment,Container
from pyasm.search import Search,SearchType,DbContainer
from pyasm.security import Login
p=Project.get()
if (not Environment.get_security().is_admin() or p.get_code()!='ngs_launcher' or p.get_database_name()!='ngs_launcher'
    or p.get_value('description')!='NGS handheld game catalog; managed from D:/code/NGS'): raise PermissionError('NGS only')
if not shutil.which('openssl'): raise ValueError('OpenSSL is required for package-only signatures')
def find(kind,field,value):
    q=Search(kind);q.add_filter(field,value);return q.get_sobject()
sql=DbContainer.get(p.get_project_db_resource());sql.do_query('SELECT pg_advisory_xact_lock(1313297223)')
registry=find('sthpw/search_object','search_type','ngs_launcher/catalog_config')
columns={r[0] for r in sql.do_query("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name='catalog_config'")}
if bool(registry)!=bool(columns) or (registry and (columns!={'id','code','description','data'} or registry.get_value('description')!=owner
    or registry.get_value('namespace')!='ngs_launcher' or registry.get_value('table_name')!='catalog_config')): raise ValueError('Foreign catalogizer schema')
if not registry:
    registry=SearchType(SearchType.SEARCH_TYPE)
    for k,v in dict(search_type='ngs_launcher/catalog_config',namespace='ngs_launcher',database='{project}',table_name='catalog_config',
        schema='public',class_name='pyasm.search.SObject',title='NGS remote catalogizer',description=owner).items(): registry.set_value(k,v)
    registry.commit();sql.do_update('CREATE TABLE catalog_config (id serial PRIMARY KEY,code varchar(64) UNIQUE NOT NULL,description text NOT NULL,data text NOT NULL)')
    (Container.get('DatabaseImpl:table_info') or {}).pop(str(p.get_project_db_resource()),None)
    schema=Schema.get_by_code('ngs_launcher');schema.add_search_type('ngs_launcher/catalog_config');schema.commit()
group_name='ngs_launcher_catalog_worker'
kinds=('ngs_launcher/game','ngs_launcher/catalog','ngs_launcher/package_job','ngs_launcher/catalog_config','ngs_launcher/api_device','sthpw/snapshot','sthpw/file')
rules=('<rules><rule group="project" code="*" access="deny"/><rule group="project" code="ngs_launcher" access="allow"/>'
       '<rule group="search_type" default="deny"/><rule group="sobject" default="deny"/>'
       +''.join('<rule group="search_type" code="%s" access="edit"/><rule group="sobject" search_type="%s" project="ngs_launcher" access="edit"/>'%(kind,kind) for kind in kinds)
       +'<rule group="search_type" code="sthpw/search_object" access="view"/>'
       '<rule group="search_type" code="config/naming" access="view"/><rule group="search_type" code="config/custom_script" access="view"/>'
       '<rule group="api_method" default="deny"/><rule group="api_cmd" default="deny"/></rules>')
group=find('sthpw/login_group','login_group',group_name)
if group and (group.get_value('description')!=owner or group.get_value('project_code')!='ngs_launcher' or group.get_value('access_rules')!=rules): raise ValueError('Foreign or modified catalogizer group')
if not group:
    group=SearchType.create('sthpw/login_group')
    for k,v in dict(code=group_name,login_group=group_name,project_code='ngs_launcher',description=owner,access_level='none',is_default=False,access_rules=rules).items(): group.set_value(k,v)
    group.commit()
credential=find('ngs_launcher/api_service','code','catalog');worker=find('sthpw/login','login','ngs_catalog_worker')
if bool(credential)!=bool(worker): raise ValueError('Catalogizer account ownership differs')
if not worker:
    password=secrets.token_hex(32);worker=Login.create('ngs_catalog_worker',password,groups=[group_name],display_name='NGS catalog worker')
    credential=SearchType.create('ngs_launcher/api_service')
    for k,v in dict(code='catalog',login='ngs_catalog_worker',password=password).items(): credential.set_value(k,v)
    credential.commit()
elif credential.get_value('login')!='ngs_catalog_worker' or set(worker.get_sub_group_names())!={group_name}: raise ValueError('Catalogizer account changed')
devices=[o.get_code() for o in Search('ngs_launcher/api_device').get_sobjects() if o.get_value('enabled') is True
    and (o.get_value('name')=='NGS current PC' or o.get_value('name').startswith('Handheld personal API '))]
config=find('ngs_launcher/catalog_config','code','service')
if config and config.get_value('description')!=owner: raise ValueError('Foreign catalogizer configuration')
if not config:
    config=SearchType.create('ngs_launcher/catalog_config');config.set_value('code','service');config.set_value('description',owner)
    config.set_value('data',json.dumps(dict(private=private,certificate=certificate,devices=devices)));config.commit()
else:
    values=json.loads(config.get_value('data'));values['devices']=sorted(set(values['devices'])|set(devices));config.set_value('data',json.dumps(values));config.commit()
known=find('ngs_launcher/catalog_config','code','known_games')
if known and known.get_value('description')!=owner: raise ValueError('Foreign catalogizer known_games configuration')
if not known:
    known=SearchType.create('ngs_launcher/catalog_config');known.set_value('code','known_games');known.set_value('description',owner)
    known.set_value('data',json.dumps(dict(schema=1,synced_at=0,games=[])));known.commit()
q=Search('config/custom_script');q.add_filter('folder','ngs');q.add_filter('title','catalog');script=q.get_sobject()
if script and script.get_value('description')!=owner: raise ValueError('Foreign catalogizer script')
if not script: script=SearchType.create('config/custom_script')
for k,v in dict(folder='ngs',title='catalog',description=owner,language='python',script=source).items(): script.set_value(k,v)
script.commit()
return dict(project='ngs_launcher',script='ngs/catalog',authorized_devices=len(devices),certificate=json.loads(config.get_value('data'))['certificate'])
''', owner=OWNER, source=remote_source(), certificate=certificate, private=private)


def main():
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--apply", action="store_true"); args = parser.parse_args()
    if not args.apply: parser.error("Use --apply to install the restricted NGS catalogizer")
    storage = PortableStorage(Path(__file__).resolve().parents[1]); storage.configure_process()
    client = admin_client(storage.read_settings()["tactic_url"])
    before = client.script(inspect.getsource(project_snapshot) + "\nreturn project_snapshot()")
    with RsaKey() as signer, RsaKey((storage.config / "update-signing-key.bin").read_bytes(), private=True) as root:
        public = signer.export()
        certificate = dict(public=base64.b64encode(public).decode(), signature=base64.b64encode(root.sign(package_key_payload(public))).decode())
        result = provision(client, certificate, private_pem(signer.export(private=True)))
    from server.plugin.known_games_data import KNOWN_GAMES_ZLIB_B64
    client.script('''
import base64, zlib
from pathlib import Path
from pyasm.common import Environment
asset_root = Path(Environment.get_asset_dir()).resolve() / 'ngs_launcher'
asset_root.mkdir(parents=True, exist_ok=True)
(asset_root / 'known-games.json').write_bytes(zlib.decompress(base64.b64decode(blob)))
''', blob=KNOWN_GAMES_ZLIB_B64)
    update_scripts(client)
    after = client.script(inspect.getsource(project_snapshot) + "\nreturn project_snapshot()")
    assert before == after, "Project metadata changed"
    with RsaKey((storage.root / "assets/update-public-key.bin").read_bytes()) as root:
        root.verify(package_key_payload(base64.b64decode(result["certificate"]["public"])), base64.b64decode(result["certificate"]["signature"]))
    result.update(protected_project_metadata_unchanged=True)
    atomic_write(storage.root / "reports/remote-catalog-deployed.json", json.dumps(result, ensure_ascii=False, indent=2).encode())
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__": main()
