"""Private TACTIC storage and revision history for per-device library & settings backups."""
from ngs.backups import (
    DOWNLOAD_CHUNK,
    MAX_BACKUPS_PER_DEVICE,
    MAX_BACKUP_BYTES,
    MAX_DEVICES_PER_PROFILE,
    UPLOAD_CHUNK,
    clean_device_name,
    validate_backup_request,
)

BACKUP_ROOT = "/opt/tactic/tactic_data/ngs_launcher/backups"
BACKUP_MARKER = {"schema": 1, "project": "ngs_launcher", "purpose": "player-library-backups"}


def provision_backups():
    import json, os
    from pathlib import Path
    from pyasm.biz import Project
    from pyasm.common import Environment

    project = Project.get()
    if (not Environment.get_security().is_admin()
            or project.get_code() != "ngs_launcher"
            or project.get_database_name() != "ngs_launcher"
            or project.get_value("description") != "NGS handheld game catalog; managed from D:/code/NGS"):
        raise PermissionError("NGS backup storage can only be provisioned in ngs_launcher")
    root = Path("/opt/tactic/tactic_data/ngs_launcher/backups")
    for path in (root, *root.parents):
        if path.is_symlink():
            raise ValueError("Redirected backup storage")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    marker = root / "owner.json"
    if marker.exists():
        if marker.is_symlink() or json.loads(marker.read_text(encoding="utf8")) != BACKUP_MARKER:
            raise ValueError("Backup root belongs to another owner")
    else:
        marker.write_text(json.dumps(BACKUP_MARKER, sort_keys=True), encoding="utf8")
        os.chmod(marker, 0o600)
    return {"backup_root": str(root)}


def backup_action(payload, device, account):
    import base64, hashlib, io, json, os, re, time, zipfile
    from pathlib import Path
    from pyasm.biz import Project
    from pyasm.common import Environment
    from pyasm.search import DbContainer, Search

    project, security = Project.get(), Environment.get_security()
    worker_name = (
        security.get_user_name()
        if hasattr(security, "get_user_name")
        else (security.get_login().get_value("login") if hasattr(security, "get_login") else "")
    )
    if (project.get_code() != "ngs_launcher"
            or project.get_database_name() != "ngs_launcher"
            or security.is_admin()
            or worker_name != "ngs_catalog_worker"):
        raise PermissionError("NGS catalog worker required")

    if not isinstance(account, dict) or not re.fullmatch(r"[0-9a-f]{32}", str(account.get("profile", ""))):
        raise PermissionError("Sign in to back up your database and settings to the server.")

    profile_id = account["profile"]
    client_device = str(account.get("client_device") or device)
    req = validate_backup_request(payload)
    action = req["action"]

    root = Path("/opt/tactic/tactic_data/ngs_launcher/backups")
    marker = root / "owner.json"
    if (not root.is_dir() or root.is_symlink() or not marker.is_file() or marker.is_symlink()
            or json.loads(marker.read_text(encoding="utf8")) != BACKUP_MARKER):
        raise RuntimeError("Backup storage is not ready on the server.")

    sql = DbContainer.get(project.get_project_db_resource())
    sql.do_query("SELECT pg_advisory_xact_lock(1313297226)")

    profile_dir = root / profile_id
    if profile_dir.is_symlink():
        raise ValueError("Invalid profile backup path")
    profile_dir.mkdir(mode=0o700, exist_ok=True)
    os.chmod(profile_dir, 0o700)

    index_path = profile_dir / "index.json"
    if index_path.is_symlink():
        raise ValueError("Invalid backup index")
    index = {"schema": 1, "profile": profile_id, "devices": {}}
    if index_path.is_file():
        try:
            loaded = json.loads(index_path.read_text(encoding="utf8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("devices"), dict):
                index["devices"] = loaded["devices"]
        except Exception:
            pass

    def write_atomic(target, raw_bytes):
        temp = target.with_name(target.name + ".tmp")
        if temp.is_symlink() or (target.exists() and target.is_symlink()):
            raise ValueError("Refusing symlink write")
        with open(temp, "wb") as handle:
            os.chmod(temp, 0o600)
            handle.write(raw_bytes)
        os.replace(temp, target)

    if action == "list":
        devices_out = []
        for dev_id, entry in index["devices"].items():
            if not re.fullmatch(r"[0-9a-f]{32}", str(dev_id)) or not isinstance(entry, dict):
                continue
            revs = []
            for rev in entry.get("backups", []):
                if not isinstance(rev, dict) or not re.fullmatch(r"[0-9a-f]{32}", str(rev.get("id", ""))):
                    continue
                zip_file = profile_dir / dev_id / (rev["id"] + ".zip")
                if not zip_file.is_file() or zip_file.is_symlink():
                    continue
                revs.append({
                    "id": rev["id"],
                    "created_at": int(rev.get("created_at", 0)),
                    "size": int(rev.get("size", 0)),
                    "sha256": str(rev.get("sha256", "")),
                    "games_count": int(rev.get("games_count", 0)),
                })
            revs.sort(key=lambda item: item["created_at"], reverse=True)
            devices_out.append({
                "device": dev_id,
                "name": str(entry.get("name") or dev_id[:8]),
                "updated_at": int(entry.get("updated_at") or (revs[0]["created_at"] if revs else 0)),
                "games_count": int(entry.get("games_count") if "games_count" in entry else (revs[0]["games_count"] if revs else 0)),
                "is_current": dev_id == client_device,
                "backups": revs[:MAX_BACKUPS_PER_DEVICE],
            })
        devices_out.sort(key=lambda item: (not item["is_current"], -item["updated_at"], item["device"]))
        return {"devices": devices_out, "client_device": client_device}

    target_device = req["target_device"]
    device_dir = profile_dir / target_device
    if device_dir.is_symlink():
        raise ValueError("Invalid target device directory")

    if action == "download":
        backup_id = req["backup_id"]
        zip_path = device_dir / (backup_id + ".zip")
        if not zip_path.is_file() or zip_path.is_symlink():
            raise ValueError("The selected backup revision was not found on the server.")
        raw = zip_path.read_bytes()
        total = len(raw)
        if not 1 <= total <= MAX_BACKUP_BYTES:
            raise ValueError("The server backup archive is damaged.")
        digest = hashlib.sha256(raw).hexdigest()
        offset = req["offset"]
        if offset >= total:
            raise ValueError("Invalid download offset")
        chunk = raw[offset:offset + DOWNLOAD_CHUNK]
        return {
            "backup_id": backup_id,
            "target_device": target_device,
            "size": total,
            "sha256": digest,
            "offset": offset,
            "eof": offset + len(chunk) >= total,
            "data": base64.b64encode(chunk).decode("ascii"),
        }

    if target_device not in index["devices"] and len(index["devices"]) >= MAX_DEVICES_PER_PROFILE:
        raise ValueError("The profile has reached its device backup limit.")
    device_dir.mkdir(mode=0o700, exist_ok=True)
    os.chmod(device_dir, 0o700)

    backup_id = req["backup_id"]
    part_path = device_dir / (backup_id + ".part")
    meta_path = device_dir / (backup_id + ".part.json")
    final_path = device_dir / (backup_id + ".zip")
    for candidate in (part_path, meta_path, final_path):
        if candidate.exists() and candidate.is_symlink():
            raise ValueError("Refusing symlinked backup artifact")

    if action == "upload":
        offset = req["offset"]
        chunk = base64.b64decode(req["data"].encode("ascii"), validate=True)
        if offset == 0:
            for stale in device_dir.glob("*.part*"):
                try:
                    if not stale.is_symlink():
                        stale.unlink()
                except OSError:
                    pass
            write_atomic(
                meta_path,
                json.dumps(
                    {
                        "backup_id": backup_id,
                        "target_device": target_device,
                        "device_name": req["device_name"],
                        "sha256": req["sha256"],
                        "size": req["size"],
                        "updated_at": int(time.time()),
                    },
                    sort_keys=True,
                ).encode("utf8"),
            )
            with open(part_path, "wb") as handle:
                os.chmod(part_path, 0o600)
                handle.write(chunk)
            return {"backup_id": backup_id, "offset": len(chunk)}

        if not meta_path.is_file() or not part_path.is_file():
            raise ValueError("Backup upload session not found.")
        meta = json.loads(meta_path.read_text(encoding="utf8"))
        if (meta.get("backup_id") != backup_id
                or meta.get("target_device") != target_device
                or meta.get("sha256") != req["sha256"]
                or meta.get("size") != req["size"]):
            raise ValueError("Backup upload metadata does not match.")
        current_size = part_path.stat().st_size
        if current_size != offset:
            if current_size == offset + len(chunk):
                return {"backup_id": backup_id, "offset": current_size}
            raise ValueError("Backup chunk offset does not match.")
        with open(part_path, "ab") as handle:
            handle.write(chunk)
        return {"backup_id": backup_id, "offset": offset + len(chunk)}

    # action == "finish"
    if not meta_path.is_file() or not part_path.is_file():
        if final_path.is_file():
            return {"stored": True, "backup_id": backup_id, "target_device": target_device}
        raise ValueError("Incomplete backup not found.")
    meta = json.loads(meta_path.read_text(encoding="utf8"))
    raw = part_path.read_bytes()
    if (meta.get("sha256") != req["sha256"]
            or meta.get("size") != req["size"]
            or len(raw) != req["size"]
            or hashlib.sha256(raw).hexdigest() != req["sha256"]):
        raise ValueError("The uploaded backup checksum does not match.")
    with zipfile.ZipFile(io.BytesIO(raw), "r") as archive:
        if "backup.json" not in archive.namelist():
            raise ValueError("The backup archive does not contain backup.json.")

    now = int(time.time())
    os.replace(part_path, final_path)
    os.chmod(final_path, 0o600)
    try:
        meta_path.unlink(missing_ok=True)
    except OSError:
        pass

    dev_entry = index["devices"].get(target_device)
    if not isinstance(dev_entry, dict):
        dev_entry = {"device": target_device, "name": req["device_name"], "backups": []}
    dev_entry["device"] = target_device
    dev_entry["name"] = req["device_name"]
    dev_entry["updated_at"] = now
    dev_entry["games_count"] = req["games_count"]
    revs = [
        item for item in dev_entry.get("backups", [])
        if isinstance(item, dict) and item.get("id") != backup_id and re.fullmatch(r"[0-9a-f]{32}", str(item.get("id", "")))
    ]
    revs.insert(0, {
        "id": backup_id,
        "created_at": now,
        "size": req["size"],
        "sha256": req["sha256"],
        "games_count": req["games_count"],
    })
    keep = revs[:MAX_BACKUPS_PER_DEVICE]
    keep_ids = {item["id"] for item in keep}
    for old_item in revs[MAX_BACKUPS_PER_DEVICE:]:
        old_zip = device_dir / (old_item["id"] + ".zip")
        try:
            if old_zip.is_file() and not old_zip.is_symlink():
                old_zip.unlink()
        except OSError:
            pass
    for stray in device_dir.glob("*.zip"):
        if stray.stem not in keep_ids and not stray.is_symlink():
            try:
                stray.unlink()
            except OSError:
                pass
    dev_entry["backups"] = keep
    index["devices"][target_device] = dev_entry
    write_atomic(index_path, json.dumps(index, ensure_ascii=False, separators=(",", ":")).encode("utf8"))

    # Commit latest backup revision metadata into the TACTIC api_device SObject as well.
    q = Search("ngs_launcher/api_device")
    q.add_filter("code", device)
    obj = q.get_sobject()
    if obj:
        state = json.loads(obj.get_value("state") or "{}")
        state["last_backup"] = {
            "target_device": target_device,
            "device_name": req["device_name"],
            "backup_id": backup_id,
            "sha256": req["sha256"],
            "size": req["size"],
            "games_count": req["games_count"],
            "created_at": now,
        }
        obj.set_value("state", json.dumps(state, separators=(",", ":")))
        obj.commit(triggers="none", log_transaction=False)

    return {
        "stored": True,
        "backup_id": backup_id,
        "target_device": target_device,
        "device_name": req["device_name"],
        "created_at": now,
        "size": req["size"],
        "games_count": req["games_count"],
    }
