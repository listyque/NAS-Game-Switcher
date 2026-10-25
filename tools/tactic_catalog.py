"""Prepare and publish only ngs_launcher; never distributed with the handheld client."""

import argparse
import base64
import hashlib
import inspect
import json
import unicodedata
from pathlib import Path

from ngs import catalog
from ngs import install
from ngs.game_db import DEFAULT_KNOWN_GAMES_URL, fetch_remote_known_games, folder_store_hint, load_bundled_known_games, match_known_game, select_best_executable
from ngs.metadata import steam_metadata, metadata_fetch, metadata_artwork, metadata_reference, search_title, search_queries, epic_metadata, gog_metadata, lutris_metadata, wikipedia_metadata, game_metadata
from ngs.tactic import TacticClient
from ngs.portable import DEFAULT_TACTIC_URL, path_key
from tools.tactic_project import DEFAULT_CREDENTIALS, admin_client, admin_password


IMPORT_FIELDS = ("name", "subtitle", "genre", "accent", "description", "source_folder", "executable", "metadata_url")


def folder_key(value):
    return unicodedata.normalize("NFC", value).casefold()


def record_token(record):
    return hashlib.sha256(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf8")).hexdigest()


def merge_import(current, proposed, state, edits):
    if set(edits) - set(IMPORT_FIELDS):
        raise ValueError("Unknown manually edited field")
    baseline = dict(state.get("baseline", {}))
    locked = set(state.get("manual_fields", []))
    merged, preserved = {}, []
    for key in IMPORT_FIELDS:
        if key in edits:
            merged[key] = edits[key]
            locked.add(key)
        elif key in ("source_folder", "executable"):
            merged[key] = proposed[key]  # Both paths are explicitly confirmed by the operator.
            baseline[key] = proposed[key]
        elif current is not None and (key in locked or key not in baseline or current[key] != baseline[key]):
            merged[key] = current[key]
            preserved.append(key)
        else:
            merged[key] = proposed[key]
            baseline[key] = proposed[key]
    return merged, {"schema": 1, "baseline": baseline, "manual_fields": sorted(locked)}, preserved


def catalog_action(action, payload, contract, service=False):
    import base64
    import hashlib
    import io
    import json
    import os
    import tempfile
    import uuid
    import re
    from pathlib import Path
    from pyasm.biz import Project, Schema, Snapshot
    from pyasm.common import Environment, Container
    from pyasm.search import Search, SearchType, DbContainer, WidgetDbConfig
    from tactic_client_lib import TacticServerStub

    project = Project.get()
    metadata_request = action in ("metadata_search", "metadata_resolve")
    security = Environment.get_security()
    service_allowed = (service and security.is_logged_in() and not security.is_admin()
                       and security.get_user_name() == "ngs_catalog_worker"
                       and action in ("inspect", "metadata_search", "metadata_details", "import", "manifest", "export", "activate"))
    if not metadata_request and not security.is_admin() and not service_allowed:
        raise PermissionError("Administrative preparation only; no client tickets are issued.")
    if (project.get_code() != "ngs_launcher" or project.get_database_name() != "ngs_launcher"
            or project.get_value("description") != "NGS handheld game catalog; managed from D:/code/NGS"):
        raise PermissionError("Refusing to operate outside the owned NGS project.")
    sql = DbContainer.get(project.get_project_db_resource())
    if action not in ("metadata_search", "metadata_details", "inspect"):
        sql.do_query("SELECT pg_advisory_xact_lock(1313297219)")
    owner = "NGS catalog schema v1"

    def find(search_type, field, value):
        query = Search(search_type)
        query.add_filter(field, value)
        return query.get_sobject()

    def set_values(obj, values):
        for key, value in values.items():
            obj.set_value(key, value)
        obj.commit()
        return obj

    if action == "install":
        common = {"id": "serial PRIMARY KEY", "code": "varchar(256) UNIQUE", "name": "varchar(256)",
                  "description": "text", "timestamp": "timestamp DEFAULT now()", "s_status": "varchar(32)"}
        tables = {
            "game": dict(common, subtitle="text", genre="text", accent="varchar(7)", source_folder="text",
                         executable="text", metadata_url="text", status="varchar(32) DEFAULT 'draft'",
                         cover_snapshot="varchar(256)", background_snapshot="varchar(256)",
                         revision="integer DEFAULT 0", published_data="text", import_state="text",
                         available_on_nas="boolean DEFAULT true"),
            "catalog": common,
        }
        # Check every mapping before touching the new project's schema.
        for table, columns in tables.items():
            registry = find("sthpw/search_object", "search_type", "ngs_launcher/" + table)
            existing = {r[0] for r in sql.do_query("SELECT column_name FROM information_schema.columns "
                                                 "WHERE table_schema='public' AND table_name='%s'" % table)}
            if bool(registry) != bool(existing) or (registry and (
                    registry.get_value("description") != owner or registry.get_value("table_name") != table
                    or registry.get_value("namespace") != "ngs_launcher"
                    or not existing <= set(columns) or (set(columns) - existing) - {"import_state", "available_on_nas"})):
                raise RuntimeError("Conflicting or incomplete NGS schema: " + table)
        for table, columns in tables.items():
            if not find("sthpw/search_object", "search_type", "ngs_launcher/" + table):
                registry = SearchType(SearchType.SEARCH_TYPE)
                set_values(registry, {"search_type": "ngs_launcher/" + table, "namespace": "ngs_launcher",
                    "database": "{project}", "table_name": table, "schema": "public",
                    "class_name": "pyasm.search.SObject", "title": "Games" if table == "game" else "Published catalog",
                    "description": owner})
                sql.do_update('CREATE TABLE "%s" (%s)' % (table, ",".join('"%s" %s' % item for item in columns.items())))
                # Native table discovery may already have cached this project's table list.
                cached = Container.get("DatabaseImpl:table_info") or {}
                cached.pop(str(project.get_project_db_resource()), None)
            else:
                existing = {r[0] for r in sql.do_query("SELECT column_name FROM information_schema.columns "
                                                     "WHERE table_schema='public' AND table_name='%s'" % table)}
                for column in sorted(set(columns) - existing):
                    sql.do_update('ALTER TABLE "%s" ADD COLUMN "%s" %s' % (table, column, columns[column]))
            schema = Schema.get_by_code("ngs_launcher")
            if not schema:
                schema = Schema.create("ngs_launcher", owner)
            schema.add_search_type("ngs_launcher/" + table)
            schema.commit()
        for view in ("table", "edit"):
            query = Search("config/widget_config")
            query.add_filter("search_type", "ngs_launcher/game")
            query.add_filter("view", view)
            existing_view = query.get_sobject()
            if not existing_view:
                fields = ["code", "name", "subtitle", "genre", "description", "source_folder", "executable",
                          "status", "available_on_nas", "revision", "cover_snapshot", "background_snapshot", "metadata_url"]
                xml = "<config><%s>%s</%s></config>" % (view, "".join('<element name="%s"/>' % f for f in fields), view)
                WidgetDbConfig.create("ngs_launcher/game", view, xml)
            else:
                from xml.etree import ElementTree
                config = ElementTree.fromstring(existing_view.get_value("config"))
                if not any(element.get("name") == "available_on_nas" for element in config.iter("element")):
                    WidgetDbConfig.append("ngs_launcher/game", view, "available_on_nas")
        for view in ("definition", "project_view"):
            query = Search("config/widget_config")
            query.add_filter("search_type", "SideBarWdg")
            query.add_filter("view", view)
            existing = query.get_sobject()
            if existing and 'name="ngs_games"' in existing.get_value("config"):
                continue
            options = {"class_name": "tactic.ui.panel.ViewPanelWdg", "search_type": "ngs_launcher/game",
                       "view": "table", "layout": "default"}
            if view == "definition":
                WidgetDbConfig.append("SideBarWdg", view, "ngs_games", class_name="LinkWdg",
                                      display_options=options, element_attrs={"title": "NGS · Games"})
            else:
                WidgetDbConfig.append("SideBarWdg", view, "ngs_games")
        for suffix, level in (("readers", "view"), ("cataloguers", "insert")):
            name = "ngs_launcher_" + suffix
            group = find("sthpw/login_group", "login_group", name)
            rules = ('<rules><rule group="project" code="*" access="deny"/>'
                     '<rule group="project" code="ngs_launcher" access="allow"/>'
                     '<rule group="search_type" default="deny"/>'
                     '<rule group="search_type" code="ngs_launcher/game" access="%s"/>'
                     '<rule group="sobject" default="deny"/>'
                     '<rule group="sobject" search_type="ngs_launcher/game" project="ngs_launcher" access="%s"/>'
                     '<rule group="api_method" default="deny"/>'
                     '<rule group="api_cmd" default="deny"/></rules>') % (level, level)
            if group:
                if group.get_value("project_code") != "ngs_launcher" or group.get_value("description") != owner:
                    raise RuntimeError("Group name is already owned by another application.")
                if group.get_value("access_rules") != rules:
                    raise RuntimeError("NGS group rules have changed; refusing to overwrite.")
            else:
                set_values(SearchType.create("sthpw/login_group"), {"code": name, "login_group": name,
                    "project_code": "ngs_launcher", "description": owner, "access_level": "none",
                    "is_default": False, "access_rules": rules})
        if not find("ngs_launcher/catalog", "code", "catalog"):
            set_values(SearchType.create("ngs_launcher/catalog"), {"code": "catalog", "name": "NGS published catalog"})
        return {"schema": "ready", "project": "ngs_launcher", "groups": "prepared_without_accounts",
                "client_access": "published_static_feed", "writer_access": "admin_preparation_only"}

    namespace = {}
    exec(contract, namespace)
    server = TacticServerStub.get(protocol="local")
    assets = Path(Environment.get_asset_dir()).resolve()
    own_assets = assets / "ngs_launcher"
    if not own_assets.resolve().is_relative_to(assets):
        raise RuntimeError("Redirected NGS asset root.")

    def checkin(obj, context, data, extension):
        upload = Path(Environment.get_upload_dir())
        upload.mkdir(parents=True, exist_ok=True)
        name = "ngs-" + uuid.uuid4().hex + extension
        stage = upload / name
        try:
            stage.write_bytes(data)
            if service:
                from pyasm.checkin import FileCheckin
                from pyasm.command import Command
                # NGS references immutable versions; the worker never deletes versionless file records.
                checkin = FileCheckin(sobject=obj, file_paths=[str(stage)], context=context, description="",
                                     mode="copy", keep_file_name=False, do_update_versionless=False)
                Command.execute_cmd(checkin)
                snapshot = checkin.get_snapshot()
            else:
                result = server.simple_checkin(obj.get_search_key(), context, name, mode="uploaded",
                                               create_icon=False, keep_file_name=False)
                snapshot = Snapshot.get_by_code(result["code"])
            path = Path(snapshot.get_lib_path_by_type("main")).resolve()
            if not path.is_relative_to(own_assets.resolve()):
                raise RuntimeError("Native checkin escaped the NGS asset directory.")
            return {"path": "/assets/" + path.relative_to(assets).as_posix(),
                    "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}, snapshot.get_code()
        finally:
            if stage.exists():
                stage.unlink()

    fields = namespace["IMPORT_FIELDS"]

    def record(obj):
        if not obj:
            return None
        result = {"card": dict({key: obj.get_value(key) or "" for key in fields}, id=obj.get_code()),
                  "available_on_nas": obj.get_value("available_on_nas") is not False,
                  "status": obj.get_value("status"), "revision": int(obj.get_value("revision") or 0),
                  "artwork": {key: obj.get_value(key + "_snapshot") or "" for key in ("cover", "background")},
                  "import_state": json.loads(obj.get_value("import_state") or "{}"),
                  "published": json.loads(obj.get_value("published_data") or "{}")}
        result["card"]["source_root"] = result["published"].get("source_root", result["import_state"].get("source_root", ""))
        for key in ("kind", "game_id"):
            if key in result["published"] or key in result["import_state"]:
                result["card"][key] = result["published"].get(key, result["import_state"].get(key))
        result["token"] = namespace["record_token"](result)
        return result

    def provider_ids(obj):
        state = json.loads(obj.get_value("import_state") or "{}")
        result = set(state.get("provider_ids", []))
        if state.get("provider_id"):
            result.add(state["provider_id"])
        match = re.search(r"^https://store\.steampowered\.com/app/([0-9]+)(?:/|$)", obj.get_value("metadata_url") or "")
        if match: result.add("steam:" + match.group(1))
        match = re.fullmatch(r"https://store\.epicgames\.com/(?:[a-z]{2}(?:-[A-Z]{2})?/)?p/([a-z0-9-]+)/?", obj.get_value("metadata_url") or "")
        if match: result.add("epic:" + match.group(1))
        match = re.fullmatch(r"https://lutris\.net/games/([a-z0-9](?:[a-z0-9-]{0,70}[a-z0-9])?)/?", obj.get_value("metadata_url") or "")
        if match: result.add("lutris:" + match.group(1))
        match = re.fullmatch(r"https://(en|ru)\.wikipedia\.org/\?curid=([1-9][0-9]{0,11})", obj.get_value("metadata_url") or "")
        if match: result.add("wikipedia:" + match.group(1) + ":" + match.group(2))
        return result

    if action == "inspect":
        return {"project": "ngs_launcher", "records": [record(obj) for obj in Search("ngs_launcher/game").get_sobjects()]}

    if action == "metadata_search":
        return namespace["game_metadata"]("search", payload.get("value"))
    if action == "metadata_details":
        return namespace["game_metadata"]("details", payload.get("value"))

    if action == "metadata_resolve":
        app_id = str(payload.get("value", ""))
        provider = namespace["metadata_reference"](app_id)
        matches = [obj for obj in Search("ngs_launcher/game").get_sobjects()
                   if provider in provider_ids(obj) and record(obj)["card"].get("kind") != "installer"]
        if len(matches) > 1:
            raise ValueError("Duplicate provider bindings in NGS; resolve them before import")
        obj = matches[0] if matches else None
        if obj and obj.get_value("published_data"):
            return {"status": "cached", "card": json.loads(obj.get_value("published_data"))}
        if obj and obj.get_value("source_folder"):
            raise ValueError("This game is already being prepared for NAS publication. Finish it in the catalog manager.")
        details = namespace["game_metadata"]("details", app_id)
        proposed = details.pop("card")
        if obj:
            proposed["id"] = obj.get_code()
        payload = dict(details, card=proposed, expected=record(obj)["token"] if obj else None,
                       edits={}, confirmed=True, available_on_nas=False)
        action = "import"

    if action == "import":
        if payload.get("confirmed") is not True:
            raise ValueError("Game and executable confirmation is required")
        proposed = payload["card"]
        identity = namespace["game_id"](proposed["id"])
        obj = find("ngs_launcher/game", "code", identity)
        current = record(obj)
        if payload.get("expected") != (current["token"] if current else None):
            raise ValueError("The entry changed in TACTIC. Prepare and confirm it again.")
        provider = payload.get("provider_id", "manual")
        if current and current["card"].get("kind") != proposed.get("kind"):
            raise ValueError("The type of an existing entry cannot be changed.")
        if (current and proposed.get("kind") == "installer"
                and current["card"].get("game_id") != proposed.get("game_id")):
            raise ValueError("This installer entry belongs to another game. Create a separate entry.")
        if provider != "manual" and (not isinstance(provider, str) or namespace["metadata_reference"](
                provider[6:] if provider.startswith("steam:") else provider) != provider):
            raise ValueError("Invalid metadata provider binding")
        available = payload.get("available_on_nas", True)
        if type(available) is not bool or (metadata_request and available):
            raise ValueError("Metadata requests cannot declare NAS availability")
        for other in Search("ngs_launcher/game").get_sobjects():
            if other.get_code() == identity:
                continue
            other_card = record(other)["card"]
            if other_card.get("kind") != proposed.get("kind"):
                continue
            other_root = other_card.get("source_root") or payload.get("legacy_source", "")
            proposed_root = proposed.get("source_root") or payload.get("legacy_source", "")
            if ((proposed.get("kind") != "installer" and proposed["source_folder"] and namespace["path_key"](other_root) == namespace["path_key"](proposed_root)
                 and namespace["folder_key"](other.get_value("source_folder") or "") == namespace["folder_key"](proposed["source_folder"]))
                    or (proposed.get("kind") == "installer" and proposed.get("game_id") and other_card.get("game_id") == proposed["game_id"])
                    or (provider != "manual" and provider in provider_ids(other))):
                raise ValueError("This folder or game is already linked to another NGS entry.")
        merged, state, preserved = namespace["merge_import"](current["card"] if current else None, proposed,
                                      current["import_state"] if current else {}, payload.get("edits", {}))
        state["provider_id"] = provider
        aliases = {p.split(":")[0]: p for p in provider_ids(obj) if ":" in p} if obj else {}
        if provider != "manual": aliases[provider.split(":")[0]] = provider
        state["provider_ids"] = sorted(aliases.values())
        state["source_root"] = proposed.get("source_root", "")
        for key in ("kind", "game_id"):
            if key in proposed:
                state[key] = proposed[key]
        state["artwork"] = dict(current["import_state"].get("artwork", {})) if current else {}
        for context in ("cover", "background"):
            if current and current["artwork"][context] and state["artwork"].get(context) != current["artwork"][context]:
                payload.pop(context, None)  # An untracked or manually replaced image belongs to the editor.
        payload = dict(payload, card=dict(merged, id=identity, available_on_nas=available,
                                         source_root=state["source_root"]), import_state=state)
        if "delivery" in proposed:
            payload["card"]["delivery"] = proposed["delivery"]
        if "total_size" in proposed:
            payload["card"]["total_size"] = proposed["total_size"]
        for key in ("kind", "game_id"):
            if key in proposed:
                payload["card"][key] = proposed[key]
        action = "publish"

    if action == "manifest":
        obj = find("ngs_launcher/game", "code", payload["game"])
        if not obj or obj.get_value("status") != "published":
            raise ValueError("Publish the game card before attaching its manifest.")
        if payload.get("expected") is not None and record(obj)["token"] != payload["expected"]:
            raise ValueError("The entry changed while packages were being prepared. Review it again.")
        encoded = payload["manifest"]["base64"]
        if len(encoded) > 45 * 1024 * 1024:
            raise ValueError("Oversized manifest upload.")
        data = base64.b64decode(encoded, validate=True)
        if len(data) > 32 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != payload["manifest"]["sha256"]:
            raise ValueError("Manifest upload digest or size mismatch.")
        card = json.loads(obj.get_value("published_data"))
        document = namespace["validate_manifest"](json.loads(data), card)
        card["total_size"] = document["total_size"]
        card["manifest"], snapshot_code = checkin(obj, "manifest", data, ".json")
        card["revision"] = int(obj.get_value("revision") or 0) + 1
        namespace["validate_card"](card)
        set_values(obj, {"revision": card["revision"], "published_data": json.dumps(card, ensure_ascii=False)})
        return {"status": "manifest_attached", "game": card["id"], "revision": card["revision"], "snapshot": snapshot_code, "card": card}

    if action in ("draft", "publish"):
        obj = None
        if "card" in payload:
            card = payload["card"]
        else:
            obj = find("ngs_launcher/game", "code", payload["game"])
            if not obj:
                raise ValueError("Game does not exist in NGS.")
            card = {key: obj.get_value(key) or "" for key in ("name", "subtitle", "description", "genre",
                    "accent", "source_folder", "executable", "metadata_url")}
            card["id"] = obj.get_code()
            card["available_on_nas"] = obj.get_value("available_on_nas") is not False
            card["source_root"] = record(obj)["card"]["source_root"]
            for key in ("kind", "game_id"):
                if key in record(obj)["card"]:
                    card[key] = record(obj)["card"][key]
        # Validate metadata before writing files; temporary image references are never published.
        placeholder = {"path": "/assets/ngs_launcher/validation.jpg", "sha256": "0" * 64, "size": 1}
        card = namespace["validate_card"](dict(card, revision=1, cover=placeholder, background=placeholder))
        obj = obj or find("ngs_launcher/game", "code", card["id"])
        created = obj is None
        if not obj:
            obj = SearchType.create("ngs_launcher/game")
            obj.set_value("code", card["id"])
        values = {k: card[k] for k in ("name", "subtitle", "genre", "accent", "description", "source_folder", "executable", "metadata_url")}
        values["available_on_nas"] = card.get("available_on_nas", True)
        if action == "draft":
            if not obj.get_value("published_data"):
                values["status"] = "draft"
            if "source_root" in card:
                state = json.loads(obj.get_value("import_state") or "{}")
                state["source_root"] = card["source_root"]
                values["import_state"] = json.dumps(state, ensure_ascii=False)
            set_values(obj, values)
            return {"status": "draft", "game": card["id"]}
        card.pop("manifest", None)
        previous = json.loads(obj.get_value("published_data") or "{}")
        if ("total_size" not in card and "total_size" in previous
                and previous.get("source_root", "") == card.get("source_root", "")
                and all(previous.get(key) == card[key] for key in ("id", "source_folder", "executable"))):
            card["total_size"] = previous["total_size"]
        if (previous.get("delivery") == "on_request" and "delivery" not in card
                and previous.get("source_root", "") == card.get("source_root", "")
                and all(previous.get(key) == card[key] for key in ("id", "source_folder", "executable"))):
            card["delivery"] = "on_request"
        if (previous.get("manifest") and previous.get("source_root", "") == card.get("source_root", "")
                and all(previous.get(key) == card[key] for key in ("id", "source_folder", "executable"))):
            card["manifest"] = previous["manifest"]
        from PIL import Image
        for context in ("cover", "background"):
            if context not in payload:
                previous = json.loads(obj.get_value("published_data") or "{}")
                snapshot = Snapshot.get_by_code(obj.get_value(context + "_snapshot"))
                if (not snapshot or snapshot.get_value("project_code") != "ngs_launcher"
                        or snapshot.get_value("search_type").split("?")[0] != "ngs_launcher/game"
                        or snapshot.get_value("search_code") != card["id"] or snapshot.get_value("context") != context):
                    raise ValueError("Upload is required for " + context)
                path = Path(snapshot.get_lib_path_by_type("main")).resolve()
                if not path.is_relative_to(own_assets.resolve()) or not 0 < path.stat().st_size <= 8 * 1024 * 1024:
                    raise ValueError("Previous artwork snapshot is unavailable.")
                data = path.read_bytes()
                card[context] = {"path": "/assets/" + path.relative_to(assets).as_posix(), "size": len(data),
                                 "sha256": hashlib.sha256(data).hexdigest()}
                continue
            image = payload[context]
            if len(image["base64"]) > 12 * 1024 * 1024:
                raise ValueError("Oversized image upload.")
            data = base64.b64decode(image["base64"], validate=True)
            if len(data) > 8 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != image["sha256"]:
                raise ValueError("Invalid image upload digest or size.")
            picture = Image.open(io.BytesIO(data))
            if (picture.format not in ("PNG", "JPEG", "WEBP") or max(picture.size) > 8192
                    or picture.width * picture.height > 16_000_000):
                raise ValueError("Invalid raster image.")
            picture.verify()
            image["bytes"] = data
            image["extension"] = ".jpg" if picture.format == "JPEG" else ".png" if picture.format == "PNG" else ".webp"
        manifest = payload.get("manifest")
        if manifest:
            if len(manifest["base64"]) > 45 * 1024 * 1024:
                raise ValueError("Oversized manifest")
            data = base64.b64decode(manifest["base64"], validate=True)
            if len(data) > 32 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != manifest["sha256"]:
                raise ValueError("Invalid manifest upload")
            document = namespace["validate_manifest"](json.loads(data), card)
            card["total_size"] = document["total_size"]
            manifest["bytes"] = data
        # Compare content before check-in: repeated import must not make new snapshots or revisions.
        same_images = all((previous.get(context, {}).get("sha256") == payload[context]["sha256"]
                           if context in payload else previous.get(context) == card[context])
                          for context in ("cover", "background"))
        same_manifest = (previous.get("manifest", {}).get("sha256") == manifest["sha256"] if manifest
                         else previous.get("manifest") == card.get("manifest"))
        same_fields = all(obj.get_value(k) == v and previous.get(k, True if k == "available_on_nas" else None) == v
                          for k, v in values.items())
        same_fields = same_fields and previous.get("source_root", "") == card.get("source_root", "")
        same_fields = same_fields and previous.get("delivery") == card.get("delivery")
        same_fields = same_fields and previous.get("total_size") == card.get("total_size")
        same_fields = same_fields and all(previous.get(k) == card.get(k) for k in ("kind", "game_id"))
        if obj.get_value("published_data") and same_images and same_manifest and same_fields:
            if "import_state" in payload:
                state = payload["import_state"]
                encoded = json.dumps(state, ensure_ascii=False, sort_keys=True)
                if encoded != obj.get_value("import_state"):
                    set_values(obj, {"import_state": encoded})
            return {"status": "unchanged", "game": card["id"], "revision": int(obj.get_value("revision")), "card": previous}
        if created:
            pending_values = dict(values, code=card["id"], status="draft")
            if "import_state" in payload:
                pending_values["import_state"] = json.dumps(payload["import_state"], ensure_ascii=False, sort_keys=True)
            set_values(obj, pending_values)
        for context in ("cover", "background"):
            if context not in payload:
                continue
            image = payload[context]
            if previous.get(context, {}).get("sha256") == image["sha256"]:
                card[context] = previous[context]
            else:
                card[context], values[context + "_snapshot"] = checkin(obj, context, image["bytes"], image["extension"])
        if manifest and previous.get("manifest", {}).get("sha256") != manifest["sha256"]:
            card["manifest"], _ = checkin(obj, "manifest", manifest["bytes"], ".json")
        elif manifest:
            card["manifest"] = previous["manifest"]
        if "import_state" in payload:
            state = payload["import_state"]
            for context in ("cover", "background"):
                if context in payload:
                    state["artwork"][context] = values.get(context + "_snapshot") or obj.get_value(context + "_snapshot")
            values["import_state"] = json.dumps(state, ensure_ascii=False, sort_keys=True)
        card["revision"] = int(obj.get_value("revision") or 0) + 1
        namespace["validate_card"](card)
        set_values(obj, dict(values, revision=card["revision"], status="published", published_data=json.dumps(card, ensure_ascii=False)))
        return {"status": "published", "game": card["id"], "revision": card["revision"], "card": card}

    if action == "export":
        if payload.get("kind") not in (None, "installer"):
            raise ValueError("Invalid catalog kind")
        catalog_code = "installers" if payload.get("kind") == "installer" else "catalog"
        query = Search("ngs_launcher/game")
        query.add_filter("status", "published")
        query.add_filter("available_on_nas", True)
        query.add_order_by("name")
        cards = [json.loads(obj.get_value("published_data")) for obj in query.get_sobjects()]
        cards = [card for card in cards if card.get("kind") == payload.get("kind")]
        feed = namespace["validate_feed"]({"schema": 1, "project": "ngs_launcher", "games": cards})
        data = json.dumps(feed, ensure_ascii=False).encode("utf8")
        if len(data) > 2 * 1024 * 1024:
            raise ValueError("Catalog exceeds client limit.")
        obj = find("ngs_launcher/catalog", "code", catalog_code)
        if not obj and catalog_code == "installers":
            obj = SearchType.create("ngs_launcher/catalog")
            set_values(obj, dict(code=catalog_code, name="NGS installers catalog"))
        reference, code = checkin(obj, "catalog", data, ".json")
        return {"snapshot": code, "sha256": reference["sha256"], "games": len(cards)}

    if action == "activate":
        if payload.get("kind") not in (None, "installer"):
            raise ValueError("Invalid catalog kind")
        catalog_code = "installers" if payload.get("kind") == "installer" else "catalog"
        # A second request sees only committed snapshots; the old feed survives a failed checkin.
        obj = find("ngs_launcher/catalog", "code", catalog_code)
        snapshot = Snapshot.get_by_code(payload["snapshot"])
        if (not snapshot or snapshot.get_value("project_code") != "ngs_launcher"
                or snapshot.get_value("search_type").split("?")[0] != "ngs_launcher/catalog"
                or snapshot.get_value("search_code") != catalog_code or snapshot.get_value("context") != "catalog"):
            raise ValueError("Snapshot is not an NGS catalog.")
        latest = Snapshot.get_latest_by_sobject(obj, context="catalog")
        if latest.get_code() != snapshot.get_code():
            raise ValueError("A newer export exists; refusing an old activation.")
        source = Path(snapshot.get_lib_path_by_type("main")).resolve()
        if not source.is_relative_to(own_assets.resolve()):
            raise ValueError("Invalid snapshot location.")
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != payload["sha256"]:
            raise ValueError("Export digest mismatch.")
        feed = namespace["validate_feed"](json.loads(data))
        if any(card.get("kind") != payload.get("kind") for card in feed["games"]):
            raise ValueError("Export contains another catalog kind")
        target = own_assets / (catalog_code + ".json")
        if target.is_symlink() or not target.resolve().is_relative_to(own_assets.resolve()):
            raise ValueError("Redirected catalog pointer.")
        stage = None
        try:
            with tempfile.NamedTemporaryFile(dir=own_assets, prefix=".catalog-", delete=False) as stream:
                stage = stream.name
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(stage, target)
        finally:
            if stage and os.path.exists(stage):
                os.unlink(stage)
        return {"status": "active", "snapshot": snapshot.get_code(), "games": len(json.loads(data)["games"])}
    raise ValueError("Unsupported NGS catalog action.")


def contract_source():
    from ngs.packages import validate_transport, PACKAGE_SIZE, PACKAGE_PREFIX
    return ("import re, json, hashlib, unicodedata\nfrom pathlib import PurePosixPath, PureWindowsPath\nfrom urllib.parse import urlsplit, unquote\n"
            + "IMPORT_FIELDS = " + repr(IMPORT_FIELDS) + "\n"
            + "DEFAULT_KNOWN_GAMES_URL = " + repr(DEFAULT_KNOWN_GAMES_URL) + "\n"
            + "PACKAGE_SIZE = " + repr(PACKAGE_SIZE) + "\nPACKAGE_PREFIX = " + repr(PACKAGE_PREFIX) + "\n"
            + "MAX_IMAGE = 8 * 1024 * 1024\nRECEIPT = '.ngs-install.json'\n" + "\n".join(inspect.getsource(function) for function in (
                folder_key, path_key, record_token, merge_import, metadata_reference, metadata_fetch, metadata_artwork,
                search_title, search_queries, steam_metadata, epic_metadata, gog_metadata, lutris_metadata, wikipedia_metadata, game_metadata,
                catalog.relative_game_path, catalog.asset_path, catalog.validate_card, catalog.validate_feed,
                install.game_id, install.is_link, install.safe_path,
                folder_store_hint, select_best_executable, load_bundled_known_games, match_known_game, fetch_remote_known_games,
                validate_transport, install.validate_manifest)))


def invoke(client, action, payload=None):
    return client.script(inspect.getsource(catalog_action) + "\nreturn catalog_action(action, payload, contract)",
                         timeout=90, action=action, payload=payload or {}, contract=contract_source())


def install_resolver(client):
    from tools.server_code import packed_script
    script = packed_script(inspect.getsource(catalog_action),
              "catalog_action('metadata_' + operation, {'value': value}, ngs_compile(" + repr(contract_source()) + "))", "operation,value")
    return client.script('''
from pyasm.biz import Project
from pyasm.common import Environment
from pyasm.search import Search, SearchType
if not Environment.get_security().is_admin() or Project.get().get_code() != 'ngs_launcher':
    raise PermissionError('NGS preparation only')
q=Search('config/custom_script')
q.add_filter('folder','ngs')
q.add_filter('title','metadata')
obj=q.get_sobject()
owner='NGS metadata resolver v1; managed from D:/code/NGS'
if obj and obj.get_value('description') != owner:
    raise ValueError('Script belongs to another application')
if not obj: obj=SearchType.create('config/custom_script')
for k,v in dict(folder='ngs',title='metadata',description=owner,language='python',script=source).items():
    obj.set_value(k,v)
obj.commit()
return {'script':'ngs/metadata','project':'ngs_launcher','authentication':'required'}
''', source=script)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("install", "draft", "publish", "manifest", "export"))
    parser.add_argument("--base-url", default=DEFAULT_TACTIC_URL)
    parser.add_argument("--credentials-env", type=Path, default=DEFAULT_CREDENTIALS)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--card", type=Path, help="Import initial metadata from a local document")
    source.add_argument("--game", help="Publish the current fields of an existing NGS game")
    parser.add_argument("--cover", type=Path)
    parser.add_argument("--background", type=Path)
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    payload = {}
    if args.command == "manifest":
        if not args.game or not args.manifest or not 0 < args.manifest.stat().st_size <= install.MAX_MANIFEST:
            parser.error("--game and --manifest (under 32 MiB) are required")
        data = args.manifest.read_bytes()
        document = install.validate_manifest(json.loads(data))
        if document["game_id"] != args.game:
            parser.error("Manifest belongs to another game")
        payload = {"game": args.game, "manifest": {"base64": base64.b64encode(data).decode("ascii"),
                                                  "sha256": hashlib.sha256(data).hexdigest()}}
    if args.command in ("draft", "publish"):
        if args.card:
            card = json.loads(args.card.read_text(encoding="utf-8"))
            placeholder = {"path": "/assets/ngs_launcher/check.jpg", "sha256": "0" * 64, "size": 1}
            catalog.validate_card(dict(card, revision=1, cover=placeholder, background=placeholder))
            payload["card"] = card
        elif args.game:
            import re
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", args.game):
                parser.error("Invalid NGS game code")
            payload["game"] = args.game
        else:
            parser.error("--card or --game is required")
        if args.command == "publish":
            for context in ("cover", "background"):
                path = getattr(args, context)
                if not path:
                    continue
                if not 0 < path.stat().st_size <= catalog.MAX_IMAGE:
                    parser.error("Artwork must be a raster image under 8 MiB")
                data = path.read_bytes()
                payload[context] = {"base64": base64.b64encode(data).decode("ascii"), "sha256": hashlib.sha256(data).hexdigest()}
    client = admin_client(args.base_url, args.credentials_env)
    if args.command != "export":
        print(json.dumps(invoke(client, args.command, payload), ensure_ascii=False))
    if args.command in ("draft", "publish", "manifest", "export"):
        exported = invoke(client, "export")
        print(json.dumps(invoke(client, "activate", exported), ensure_ascii=False))


if __name__ == "__main__":
    main()
