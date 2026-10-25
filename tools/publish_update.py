"""Publish signed HTTP releases in ngs_launcher; optionally mirror the transition to SMB."""

import argparse
import base64
import hashlib
import inspect
import json
import os
import shutil
import uuid
import zipfile
from pathlib import Path

from ngs.catalog import atomic_write, CatalogCache
from ngs.portable import DEFAULT_TACTIC_URL, PortableStorage
from ngs.tactic import TacticClient
from ngs.update_crypto import RsaKey
from ngs.updates import validate_release, verify_envelope, archive_entries, VERSION_PATTERN
from tools.tactic_project import DEFAULT_CREDENTIALS, admin_client, admin_password, project_snapshot


UPDATE_SHARE = r"\\NAS\Files\games\NGS"


def signing_key(storage):
    private = storage.config / "update-signing-key.bin"
    public = storage.root / "assets/update-public-key.bin"
    if not private.exists():
        if public.exists():
            raise ValueError("The pinned signing key exists but its private key is missing; restore it from backup")
        with RsaKey() as key:
            with private.open("xb") as stream:
                stream.write(key.export(private=True))
            with public.open("xb") as stream:
                stream.write(key.export())
    with RsaKey(private.read_bytes(), private=True) as key:
        if key.export() != public.read_bytes():
            raise ValueError("The signing key differs from the pinned client key")
    return private


def release_upload(release, action, contract, data="", offset=0):
    import base64, hashlib, json, os
    from pathlib import Path
    from pyasm.biz import Project
    from pyasm.common import Environment
    from pyasm.search import DbContainer
    project = Project.get()
    if (not Environment.get_security().is_admin() or project.get_code() != "ngs_launcher"
            or project.get_database_name() != "ngs_launcher"
            or project.get_value("description") != "NGS handheld game catalog; managed from D:/code/NGS"):
        raise PermissionError("Only NGS administrative preparation can upload releases")
    namespace = {}; exec(contract, namespace)
    namespace["validate_release"](release)
    if action not in ("begin", "write", "finish"):
        raise ValueError("Invalid NGS release upload action")
    DbContainer.get(project.get_project_db_resource()).do_query("SELECT pg_advisory_xact_lock(1313297222)")
    assets = Path(Environment.get_asset_dir()).resolve()
    own = assets / "ngs_launcher"
    folder = own / "releases"
    if own.is_symlink() or folder.is_symlink() or not folder.resolve().is_relative_to(own.resolve()):
        raise ValueError("Redirected NGS releases")
    folder.mkdir(exist_ok=True)
    target = folder / (release["sha256"] + ".zip")
    partial = folder / (release["sha256"] + ".part")
    marker = folder / (release["sha256"] + ".upload.json")
    for path in (target, partial, marker):
        if path.is_symlink() or not path.resolve().is_relative_to(folder.resolve()):
            raise ValueError("Redirected NGS upload file")
    def digest(path):
        result = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(4 * 1024 * 1024): result.update(chunk)
        return result.hexdigest()
    result = dict(path="/assets/ngs_launcher/releases/" + target.name, size=release["size"], sha256=release["sha256"])
    if target.exists():
        if target.stat().st_size != release["size"] or digest(target) != release["sha256"]:
            raise ValueError("Immutable HTTP release differs")
        return dict(result, ready=True)
    owner = dict(schema=1, application="NGS release upload", filename=release["filename"], size=release["size"], sha256=release["sha256"])
    if marker.exists():
        if marker.stat().st_size > 4096 or json.loads(marker.read_bytes()) != owner:
            raise ValueError("Upload ownership differs")
    else:
        if partial.exists(): raise ValueError("Unowned upload file")
        with marker.open("xb") as stream: stream.write(json.dumps(owner).encode())
    if not partial.exists():
        with partial.open("xb"): pass
    count = partial.stat().st_size
    if count > release["size"]: raise ValueError("Oversized partial upload")
    if action == "write":
        if type(offset) is not int or offset != count or not isinstance(data, str) or len(data) > 6 * 1024 * 1024:
            raise ValueError("Invalid upload offset or body")
        chunk = base64.b64decode(data, validate=True)
        if not 0 < len(chunk) <= min(4 * 1024 * 1024, release["size"] - count):
            raise ValueError("Invalid upload chunk size")
        with partial.open("r+b") as stream:
            stream.seek(offset); stream.write(chunk); stream.flush(); os.fsync(stream.fileno())
        count += len(chunk)
    if action == "finish":
        if count != release["size"]: raise ValueError("Incomplete HTTP release")
        if digest(partial) != release["sha256"]:
            with partial.open("wb"): pass
            raise ValueError("HTTP release SHA256 mismatch; corrupt upload reset")
        if os.name == "nt":
            partial.rename(target)
        else:
            os.link(partial, target)
            partial.unlink()
        marker.unlink()
        return dict(result, ready=True)
    return dict(result, ready=False, offset=count)


def upload_release(client, archive, release):
    contract = "import re\nVERSION_PATTERN=" + repr(VERSION_PATTERN) + "\n" + inspect.getsource(validate_release)
    source = inspect.getsource(release_upload) + "\nreturn release_upload(release, action, contract, data, offset)"
    def call(action, **fields):
        return client.script(source, release=release, action=action, contract=contract,
                             data=fields.get("data", ""), offset=fields.get("offset", 0))
    state = call("begin")
    if state["ready"]: return state
    with archive.open("rb") as stream:
        stream.seek(state["offset"])
        while chunk := stream.read(4 * 1024 * 1024):
            state = call("write", offset=stream.tell() - len(chunk), data=base64.b64encode(chunk).decode("ascii"))
    return call("finish")


def release_action(envelope, activate=False):
    import hashlib, json, os, tempfile
    from pathlib import Path
    from pyasm.biz import Project, Schema
    from pyasm.common import Environment, Container, Config
    from pyasm.search import Search, SearchType, DbContainer
    project = Project.get()
    if (not Environment.get_security().is_admin() or project.get_code() != "ngs_launcher"
            or project.get_database_name() != "ngs_launcher"
            or project.get_value("description") != "NGS handheld game catalog; managed from D:/code/NGS"):
        raise PermissionError("Only the owned NGS project can publish updates")
    before = Config.get_value("install", "default_project")
    owner, kind = "NGS launcher releases v1", "ngs_launcher/app_release"
    sql = DbContainer.get(project.get_project_db_resource())
    sql.do_query("SELECT pg_advisory_xact_lock(1313297220)")
    query = Search("sthpw/search_object"); query.add_filter("search_type", kind)
    registry = query.get_sobject()
    columns = {r[0] for r in sql.do_query("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name='app_release'")}
    expected = {"id", "code", "name", "description", "timestamp", "s_status", "published_data"}
    if bool(registry) != bool(columns) or (registry and (columns != expected
            or registry.get_value("description") != owner or registry.get_value("table_name") != "app_release"
            or registry.get_value("namespace") != "ngs_launcher")):
        raise ValueError("Conflicting NGS release schema")
    if not registry:
        if activate:
            raise ValueError("Release schema has not been committed")
        registry = SearchType(SearchType.SEARCH_TYPE)
        for key, value in dict(search_type=kind, namespace="ngs_launcher", database="{project}", table_name="app_release",
                              schema="public", class_name="pyasm.search.SObject", title="NGS releases", description=owner).items():
            registry.set_value(key, value)
        registry.commit()
        sql.do_update('CREATE TABLE app_release (id serial PRIMARY KEY, code varchar(64) UNIQUE NOT NULL, name varchar(256), description text, timestamp timestamp DEFAULT now(), s_status varchar(32), published_data text NOT NULL)')
        (Container.get("DatabaseImpl:table_info") or {}).pop(str(project.get_project_db_resource()), None)
        schema = Schema.get_by_code("ngs_launcher")
        schema.add_search_type(kind); schema.commit()
    release = json.loads(envelope["payload"])
    digest = release.get("sha256", "")
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("Invalid release digest")
    own = Path(Environment.get_asset_dir()).resolve() / "ngs_launcher"
    folder = own / "releases"
    if own.is_symlink() or folder.is_symlink() or not folder.resolve().is_relative_to(own.resolve()):
        raise ValueError("Redirected NGS releases")
    archive = folder / (digest + ".zip")
    if archive.is_symlink() or not archive.is_file() or archive.stat().st_size != release["size"]:
        raise ValueError("Upload and verify the HTTP release before publication")
    with archive.open("rb") as stream:
        check = hashlib.sha256()
        while chunk := stream.read(4 * 1024 * 1024): check.update(chunk)
    if check.hexdigest() != digest:
        raise ValueError("HTTP release differs before publication")
    query = Search(kind); query.add_filter("code", release["version"])
    obj = query.get_sobject()
    encoded = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if obj:
        if obj.get_value("published_data") != encoded:
            raise ValueError("An immutable release with this version already exists")
    else:
        if activate:
            raise ValueError("Release has not been committed")
        obj = SearchType.create(kind)
        for key, value in dict(code=release["version"], name="NAS Game Switcher " + release["version"],
                               description=release["notes"], s_status="published", published_data=encoded).items():
            obj.set_value(key, value)
        obj.commit()
    if not activate:
        return dict(project="ngs_launcher", version=release["version"], status="recorded")
    query = Search(kind); query.add_order_by("code desc")
    latest = query.get_sobject()
    assets = Path(Environment.get_asset_dir()).resolve()
    directory = assets / "ngs_launcher"
    if directory.is_symlink() or not directory.resolve().is_relative_to(assets):
        raise ValueError("Redirected NGS assets")
    target = directory / "updates.json"
    if target.is_symlink() or not target.resolve().is_relative_to(directory):
        raise ValueError("Redirected NGS update feed")
    name = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".ngs-update-", delete=False) as stream:
            name = stream.name
            stream.write(latest.get_value("published_data").encode("utf8")); stream.flush(); os.fsync(stream.fileno())
        os.replace(name, target)
    finally:
        if name and os.path.exists(name): os.unlink(name)
    if Config.get_value("install", "default_project") != before:
        raise RuntimeError("Default project changed unexpectedly")
    return dict(project="ngs_launcher", version=release["version"], latest=latest.get_code(),
                history=len(Search(kind).get_sobjects()), feed="/assets/ngs_launcher/updates.json")


def publish_legacy(archive, envelope, release):
    share = Path(UPDATE_SHARE)
    if str(share) != r"\\NAS\Files\games\NGS" or not share.parent.is_dir():
        raise ValueError("Only the explicitly authorized NGS share can receive the transition release")
    share.mkdir(exist_ok=True)
    target = share / archive.name
    partial = share / (archive.name + "." + uuid.uuid4().hex + ".partial")
    try:
        if target.exists():
            with target.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != release["sha256"]:
                    raise ValueError("A different NAS package already has this name")
        else:
            shutil.copyfile(archive, partial)
            with partial.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != release["sha256"]:
                    raise ValueError("NAS copy digest mismatch")
            partial.rename(target)
        atomic_write(share / (archive.name + ".sha256"), (release["sha256"] + "  " + archive.name + "\n").encode("utf8"))
        atomic_write(share / (archive.name + ".release.json"), json.dumps(envelope, ensure_ascii=False).encode("utf8"))
        atomic_write(share / "NGS-updates.json", b'{"schema":1,"application":"NAS Game Switcher updater"}')
    finally:
        partial.unlink(missing_ok=True)
    return str(target)


def publish(archive, notes, storage, base_url, credentials, legacy_smb=False):
    archive = Path(archive).resolve()
    if archive.parent not in (storage.root / "dist/windows", storage.root / "dist") or archive.suffix != ".zip":
        raise ValueError("Only a checked release ZIP from NGS/dist/windows can be published")
    folder = archive.with_suffix("")
    report = json.loads((storage.root / "reports/windows-portable.json").read_text(encoding="utf8"))
    if report["folder"] != str(folder) or len(report["runs"]) != 2 or not all(r["ok"] for r in report["runs"]):
        raise ValueError("Check this exact build before publication")
    metadata = json.loads((folder / "runtime/release.json").read_text(encoding="utf8"))
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    with zipfile.ZipFile(archive) as package:
        release = validate_release(dict(schema=1, version=metadata["version"], platform="windows-x64", filename=archive.name,
            sha256=digest, size=archive.stat().st_size, unpacked_size=sum(p.file_size for p in package.infolist()), notes=notes))
        archive_entries(package, release)
        if package.testzip() is not None:
            raise ValueError("Corrupted ZIP")
        if json.loads(package.read(folder.name + "/runtime/release.json"))["version"] != metadata["version"]:
            raise ValueError("The archive and build folder have different versions")
        if package.read(folder.name + "/runtime/assets/update-public-key.bin") != (storage.root / "assets/update-public-key.bin").read_bytes():
            raise ValueError("The build contains a different update key")
    with RsaKey(signing_key(storage).read_bytes(), private=True) as key:
        payload = json.dumps(release, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        envelope = dict(payload=payload, signature=base64.b64encode(key.sign(payload.encode("utf8"))).decode("ascii"))
    verify_envelope(envelope, (storage.root / "assets/update-public-key.bin").read_bytes())
    client = admin_client(base_url, credentials)
    before = client.script(inspect.getsource(project_snapshot) + "\nreturn project_snapshot()")
    http = upload_release(client, archive, release)
    http_url = CatalogCache(storage, base_url).origin + http["path"]
    target = publish_legacy(archive, envelope, release) if legacy_smb else http_url
    client.script(inspect.getsource(release_action) + "\nreturn release_action(envelope)", envelope=envelope)
    result = client.script(inspect.getsource(release_action) + "\nreturn release_action(envelope, True)", envelope=envelope)
    after = client.script(inspect.getsource(project_snapshot) + "\nreturn project_snapshot()")
    protected_before = [p for p in before["projects"] if p["code"] != "ngs_launcher"]
    protected_after = [p for p in after["projects"] if p["code"] != "ngs_launcher"]
    if protected_before != protected_after or before["default_project"] != after["default_project"]:
        raise RuntimeError("Project metadata/default settings changed unexpectedly")
    result.update(archive=target, http_url=http_url, legacy_smb=legacy_smb,
                  size=release["size"], sha256=digest, protected_projects_unchanged=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", nargs="?", type=Path)
    parser.add_argument("--init-key", action="store_true")
    parser.add_argument("--notes", default="Обновление NAS Game Switcher")
    parser.add_argument("--credentials-env", type=Path, default=DEFAULT_CREDENTIALS)
    parser.add_argument("--legacy-smb", action="store_true", help="Also publish the final transition ZIP for old SMB-only updaters")
    parser.add_argument("--base-url", default=DEFAULT_TACTIC_URL)
    args = parser.parse_args()
    storage = PortableStorage(Path(__file__).resolve().parents[1]); storage.configure_process()
    if args.init_key:
        signing_key(storage)
        print("Update signing key is ready; the private key was not printed or distributed.")
        return
    if args.archive is None:
        parser.error("Choose a checked release archive")
    result = publish(args.archive, args.notes, storage, args.base_url,
        args.credentials_env, legacy_smb=args.legacy_smb)
    (storage.root / "reports/update-published.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
