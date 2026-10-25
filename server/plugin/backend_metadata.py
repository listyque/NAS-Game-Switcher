def ngs_compile(source):
    import functools, sys, types
    name = "_ngs_launcher_compiled_code"
    module = sys.modules.get(name)
    if module is None:
        candidate = types.ModuleType(name)
        candidate.compile = functools.lru_cache(maxsize=16)(compile)
        module = sys.modules.setdefault(name, candidate)
    # Cache only code, never request data, credentials, security objects or query results.
    return module.compile(source, "<ngs_launcher>", "exec")

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

def ngs_entry(operation,value):
    return catalog_action('metadata_' + operation, {'value': value}, ngs_compile("import re, json, hashlib, unicodedata\nfrom pathlib import PurePosixPath, PureWindowsPath\nfrom urllib.parse import urlsplit, unquote\nIMPORT_FIELDS = ('name', 'subtitle', 'genre', 'accent', 'description', 'source_folder', 'executable', 'metadata_url')\nDEFAULT_KNOWN_GAMES_URL = 'https://raw.githubusercontent.com/listy/ngs-known-games/main/known-games.json'\nPACKAGE_SIZE = 268435456\nPACKAGE_PREFIX = '/assets/ngs_launcher/packages/'\nMAX_IMAGE = 8 * 1024 * 1024\nRECEIPT = '.ngs-install.json'\ndef folder_key(value):\n    return unicodedata.normalize(\"NFC\", value).casefold()\n\ndef path_key(value):\n    import ntpath, unicodedata\n    return unicodedata.normalize(\"NFC\", ntpath.normpath(str(value))).casefold()\n\ndef record_token(record):\n    return hashlib.sha256(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(\",\", \":\")).encode(\"utf8\")).hexdigest()\n\ndef merge_import(current, proposed, state, edits):\n    if set(edits) - set(IMPORT_FIELDS):\n        raise ValueError(\"Unknown manually edited field\")\n    baseline = dict(state.get(\"baseline\", {}))\n    locked = set(state.get(\"manual_fields\", []))\n    merged, preserved = {}, []\n    for key in IMPORT_FIELDS:\n        if key in edits:\n            merged[key] = edits[key]\n            locked.add(key)\n        elif key in (\"source_folder\", \"executable\"):\n            merged[key] = proposed[key]  # Both paths are explicitly confirmed by the operator.\n            baseline[key] = proposed[key]\n        elif current is not None and (key in locked or key not in baseline or current[key] != baseline[key]):\n            merged[key] = current[key]\n            preserved.append(key)\n        else:\n            merged[key] = proposed[key]\n            baseline[key] = proposed[key]\n    return merged, {\"schema\": 1, \"baseline\": baseline, \"manual_fields\": sorted(locked)}, preserved\n\ndef metadata_reference(value):\n    import re\n    if isinstance(value, str) and re.fullmatch(r\"[1-9][0-9]{0,9}\", value):\n        return \"steam:\" + value\n    if isinstance(value, str) and re.fullmatch(r\"epic:[a-z0-9][a-z0-9-]{0,68}[a-z0-9]\", value):\n        return value\n    if isinstance(value, str) and re.fullmatch(r\"gog:[1-9][0-9]{0,19}\", value):\n        return value\n    if isinstance(value, str) and re.fullmatch(r\"lutris:[a-z0-9](?:[a-z0-9-]{0,70}[a-z0-9])?\", value):\n        return value\n    if isinstance(value, str) and re.fullmatch(r\"wikipedia:(?:en|ru):[1-9][0-9]{0,11}\", value):\n        return value\n    raise ValueError(\"Invalid game reference\")\n\ndef metadata_fetch(url, limit, missing_ok=False, timeout=12):\n    import sys, time, types, threading\n    from collections import OrderedDict\n    import urllib.error\n    import urllib.request\n    from urllib.parse import urlsplit\n    parsed = urlsplit(url)\n    if (parsed.scheme != \"https\" or parsed.username or parsed.password or parsed.port not in (None, 443)\n            or parsed.fragment or parsed.hostname not in (\"store.steampowered.com\", \"shared.akamai.steamstatic.com\",\n                \"cdn.akamai.steamstatic.com\", \"shared.fastly.steamstatic.com\", \"cdn.cloudflare.steamstatic.com\",\n                \"store-content.ak.epicgames.com\", \"cdn2.unrealengine.com\", \"cdn1.epicgames.com\",\n                \"catalog.gog.com\", \"api.gog.com\", \"images.gog-statics.com\", \"images-1.gog-statics.com\",\n                \"images-2.gog-statics.com\", \"images-3.gog-statics.com\", \"images-4.gog-statics.com\",\n                \"lutris.net\", \"images.igdb.com\", \"en.wikipedia.org\", \"ru.wikipedia.org\", \"upload.wikimedia.org\")):\n        raise ValueError(\"Untrusted metadata URL\")\n    name = \"_ngs_launcher_provider_cache\"\n    cache = sys.modules.get(name)\n    if cache is None:\n        candidate = types.ModuleType(name)\n        candidate.items, candidate.lock = OrderedDict(), threading.RLock()\n        cache = sys.modules.setdefault(name, candidate)\n    with cache.lock:\n        now = time.monotonic()\n        for key, (saved, data) in list(cache.items.items()):\n            if now - saved >= 300: del cache.items[key]\n        if url in cache.items:\n            saved, data = cache.items[url]\n            if len(data) > limit: raise ValueError(\"Metadata response exceeds size limit\")\n            cache.items.move_to_end(url)\n            return data\n    class NoRedirect(urllib.request.HTTPRedirectHandler):\n        def redirect_request(self, req, fp, code, msg, headers, newurl):\n            raise ValueError(\"Metadata redirects are disabled\")\n    request = urllib.request.Request(url, headers={\"User-Agent\": \"NGS-Catalogizer/1.0\", \"Accept\": \"application/json,image/*\"})\n    for attempt in range(2):\n        try:\n            with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout) as response:\n                data = response.read(limit + 1)\n                if len(data) > limit:\n                    raise ValueError(\"Metadata response exceeds size limit\")\n                # Public provider bytes only; API authentication and catalog records stay uncached.\n                with cache.lock:\n                    cache.items[url] = (time.monotonic(), data)\n                    cache.items.move_to_end(url)\n                    while len(cache.items) > 32 or sum(len(item[1]) for item in cache.items.values()) > 16 * 1024 * 1024:\n                        cache.items.popitem(last=False)\n                return data\n        except urllib.error.HTTPError as error:\n            code = error.code\n            error.close()\n            if missing_ok and code == 404:\n                return b\"{}\"\n            raise RuntimeError(\"Metadata HTTP %s\" % code) from None\n        except urllib.error.URLError as error:\n            # A fresh connection can recover the NAS's transient unavailable-address error.\n            if attempt or getattr(error.reason, \"errno\", None) not in (99, 101, 10049, 10051):\n                raise\n\ndef metadata_artwork(sources):\n    import base64, hashlib\n    from concurrent.futures import ThreadPoolExecutor\n    sources = {context: tuple(dict.fromkeys(url for url in urls if url)) for context, urls in sources.items()}\n    if any(not urls for urls in sources.values()):\n        raise RuntimeError(\"The provider returned no game images.\")\n    def download(urls):\n        for index, url in enumerate(urls):\n            try:\n                data = metadata_fetch(url, 8 * 1024 * 1024, timeout=4 if index + 1 < len(urls) else 12)\n                return {\"base64\": base64.b64encode(data).decode(\"ascii\"), \"sha256\": hashlib.sha256(data).hexdigest()}\n            except (RuntimeError, OSError):\n                continue\n        raise RuntimeError(\"The artwork provider did not return an image. Try downloading again.\")\n    # Only network reads run in these threads; TACTIC writes stay on its request thread.\n    unique = list(dict.fromkeys(sources.values()))\n    with ThreadPoolExecutor(max_workers=2) as pool:\n        images = dict(zip(unique, pool.map(download, unique)))\n    return {context: images[urls] for context, urls in sources.items()}\n\ndef search_title(value):\n    import re\n    value = re.sub(r\"(?i)^\\s*(?:\\[(?:dixen18|xatab|fitgirl|dodi|elamigos|dl|p|l|gog|steam|repack|portable)\\]\\s*)+\", \"\", value)\n    value = re.sub(r\"(?i)(?:\\s*\\[(?:p|l|dl|rus|eng|multi\\d*|repack|portable|gog|steam)[^\\]]*\\])+\\s*$\", \"\", value)\n    value = re.sub(r\"(?i)(?:\\s*\\((?:rus|eng|multi\\d*|repack|portable|gog|steam|\\d{4,6})[^)]*\\))+\\s*$\", \"\", value)\n    value = value.replace(\"_\", \" \")\n    value = re.sub(r\"(?<=[a-z])(?=[A-Z][a-z]|RTX\\b)\", \" \", value)\n    value = re.sub(r\"(?<!\\d)\\.|\\.(?!\\d)\", \" \", value)\n    value = re.sub(r\"(?i)[ -]+(?:win[ -]+)?gog\\b.*$\", \"\", value)\n    value = re.sub(r\"(?i)[ -]+(?:by\\s+)?(?:insane\\s*ram\\s*zes|fitgirl|dodi|elamigos|xatab|dixen18|rune|codex|tenoke|flt|kaos|rg\\s+mechanics|chovka|selezen|wanterlude)\\b.*$\", \"\", value)\n    value = re.split(r\"(?i)\\s+(?:v\\d|build\\s*\\d|repack\\b|(?:win\\s+)?gog\\b|steam\\b|rip\\b|\\d+\\.\\d{3,})\", value, maxsplit=1)[0]\n    value = re.sub(r\"\\s*\\(\\d{4}(?:-\\d{4})?\\)\\s*$\", \"\", value)\n    value = re.sub(r\"\\b(\\d)\\s+0\\b\", r\"\\1.0\", value)\n    return \" \".join(value.split())[:160]\n\ndef search_queries(value):\n    import re\n    clean = search_title(value)\n    clean = re.sub(r\"(?i)\\btmnt\\b\", \"Teenage Mutant Ninja Turtles\", clean)\n    original = clean\n    clean = re.sub(r\"[-‐‑‒–—―−﹘﹣－:/\\\\|]+\", \" \", clean)\n    clean = re.sub(r\"['’‘\\\"“”«»™®©]\", \"\", clean)\n    clean = \" \".join(clean.split())\n    base = re.sub(r\"(?i)\\s+(?:(?:digital\\s+)?(?:deluxe|ultimate|premium|gold|complete|definitive|standard|game of the year|goty)\\s*(?:edition)?|campaign only)\\b.*$\", \"\", clean)\n    remake = re.sub(r\"(?i)\\s+(?:remake|ремейк|ремэйк)\\b\", \"\", base)\n    collection = re.sub(r\"(?i)\\s+(?:collection|anthology|dilogy|trilogy|дилогия|трилогия|антология|сборник)$\", \"\", remake)\n    # Some release folders prepend \"game\"; search the full title first to keep Game Dev Tycoon intact.\n    package = re.sub(r\"(?i)^game\\s+(?=\\S)\", \"\", collection)\n    return list(dict.fromkeys(q.strip() for q in (clean, original, base, remake, collection, package) if q.strip()))\n\ndef steam_metadata(operation, value):\n    import html\n    import json\n    import re\n    from urllib.parse import urlencode\n    fetch = metadata_fetch\n\n    def clean(value, limit):\n        return \" \".join(html.unescape(re.sub(r\"<[^>]*>\", \" \", str(value or \"\"))).split())[:limit]\n\n    if operation == \"search\":\n        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 160 or any(ord(c) < 32 for c in value):\n            raise ValueError(\"The search title must contain 1 to 160 characters.\")\n        if not re.fullmatch(r\"[1-9][0-9]{0,9}\", value.strip()):\n            query = urlencode({\"term\": value, \"l\": \"russian\" if re.search(r\"[А-Яа-яЁё]\", value) else \"english\", \"cc\": \"US\"})\n            result = json.loads(fetch(\"https://store.steampowered.com/api/storesearch/?\" + query, 2 * 1024 * 1024))\n            return [{\"app_id\": str(item[\"id\"]), \"name\": clean(item[\"name\"], 160)}\n                    for item in result.get(\"items\", [])[:12] if str(item.get(\"id\", \"\")).isdigit() and item.get(\"type\", \"app\") == \"app\"]\n        value = value.strip()\n    if operation not in (\"search\", \"details\") or not re.fullmatch(r\"[1-9][0-9]{0,9}\", str(value)):\n        raise ValueError(\"Invalid Steam app ID\")\n    app_id = str(value)\n    query = urlencode({\"appids\": app_id, \"l\": \"english\" if operation == \"search\" else \"russian\", \"cc\": \"US\"})\n    result = json.loads(fetch(\"https://store.steampowered.com/api/appdetails?\" + query, 4 * 1024 * 1024)).get(app_id, {})\n    game = result.get(\"data\", {})\n    if not result.get(\"success\") or game.get(\"type\") != \"game\":\n        if operation == \"search\":\n            return []\n        raise ValueError(\"Steam did not return game details. Choose another result.\")\n    if operation == \"search\":\n        return [{\"app_id\": app_id, \"name\": clean(game.get(\"name\"), 160)}]\n    card = {\"id\": \"steam-\" + app_id, \"name\": clean(game.get(\"name\"), 160),\n            \"subtitle\": clean(game.get(\"short_description\"), 200),\n            \"description\": clean(game.get(\"about_the_game\") or game.get(\"short_description\"), 3000),\n            \"genre\": clean(\" · \".join(g[\"description\"] for g in game.get(\"genres\", [])), 100),\n            \"accent\": \"#d9ff82\", \"source_folder\": \"\", \"executable\": \"\", \"available_on_nas\": False,\n            \"metadata_url\": \"https://store.steampowered.com/app/\" + app_id + \"/\"}\n    assets = \"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/\" + app_id\n    artwork = metadata_artwork({\"cover\": [assets + \"/library_600x900.jpg\", game.get(\"header_image\")],\n                                \"background\": [assets + \"/library_hero.jpg\", game.get(\"background_raw\"), game.get(\"header_image\")]})\n    return {\"card\": card, \"provider_id\": \"steam:\" + app_id, **artwork}\n\ndef epic_metadata(operation, value):\n    import html, json, re\n    if operation not in (\"search\", \"details\"):\n        raise ValueError(\"Invalid metadata operation\")\n    reference = metadata_reference(value)\n    if not reference.startswith(\"epic:\"):\n        raise ValueError(\"Expected an Epic reference\")\n    slug = reference[5:]\n    # shortcut: Epic exposes product slugs, not a documented public search; accept an official product URL for other slugs.\n    document = json.loads(metadata_fetch(\"https://store-content.ak.epicgames.com/api/ru/content/products/\" + slug,\n                                         2 * 1024 * 1024, missing_ok=True))\n    pages = document.get(\"pages\", [])\n    page = next((p for p in pages if p.get(\"_slug\") == \"home\"), {})\n    data = page.get(\"data\", {})\n    about, hero = data.get(\"about\", {}), data.get(\"hero\", {})\n    def clean(text, limit):\n        return \" \".join(html.unescape(re.sub(r\"<[^>]*>\", \" \", str(text or \"\"))).split())[:limit]\n    name = clean(document.get(\"productName\") or page.get(\"productName\") or document.get(\"_title\", \"\").replace(\"-\", \" \"), 160)\n    if document.get(\"_slug\") != slug or not name or not about.get(\"description\"):\n        if operation == \"search\": return []\n        raise ValueError(\"Epic did not return game details. Check the title or URL.\")\n    if operation == \"search\":\n        return [{\"app_id\": reference, \"name\": name}]\n    card = dict(id=\"epic-\" + slug, name=name, subtitle=clean(about.get(\"shortDescription\"), 200),\n                description=clean(about[\"description\"], 3000), genre=\"\", accent=\"#d9ff82\",\n                source_folder=\"\", executable=\"\", available_on_nas=False,\n                metadata_url=\"https://store.epicgames.com/p/\" + slug)\n    cover = hero.get(\"portraitBackgroundImageUrl\") or about.get(\"image\", {}).get(\"src\")\n    background = hero.get(\"backgroundImageUrl\") or cover\n    artwork = metadata_artwork({\"cover\": [cover], \"background\": [background]})\n    return dict(card=card, provider_id=reference, **artwork)\n\ndef gog_metadata(operation, value):\n    import html, json, re, unicodedata\n    from urllib.parse import urlencode\n    def clean(text, limit):\n        return \" \".join(html.unescape(re.sub(r\"<[^>]*>\", \" \", str(text or \"\"))).split())[:limit]\n    def catalog(query):\n        query = \"\".join(c for c in unicodedata.normalize(\"NFKD\", query) if not unicodedata.combining(c))\n        query = \" \".join(re.findall(r\"\\w+\", query))\n        params = urlencode(dict(query=query.lower(), limit=12, countryCode=\"US\",\n                                locale=\"en-US\", currencyCode=\"USD\", order=\"desc:score\"))\n        return json.loads(metadata_fetch(\"https://catalog.gog.com/v1/catalog?\" + params, 2 * 1024 * 1024)).get(\"products\", [])\n    if operation not in (\"search\", \"details\") or not isinstance(value, str):\n        raise ValueError(\"Invalid GOG operation\")\n    if operation == \"search\" and not value.startswith(\"gog:\"):\n        return [{\"app_id\": \"gog:\" + str(p[\"id\"]), \"name\": clean(p[\"title\"], 160)} for p in catalog(value)\n                if p.get(\"productType\") in (\"game\", \"pack\") and re.fullmatch(r\"[1-9][0-9]{0,19}\", str(p.get(\"id\", \"\")))][:12]\n    reference = metadata_reference(value)\n    if not reference.startswith(\"gog:\"):\n        raise ValueError(\"Expected a GOG reference\")\n    product = json.loads(metadata_fetch(\"https://api.gog.com/products/\" + reference[4:] + \"?expand=description,screenshots\",\n                                       2 * 1024 * 1024))\n    if str(product.get(\"id\")) != reference[4:] or product.get(\"game_type\") not in (\"game\", \"pack\"):\n        raise ValueError(\"GOG did not return game details.\")\n    name = clean(product.get(\"title\"), 160)\n    if not name:\n        raise ValueError(\"GOG did not return a game title.\")\n    if operation == \"search\": return [{\"app_id\": reference, \"name\": name}]\n    art = next((p for p in catalog(name) if str(p.get(\"id\")) == reference[4:]), {})\n    description = product.get(\"description\", {})\n    text = clean(description.get(\"full\") or description.get(\"lead\"), 3000)\n    if not text:\n        raise ValueError(\"GOG did not return a game description.\")\n    slug = product.get(\"slug\", \"\")\n    if not re.fullmatch(r\"[a-z0-9_]{1,160}\", slug):\n        raise ValueError(\"Invalid GOG product slug\")\n    images = product.get(\"images\", {})\n    cover = art.get(\"coverVertical\") or images.get(\"logo2x\") or images.get(\"logo\")\n    background = art.get(\"galaxyBackgroundImage\") or images.get(\"background\") or cover\n    artwork = metadata_artwork({context: [\"https:\" + url if url and url.startswith(\"//\") else url]\n                               for context, url in ((\"cover\", cover), (\"background\", background))})\n    card = dict(id=\"gog-\" + reference[4:], name=name, subtitle=clean(description.get(\"lead\"), 200),\n                description=text, genre=clean(\" · \".join(g.get(\"name\", \"\") for g in art.get(\"genres\", [])), 100),\n                accent=\"#d9ff82\", source_folder=\"\", executable=\"\", available_on_nas=False,\n                metadata_url=\"https://www.gog.com/game/\" + slug)\n    return dict(card=card, provider_id=reference, **artwork)\n\ndef lutris_metadata(operation, value):\n    import html, json, re\n    from urllib.parse import urlencode\n    def clean(text, limit):\n        return \" \".join(html.unescape(re.sub(r\"<[^>]*>\", \" \", str(text or \"\"))).split())[:limit]\n    if operation not in (\"search\", \"details\") or not isinstance(value, str):\n        raise ValueError(\"Invalid Lutris operation\")\n    if operation == \"search\" and not value.startswith(\"lutris:\"):\n        document = json.loads(metadata_fetch(\"https://lutris.net/api/games?\" + urlencode(dict(search=value)), 2 * 1024 * 1024))\n        return [{\"app_id\": \"lutris:\" + row[\"slug\"], \"name\": clean(row.get(\"name\"), 160)}\n                for row in document.get(\"results\", [])[:12] if isinstance(row, dict)\n                and re.fullmatch(r\"[a-z0-9](?:[a-z0-9-]{0,70}[a-z0-9])?\", str(row.get(\"slug\", \"\")))\n                and clean(row.get(\"name\"), 160)]\n    reference = metadata_reference(value)\n    if not reference.startswith(\"lutris:\"):\n        raise ValueError(\"Expected a Lutris reference\")\n    slug = reference[7:]\n    data = json.loads(metadata_fetch(\"https://lutris.net/api/games/\" + slug, 2 * 1024 * 1024, missing_ok=operation == \"search\"))\n    name = clean(data.get(\"name\"), 160)\n    if data.get(\"slug\") != slug or not name:\n        if operation == \"search\": return []\n        raise ValueError(\"Lutris did not return game details.\")\n    if operation == \"search\": return [{\"app_id\": reference, \"name\": name}]\n    text = clean(data.get(\"description\"), 3000)\n    if not text:\n        raise ValueError(\"Lutris did not return a game description.\")\n    cover, background = data.get(\"coverart\"), data.get(\"banner_url\")\n    covers = [cover or background]\n    image_id = re.fullmatch(r\"https://lutris\\.net/media/igdb/cover_big/(co[a-zA-Z0-9]+)\\.jpg\", cover or \"\")\n    if image_id:\n        covers.insert(0, \"https://images.igdb.com/igdb/image/upload/t_cover_big_2x/\" + image_id.group(1) + \".jpg\")\n    artwork = metadata_artwork({\"cover\": covers, \"background\": [background or cover]})\n    card = dict(id=\"lutris-\" + slug, name=name, subtitle=clean(data.get(\"year\"), 200),\n                description=text, genre=clean(\" · \".join(row.get(\"name\", \"\") for row in data.get(\"genres\", [])), 100),\n                accent=\"#d9ff82\", source_folder=\"\", executable=\"\", available_on_nas=False,\n                metadata_url=\"https://lutris.net/games/\" + slug + \"/\")\n    return dict(card=card, provider_id=reference, **artwork)\n\ndef wikipedia_metadata(operation, value):\n    import html, json, re\n    from urllib.parse import urlencode, urlsplit, quote, unquote\n    def clean(text, limit):\n        return \" \".join(html.unescape(re.sub(r\"<[^>]*>\", \" \", str(text or \"\"))).split())[:limit]\n    def is_game(description):\n        return (re.search(r\"(?:video game|computer game|компьютерн.{0,16}игр|видеоигр)\", description or \"\", re.I)\n                and not re.search(r\"series|franchise|character|серия|персонаж\", description or \"\", re.I))\n    if operation not in (\"search\", \"details\") or not isinstance(value, str):\n        raise ValueError(\"Invalid Wikipedia operation\")\n    if operation == \"search\" and not value.startswith(\"wikipedia:\"):\n        link = re.fullmatch(r\"https://(en|ru)\\.wikipedia\\.org/wiki/([^?#]{1,400})\", value)\n        language = link.group(1) if link else \"ru\" if re.search(r\"[А-Яа-яЁё]\", value) else \"en\"\n        if link:\n            data = json.loads(metadata_fetch(\"https://\" + language + \".wikipedia.org/api/rest_v1/page/summary/\"\n                                             + quote(unquote(link.group(2)), safe=\"\"), 2 * 1024 * 1024))\n            return ([{\"app_id\": \"wikipedia:\" + language + \":\" + str(data[\"pageid\"]), \"name\": clean(data[\"title\"], 160)}]\n                    if type(data.get(\"pageid\")) is int and 0 < data[\"pageid\"] < 10**12 and is_game(data.get(\"description\")) else [])\n        rows = json.loads(metadata_fetch(\"https://\" + language + \".wikipedia.org/w/rest.php/v1/search/page?\"\n                                        + urlencode(dict(q=value, limit=12)), 2 * 1024 * 1024)).get(\"pages\", [])\n        return [{\"app_id\": \"wikipedia:\" + language + \":\" + str(row[\"id\"]), \"name\": clean(row.get(\"title\"), 160)}\n                for row in rows if isinstance(row, dict) and type(row.get(\"id\")) is int and 0 < row[\"id\"] < 10**12\n                and is_game(row.get(\"description\"))][:12]\n    reference = metadata_reference(value)\n    if not reference.startswith(\"wikipedia:\"):\n        raise ValueError(\"Expected a Wikipedia reference\")\n    _, language, page_id = reference.split(\":\")\n    params = dict(action=\"query\", format=\"json\", formatversion=2, pageids=page_id,\n                  prop=\"extracts|pageimages|description\", exintro=1, explaintext=1, exchars=1200,\n                  piprop=\"original|thumbnail\", pithumbsize=1200, pilicense=\"any\")\n    pages = json.loads(metadata_fetch(\"https://\" + language + \".wikipedia.org/w/api.php?\" + urlencode(params),\n                                     2 * 1024 * 1024)).get(\"query\", {}).get(\"pages\", [])\n    page = next((row for row in pages if row.get(\"pageid\") == int(page_id) and row.get(\"ns\") == 0), {})\n    name, text = clean(page.get(\"title\"), 160), clean(page.get(\"extract\"), 2400)\n    if not name or not text or not is_game(page.get(\"description\")):\n        if operation == \"search\": return []\n        raise ValueError(\"Wikipedia did not return a game description.\")\n    if operation == \"search\": return [{\"app_id\": reference, \"name\": name}]\n    image = page.get(\"original\", {}).get(\"source\", \"\")\n    if not re.search(r\"\\.(?:jpg|jpeg|png|webp)$\", urlsplit(image).path, re.I):\n        image = page.get(\"thumbnail\", {}).get(\"source\")\n    artwork = metadata_artwork({\"cover\": [image], \"background\": [image]})\n    url = \"https://\" + language + \".wikipedia.org/?curid=\" + page_id\n    card = dict(id=\"wikipedia-\" + language + \"-\" + page_id, name=name, subtitle=\"Wikipedia\",\n                description=text + \"\\n\\nSource: Wikipedia, \" + url + \" · CC BY-SA 4.0.\", genre=\"\",\n                accent=\"#d9ff82\", source_folder=\"\", executable=\"\", available_on_nas=False, metadata_url=url)\n    return dict(card=card, provider_id=reference, **artwork)\n\ndef game_metadata(operation, value):\n    import difflib, re, unicodedata\n    if operation == \"details\":\n        reference = metadata_reference(value)\n        if reference.startswith(\"epic:\"): return epic_metadata(operation, reference)\n        if reference.startswith(\"gog:\"): return gog_metadata(operation, reference)\n        if reference.startswith(\"lutris:\"): return lutris_metadata(operation, reference)\n        if reference.startswith(\"wikipedia:\"): return wikipedia_metadata(operation, reference)\n        return steam_metadata(operation, value)\n    if (operation != \"search\" or not isinstance(value, str) or not 1 <= len(value.strip()) <= 160\n            or any(ord(c) < 32 for c in value)):\n        raise ValueError(\"The search title must contain 1 to 160 characters.\")\n    steam_link = re.fullmatch(r\"https://store\\.steampowered\\.com/app/([1-9][0-9]{0,9})(?:/[A-Za-z0-9_-]*)?/?\", value.strip())\n    if steam_link or re.fullmatch(r\"[1-9][0-9]{0,9}\", value.strip()):\n        return steam_metadata(\"search\", steam_link.group(1) if steam_link else value.strip())\n    link = re.fullmatch(r\"https://store\\.epicgames\\.com/(?:[a-z]{2}(?:-[A-Z]{2})?/)?p/([a-z0-9-]+)/?\", value.strip())\n    if link:\n        return epic_metadata(\"search\", \"epic:\" + link.group(1))\n    if value.startswith(\"gog:\"):\n        return gog_metadata(\"search\", value)\n    link = re.fullmatch(r\"https://lutris\\.net/games/([a-z0-9](?:[a-z0-9-]{0,70}[a-z0-9])?)/?\", value.strip())\n    if link or value.startswith(\"lutris:\"):\n        return lutris_metadata(\"search\", \"lutris:\" + link.group(1) if link else value)\n    link = re.fullmatch(r\"https://(en|ru)\\.wikipedia\\.org/\\?curid=([1-9][0-9]{0,11})\", value.strip())\n    if link or value.startswith(\"wikipedia:\"):\n        return wikipedia_metadata(\"search\", \"wikipedia:\" + link.group(1) + \":\" + link.group(2) if link else value)\n    if re.fullmatch(r\"https://(?:en|ru)\\.wikipedia\\.org/wiki/[^?#]{1,400}\", value.strip()):\n        return wikipedia_metadata(\"search\", value.strip())\n    queries, failures, matches = search_queries(value), [], []\n    if not queries:\n        return []\n    numerals = {\"i\": \"1\", \"ii\": \"2\", \"iii\": \"3\", \"iv\": \"4\", \"v\": \"5\", \"vi\": \"6\", \"vii\": \"7\", \"viii\": \"8\", \"ix\": \"9\", \"x\": \"10\"}\n    def words(text):\n        text = \"\".join(c for c in unicodedata.normalize(\"NFKD\", text.casefold()) if not unicodedata.combining(c))\n        return [numerals.get(w, w) for w in re.findall(r\"\\w+\", text.replace(\"'\", \"\").replace(\"’\", \"\"))]\n    def plausible(row, query):\n        target, candidate = words(query), words(row[\"name\"])\n        extra = set(candidate) - set(target)\n        # Short titles need a whole first word: Bo matches Bō: Path..., never Box or Bodycam.\n        if len(target) == 1 and len(target[0]) <= 2:\n            similarity = target == candidate[:1]\n        else:\n            similarity = (target == candidate[:len(target)]\n                          or difflib.SequenceMatcher(None, \"\".join(target), \"\".join(candidate)).ratio() >= .5)\n        return (not extra.intersection({\"soundtrack\", \"soundtracks\", \"ost\", \"dlc\", \"demo\", \"upgrade\", \"artbook\"})\n                and {w for w in target if w.isdigit()} <= set(candidate)\n                and similarity)\n    for query in queries:\n        try:\n            matches = [r for r in steam_metadata(\"search\", query) if plausible(r, query)]\n        except (RuntimeError, OSError) as error:\n            failures.append(error)\n            break\n        if matches:\n            target = \"\".join(words(query))\n            return sorted(matches, key=lambda r: -difflib.SequenceMatcher(None, target, \"\".join(words(r[\"name\"]))).ratio())[:12]\n    query = queries[-1]\n    broad = \" \".join(w for w in words(query) if not w.isdigit())\n    for gog_query in dict.fromkeys((query, broad)):\n        if not gog_query: continue\n        try:\n            expected = set(words(query))\n            matches = [r for r in gog_metadata(\"search\", gog_query) if expected <= set(words(r[\"name\"])) and plausible(r, query)]\n            if matches: return matches[:12]\n        except (RuntimeError, OSError) as error:\n            failures.append(error)\n            break\n    for query in dict.fromkeys((queries[-1], queries[0])):\n        slug = re.sub(r\"[^a-z0-9]+\", \"-\", query.lower()).strip(\"-\")\n        if not re.fullmatch(r\"[a-z0-9][a-z0-9-]{0,68}[a-z0-9]\", slug):\n            continue\n        try:\n            matches = epic_metadata(\"search\", \"epic:\" + slug)\n        except (RuntimeError, OSError) as error:\n            failures.append(error)\n            continue\n        if matches: return matches\n    for provider in (lutris_metadata, wikipedia_metadata):\n        try:\n            matches = [row for row in provider(\"search\", queries[0]) if plausible(row, queries[0])]\n            if matches:\n                target = \"\".join(words(queries[0]))\n                return sorted(matches, key=lambda row: -difflib.SequenceMatcher(None, target, \"\".join(words(row[\"name\"]))).ratio())[:12]\n        except (RuntimeError, OSError) as error:\n            failures.append(error)\n    if failures:\n        raise RuntimeError(\"Artwork providers are temporarily unavailable. Search again.\") from None\n    return []\n\ndef relative_game_path(value, allow_empty=False):\n    if allow_empty and value == \"\":\n        return value\n    if (not isinstance(value, str) or not value or len(value) > 512\n            or re.search(r'[\\x00-\\x1f\\\\:<>\"|?*]', value) or value.startswith(\"/\")\n            or any(part in (\"\", \".\", \"..\") or part.endswith((\" \", \".\"))\n                   or re.fullmatch(r\"(?i:con|conin\\$|conout\\$|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])(?:\\..*)?\", part)\n                   for part in value.split(\"/\"))):\n        raise ValueError(\"Invalid relative game path.\")\n    return value\n\ndef asset_path(path):\n    if (not isinstance(path, str) or not path.startswith(\"/assets/ngs_launcher/\")\n            or urlsplit(path).query or urlsplit(path).fragment or \"%\" in path\n            or unquote(path) != path or \"\\\\\" in path or any(ord(c) < 33 for c in path)\n            or any(p in (\"\", \".\", \"..\") for p in path.split(\"/\")[1:])):\n        raise ValueError(\"Image must belong to the NGS asset directory.\")\n    return path\n\ndef validate_card(card):\n    if not isinstance(card, dict):\n        raise ValueError(\"Expected a game card.\")\n    if \"kind\" in card and card[\"kind\"] != \"installer\":\n        raise ValueError(\"Invalid catalog kind.\")\n    if \"game_id\" in card and (card.get(\"kind\") != \"installer\" or not isinstance(card[\"game_id\"], str)\n                             or not re.fullmatch(r\"[a-z0-9][a-z0-9_-]{0,79}\", card[\"game_id\"])):\n        raise ValueError(\"Invalid installer game identity.\")\n    limits = {\"id\": 80, \"name\": 160, \"subtitle\": 200, \"description\": 3000,\n              \"genre\": 100, \"source_folder\": 512, \"executable\": 512, \"metadata_url\": 500}\n    for key, limit in limits.items():\n        value = card.get(key)\n        if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 and c != \"\\n\" for c in value):\n            raise ValueError(\"Invalid card field: \" + key)\n    if not re.fullmatch(r\"[a-z0-9][a-z0-9_-]{0,79}\", card[\"id\"]) or not card[\"name\"].strip():\n        raise ValueError(\"Invalid game identity.\")\n    if not isinstance(card.get(\"accent\"), str) or not re.fullmatch(r\"#[0-9a-fA-F]{6}\", card[\"accent\"]):\n        raise ValueError(\"Invalid accent color.\")\n    available = card.get(\"available_on_nas\", True)\n    root = card.get(\"source_root\", \"\")\n    if (not isinstance(root, str) or len(root) > 512 or any(ord(c) < 32 for c in root)\n            or (root and (not PureWindowsPath(root).is_absolute() or \"..\" in PureWindowsPath(root).parts))\n            or (root and not available)):\n        raise ValueError(\"Invalid NAS source root\")\n    if type(available) is not bool or (not available and (card[\"source_folder\"] or card[\"executable\"] or \"manifest\" in card)):\n        raise ValueError(\"Metadata-only games cannot declare NAS files\")\n    if \"delivery\" in card and (card[\"delivery\"] != \"on_request\" or not available or not card[\"executable\"]):\n        raise ValueError(\"Invalid game delivery\")\n    if \"total_size\" in card and (type(card[\"total_size\"]) is not int or not 0 <= card[\"total_size\"] <= 2**50 or not available):\n        raise ValueError(\"Invalid game size\")\n    relative_game_path(card[\"source_folder\"], allow_empty=not available)\n    relative_game_path(card[\"executable\"], allow_empty=True)\n    if type(card.get(\"revision\")) is not int or card[\"revision\"] < 1:\n        raise ValueError(\"Invalid card revision.\")\n    for key, limit in ((\"cover\", MAX_IMAGE), (\"background\", MAX_IMAGE), (\"manifest\", 32 * 1024 * 1024)):\n        if key == \"manifest\" and key not in card:\n            continue\n        image = card.get(key, {})\n        if (not isinstance(image, dict) or not isinstance(image.get(\"sha256\"), str)\n                or not re.fullmatch(r\"[0-9a-f]{64}\", image[\"sha256\"])\n                or type(image.get(\"size\")) is not int or not 0 < image[\"size\"] <= limit):\n            raise ValueError(\"Invalid image reference.\")\n        asset_path(image.get(\"path\", \"\"))\n    return {key: card[key] for key in (*limits, \"accent\", \"revision\", \"cover\", \"background\", \"manifest\", \"available_on_nas\", \"source_root\", \"delivery\", \"total_size\", \"kind\", \"game_id\") if key in card}\n\ndef validate_feed(feed):\n    if (not isinstance(feed, dict) or feed.get(\"schema\") != 1\n            or feed.get(\"project\") != \"ngs_launcher\" or not isinstance(feed.get(\"games\"), list)\n            or len(feed[\"games\"]) > 1000):\n        raise ValueError(\"Unsupported catalog format.\")\n    cards = [validate_card(card) for card in feed[\"games\"]]\n    if len({card[\"id\"] for card in cards}) != len(cards):\n        raise ValueError(\"Duplicate game identity.\")\n    return {\"schema\": 1, \"project\": \"ngs_launcher\", \"games\": cards}\n\ndef game_id(value):\n    if not isinstance(value, str) or not re.fullmatch(r\"[a-z0-9][a-z0-9_-]{0,79}\", value):\n        raise ValueError(\"Invalid game ID.\")\n    return value\n\ndef is_link(info):\n    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, \"st_file_attributes\", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)\n\ndef safe_path(root, relative=\"\"):\n    root = Path(root)\n    if not root.is_absolute() or is_link(root.lstat()):\n        raise ValueError(\"The game root must be a normal folder with an absolute path.\")\n    if relative:\n        relative_game_path(relative)\n    target = root\n    for component in relative.split(\"/\") if relative else ():\n        target = target / component\n        try:\n            info = target.lstat()\n        except FileNotFoundError:\n            continue\n        if is_link(info):\n            raise ValueError(\"Links and reparse points are not allowed in game files: \" + relative)\n    if not target.resolve().is_relative_to(root.resolve()):\n        raise ValueError(\"The path escapes the game folder.\")\n    return target\n\ndef folder_store_hint(source, folder):\n    import json, re\n    from pathlib import Path\n    root = safe_path(Path(source), relative_game_path(folder))\n    if not root.is_dir():\n        return {}\n    subdirs = [\"\"]\n    try:\n        import os\n        with os.scandir(root) as entries:\n            for entry in entries:\n                if entry.is_dir(follow_symlinks=False) and not is_link(entry.stat(follow_symlinks=False)):\n                    if entry.name.casefold() not in (\"@eadir\", \"#recycle\", \"engine\", \"redist\", \"_commonredist\"):\n                        subdirs.append(entry.name)\n    except OSError:\n        pass\n    if \"Crack/Game\" not in subdirs:\n        subdirs.append(\"Crack/Game\")\n    steam_ids, gog_ids, epic_ids, gog_exe, gog_title = set(), set(), set(), \"\", \"\"\n    steam_files = (\"steam_appid.txt\", \"steam_settings/steam_appid.txt\", \"steam_emu.ini\", \"steam_api.ini\",\n                   \"steam_api64.ini\", \"tenoke.ini\", \"codex.ini\", \"flt.ini\", \"rune.ini\", \"onlinefix.ini\")\n    for sub in subdirs[:24]:\n        try:\n            base = safe_path(root, sub) if sub else root\n            if not base.is_dir():\n                continue\n        except (OSError, ValueError):\n            continue\n        for name in steam_files:\n            try:\n                path = safe_path(base, name)\n                with path.open(\"rb\") as stream:\n                    raw = stream.read(65537)\n                if len(raw) > 65536:\n                    continue\n                text = raw.decode(\"utf-8-sig\", errors=\"replace\").strip()\n                ids = ({text} if name.endswith(\"steam_appid.txt\") else\n                       set(re.findall(r\"(?im)^\\s*(?:AppId|SteamAppId)\\s*=\\s*([1-9][0-9]{0,9})\\s*(?:[;#].*)?$\", text)))\n                if len(ids) == 1 and re.fullmatch(r\"[1-9][0-9]{0,9}\", next(iter(ids))):\n                    steam_ids.add(next(iter(ids)))\n                elif len(ids) > 1:\n                    return {}\n            except (OSError, ValueError):\n                continue\n        try:\n            path = safe_path(base, \"epic_emu.ini\")\n            with path.open(\"rb\") as stream:\n                raw = stream.read(65537)\n            if len(raw) <= 65536:\n                text = raw.decode(\"utf-8-sig\", errors=\"replace\")\n                for slug in re.findall(r\"(?im)^\\s*AppName\\s*=\\s*([A-Za-z0-9_-]{2,64})\\s*$\", text):\n                    norm = re.sub(r\"(?<=[a-z])(?=[A-Z0-9])\", \"-\", slug).lower().strip(\"-\")\n                    if re.fullmatch(r\"[a-z0-9][a-z0-9-]{0,68}[a-z0-9]\", norm):\n                        epic_ids.add(\"epic:\" + norm)\n        except (OSError, ValueError):\n            pass\n        try:\n            import os\n            with os.scandir(base) as entries:\n                for entry in entries:\n                    if not entry.name.lower().startswith(\"goggame-\") or not entry.name.lower().endswith(\".info\"):\n                        continue\n                    if not entry.is_file(follow_symlinks=False):\n                        continue\n                    info_path = safe_path(base, entry.name)\n                    with info_path.open(\"rb\") as stream:\n                        raw = stream.read(65537)\n                    if len(raw) > 65536:\n                        continue\n                    doc = json.loads(raw.decode(\"utf-8-sig\", errors=\"replace\"))\n                    if not isinstance(doc, dict):\n                        continue\n                    gid = str(doc.get(\"rootGameId\") or doc.get(\"gameId\") or \"\").strip()\n                    if re.fullmatch(r\"[1-9][0-9]{0,19}\", gid):\n                        # Only primary game info (where gameId == rootGameId or rootGameId is absent) sets title/exe\n                        if str(doc.get(\"gameId\") or gid) == gid:\n                            gog_ids.add(\"gog:\" + gid)\n                            if isinstance(doc.get(\"name\"), str) and doc[\"name\"].strip():\n                                gog_title = doc[\"name\"].strip()[:160]\n                            for task in doc.get(\"playTasks\") or ():\n                                if isinstance(task, dict) and task.get(\"category\") == \"game\" and task.get(\"isPrimary\", True):\n                                    tpath = str(task.get(\"path\") or \"\").replace(\"\\\\\", \"/\").lstrip(\"/\")\n                                    if tpath.lower().endswith(\".exe\"):\n                                        rel = (sub + \"/\" + tpath if sub else tpath).strip(\"/\")\n                                        try:\n                                            if safe_path(root, rel).is_file():\n                                                gog_exe = relative_game_path(rel)\n                                                break\n                                        except (OSError, ValueError):\n                                            pass\n        except (OSError, ValueError):\n            pass\n    result = {}\n    if len(steam_ids) == 1:\n        result[\"provider_id\"] = \"steam:\" + next(iter(steam_ids))\n    elif len(gog_ids) == 1:\n        result[\"provider_id\"] = next(iter(gog_ids))\n    elif len(epic_ids) == 1:\n        result[\"provider_id\"] = next(iter(epic_ids))\n    if gog_exe:\n        result[\"executable\"] = gog_exe\n    if gog_title:\n        result[\"name\"] = gog_title\n    return result\n\ndef select_best_executable(folder, candidates, known_exes=()):\n    import re\n    from pathlib import Path\n    usable = [item for item in candidates if not item.get(\"utility\")]\n    if not usable:\n        return None\n    if len(usable) == 1:\n        return usable[0][\"path\"]\n    by_lower = {item[\"path\"].casefold(): item[\"path\"] for item in usable}\n    by_name = {}\n    for item in usable:\n        name = item[\"path\"].rsplit(\"/\", 1)[-1].casefold()\n        by_name.setdefault(name, []).append(item[\"path\"])\n    for known in known_exes:\n        norm = str(known or \"\").replace(\"\\\\\", \"/\").lstrip(\"/\").casefold()\n        if not norm:\n            continue\n        if norm in by_lower:\n            return by_lower[norm]\n        leaf = norm.rsplit(\"/\", 1)[-1]\n        if leaf in by_name and len(by_name[leaf]) == 1:\n            return by_name[leaf][0]\n        suffix_matches = [item[\"path\"] for item in usable if item[\"path\"].casefold().endswith(\"/\" + norm)]\n        if len(suffix_matches) == 1:\n            return suffix_matches[0]\n    # Unreal Engine pattern: RootWrapper.exe + .../Binaries/Win64/<Stem>-Win64-Shipping.exe\n    shipping_stems = set()\n    for item in usable:\n        m = re.search(r\"(?:^|/)binaries/win(?:64|gdk)/([a-z0-9_]+)-win(?:64|gdk)-shipping\\.exe$\", item[\"path\"], re.I)\n        if m:\n            shipping_stems.add(m.group(1).casefold())\n    if shipping_stems:\n        wrappers = [item[\"path\"] for item in usable if Path(item[\"path\"]).stem.casefold() in shipping_stems\n                    and \"/binaries/\" not in item[\"path\"].casefold()]\n        if len(wrappers) == 1:\n            return wrappers[0]\n    # Check if a single root executable matches the cleaned folder title or stem of a subfolder binary\n    clean_key = title_key(folder)\n    exact_stem = [item[\"path\"] for item in usable if title_key(Path(item[\"path\"]).stem) == clean_key]\n    if len(exact_stem) == 1:\n        return exact_stem[0]\n    # If x64 vs x86/x64Vk variants of the same executable name exist (e.g. x64/Hades.exe, x86/Hades.exe)\n    stems = {Path(item[\"path\"]).name.casefold() for item in usable}\n    if len(stems) == 1:\n        x64 = [item[\"path\"] for item in usable if re.search(r\"(?:^|/)x64/\", item[\"path\"], re.I)]\n        if len(x64) == 1:\n            return x64[0]\n        root_level = [item[\"path\"] for item in usable if \"/\" not in item[\"path\"]]\n        if len(root_level) == 1:\n            return root_level[0]\n    # DX12 / Vulkan / Trial variants of the same game binary in the same folder\n    no_trial = [item for item in usable if not re.search(r\"(?:trial|_x86|_eac|launcher)\\.exe$\", item[\"path\"], re.I)]\n    if len(no_trial) == 1:\n        return no_trial[0][\"path\"]\n    if len(no_trial) == 2:\n        s0, s1 = Path(no_trial[0][\"path\"]).stem.casefold(), Path(no_trial[1][\"path\"]).stem.casefold()\n        if s1 in (s0 + \"_dx11\", s0 + \"_dx12\", s0 + \"_vk\"):\n            return no_trial[0][\"path\"]\n        if s0 in (s1 + \"_dx11\", s1 + \"_dx12\", s1 + \"_vk\"):\n            return no_trial[1][\"path\"]\n    return None\n\ndef load_bundled_known_games():\n    import base64, json, sys, types, zlib\n    from pathlib import Path\n    cache_name = \"_ngs_launcher_known_games_mem\"\n    cached = sys.modules.get(cache_name)\n    if cached is not None and hasattr(cached, \"games\"):\n        return cached.games\n    games = []\n    try:\n        from ngs.compression import open_compressed\n        for candidate in (Path(__file__).resolve().parents[1] / \"assets/known-games.zst\",\n                          Path(sys.executable).resolve().parent / \"assets/known-games.zst\",\n                          Path(sys.executable).resolve().parent / \"runtime/assets/known-games.zst\"):\n            if candidate.is_file():\n                with open_compressed(candidate) as stream:\n                    doc = json.loads(stream.read(16 * 1024 * 1024).decode(\"utf-8\"))\n                games = doc.get(\"games\", [])\n                break\n    except Exception:\n        pass\n    if not games:\n        b64_blob = globals().get(\"KNOWN_GAMES_ZLIB_B64\", \"\")\n        if not b64_blob:\n            try:\n                from server.plugin.known_games_data import KNOWN_GAMES_ZLIB_B64 as b64_blob\n            except Exception:\n                try:\n                    from ngs_launcher_plugin.known_games_data import KNOWN_GAMES_ZLIB_B64 as b64_blob\n                except Exception:\n                    b64_blob = \"\"\n        if b64_blob:\n            try:\n                raw = zlib.decompress(base64.b64decode(b64_blob))\n                games = json.loads(raw.decode(\"utf-8\")).get(\"games\", [])\n            except Exception:\n                games = []\n    mod = types.ModuleType(cache_name)\n    mod.games = games\n    sys.modules[cache_name] = mod\n    return games\n\ndef match_known_game(folder, candidates, extra_entries=(), source=None):\n    from pathlib import Path\n    hint = folder_store_hint(source, folder) if source else {}\n    entries = list(extra_entries) + list(load_bundled_known_games())\n    by_title = {}\n    by_provider = {}\n    by_exe = {}\n    for entry in entries:\n        name = entry.get(\"n\") or entry.get(\"name\") or \"\"\n        if not name:\n            continue\n        prov = entry.get(\"p\") or entry.get(\"provider_id\") or \"\"\n        exes = entry.get(\"e\") or entry.get(\"executables\") or ()\n        if isinstance(exes, str):\n            import json\n            try:\n                exes = json.loads(exes)\n            except Exception:\n                exes = [exes] if exes else []\n        aliases = entry.get(\"a\") or entry.get(\"aliases\") or ()\n        if isinstance(aliases, str):\n            import json\n            try:\n                aliases = json.loads(aliases)\n            except Exception:\n                aliases = [aliases] if aliases else []\n        norm_entry = {\"name\": name, \"executables\": list(exes), \"aliases\": list(aliases), \"provider_id\": prov}\n        if prov:\n            by_provider.setdefault(prov, []).append(norm_entry)\n        for text in (name, *aliases):\n            k = title_key(text)\n            if k:\n                by_title.setdefault(k, []).append(norm_entry)\n        for ep in exes:\n            norm_ep = str(ep).replace(\"\\\\\", \"/\").lstrip(\"/\").casefold()\n            if norm_ep:\n                by_exe.setdefault(norm_ep, []).append(norm_entry)\n                leaf = norm_ep.rsplit(\"/\", 1)[-1]\n                if leaf not in (\"game.exe\", \"play.exe\", \"start.exe\", \"launcher.exe\", \"hl2.exe\", \"dosbox.exe\", \"unreal.exe\"):\n                    by_exe.setdefault(leaf, []).append(norm_entry)\n    matched_entry = None\n    if hint.get(\"provider_id\") and hint[\"provider_id\"] in by_provider:\n        matched_entry = by_provider[hint[\"provider_id\"]][0]\n    if not matched_entry:\n        for query in (folder, *search_queries(folder)):\n            k = title_key(query)\n            if k and k in by_title:\n                candidates_for_title = by_title[k]\n                # Prefer an entry whose executable is present in the folder\n                for cand in candidates_for_title:\n                    if select_best_executable(folder, candidates, cand[\"executables\"]):\n                        matched_entry = cand\n                        break\n                if not matched_entry:\n                    matched_entry = candidates_for_title[0]\n                break\n    # If folder name didn't match (e.g. abbreviated folder like 'plucky' or 'g18'), check unique executable match\n    if not matched_entry:\n        usable = [item for item in candidates if not item.get(\"utility\")]\n        exe_matches = []\n        for item in usable:\n            p_low = item[\"path\"].casefold()\n            leaf = p_low.rsplit(\"/\", 1)[-1]\n            for key in (p_low, leaf):\n                if key in by_exe and len({e[\"name\"] for e in by_exe[key]}) == 1:\n                    exe_matches.append(by_exe[key][0])\n                    break\n        if len({e[\"name\"] for e in exe_matches}) == 1:\n            matched_entry = exe_matches[0]\n    known_exes = list(matched_entry[\"executables\"]) if matched_entry else []\n    if hint.get(\"executable\") and hint[\"executable\"] not in known_exes:\n        known_exes.insert(0, hint[\"executable\"])\n    best_exe = select_best_executable(folder, candidates, known_exes)\n    provider_id = hint.get(\"provider_id\") or (matched_entry[\"provider_id\"] if matched_entry else \"\")\n    canonical_name = (matched_entry[\"name\"] if matched_entry else \"\") or hint.get(\"name\") or search_title(folder)\n    return {\n        \"name\": canonical_name,\n        \"executable\": best_exe,\n        \"provider_id\": provider_id,\n        \"matched\": matched_entry is not None or bool(hint.get(\"provider_id\")),\n        \"known_executables\": known_exes,\n    }\n\ndef fetch_remote_known_games(url=\"\", timeout=3):\n    import json, re\n    import urllib.error\n    import urllib.request\n    from urllib.parse import urlsplit\n    target = (url or DEFAULT_KNOWN_GAMES_URL).strip()\n    if not target:\n        return []\n    parsed = urlsplit(target)\n    if (parsed.scheme != \"https\" or parsed.username or parsed.password or parsed.port not in (None, 443)\n            or parsed.fragment or parsed.hostname not in (\"raw.githubusercontent.com\", \"gist.githubusercontent.com\", \"github.com\")):\n        return []\n    try:\n        req = urllib.request.Request(target, headers={\"User-Agent\": \"NGS-Catalogizer/1.0\", \"Accept\": \"application/json\"})\n        with urllib.request.urlopen(req, timeout=timeout) as resp:\n            raw = resp.read(4 * 1024 * 1024 + 1)\n        if len(raw) > 4 * 1024 * 1024:\n            return []\n        doc = json.loads(raw.decode(\"utf-8\"))\n        rows = doc.get(\"games\", doc) if isinstance(doc, dict) else doc\n        if not isinstance(rows, list):\n            return []\n        valid = []\n        for item in rows[:5000]:\n            if not isinstance(item, dict):\n                continue\n            name = str(item.get(\"n\") or item.get(\"name\") or \"\").strip()[:160]\n            if not name:\n                continue\n            exes = item.get(\"e\") or item.get(\"executables\") or []\n            if isinstance(exes, str):\n                exes = [exes]\n            exes = [str(x).replace(\"\\\\\", \"/\").lstrip(\"/\")[:256] for x in exes if isinstance(x, str) and x.lower().endswith(\".exe\")][:16]\n            aliases = item.get(\"a\") or item.get(\"aliases\") or []\n            if isinstance(aliases, str):\n                aliases = [aliases]\n            aliases = [str(a).strip()[:160] for a in aliases if isinstance(a, str) and a.strip()][:16]\n            prov = str(item.get(\"p\") or item.get(\"provider_id\") or \"\").strip()[:80]\n            if prov and not re.fullmatch(r\"(?:steam:[1-9][0-9]{0,9}|gog:[1-9][0-9]{0,19}|epic:[a-z0-9][a-z0-9-]{0,68}[a-z0-9]|lutris:[a-z0-9](?:[a-z0-9-]{0,70}[a-z0-9])?|wikipedia:(?:en|ru):[1-9][0-9]{0,11})\", prov):\n                prov = \"\"\n            if exes or prov:\n                valid.append({\"n\": name, \"e\": exes, \"a\": aliases, \"p\": prov})\n        return valid\n    except Exception:\n        return []\n\ndef validate_transport(transport, document):\n    fields = {\"schema\", \"manifest_sha256\", \"chunks\", \"signature\"}\n    if (not isinstance(transport, dict) or set(transport) not in (fields, fields | {\"key\"})\n            or type(transport[\"schema\"]) is not int or transport[\"schema\"] not in (1, 2)\n            or not isinstance(transport[\"signature\"], str) or len(transport[\"signature\"]) != 344):\n        raise ValueError(\"Invalid HTTP package description.\")\n    if \"key\" in transport:\n        key = transport[\"key\"]\n        if (not isinstance(key, dict) or set(key) != {\"public\", \"signature\"}\n                or any(not isinstance(key[k], str) for k in key)\n                or len(key[\"public\"]) != 380 or len(key[\"signature\"]) != 344):\n            raise ValueError(\"Invalid HTTP package key certificate.\")\n    core = {key: value for key, value in document.items() if key != \"http\"}\n    digest = hashlib.sha256(json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(\",\", \":\")).encode(\"utf8\")).hexdigest()\n    if transport[\"manifest_sha256\"] != digest:\n        raise ValueError(\"HTTP packages belong to a different file list.\")\n    chunks = transport[\"chunks\"]\n    if not isinstance(chunks, list) or len(chunks) > 100_000:\n        raise ValueError(\"Invalid HTTP package list.\")\n    total = 0\n    for chunk in chunks:\n        compressed = transport[\"schema\"] == 2\n        if (not isinstance(chunk, dict) or set(chunk) != ({\"path\", \"size\", \"sha256\", \"raw_size\", \"raw_sha256\", \"codec\"} if compressed else {\"path\", \"size\", \"sha256\"})\n                or type(chunk[\"size\"]) is not int or not 0 < chunk[\"size\"] <= PACKAGE_SIZE + (1024 * 1024 if compressed else 0)\n                or not isinstance(chunk[\"sha256\"], str) or not re.fullmatch(r\"[0-9a-f]{64}\", chunk[\"sha256\"])\n                or (not compressed and chunk[\"path\"] != PACKAGE_PREFIX + chunk[\"sha256\"] + \".bin\")):\n            raise ValueError(\"Invalid HTTP package.\")\n        if compressed and (chunk[\"codec\"] != \"zstd\" or type(chunk[\"raw_size\"]) is not int or not 0 < chunk[\"raw_size\"] <= PACKAGE_SIZE\n                or not isinstance(chunk[\"raw_sha256\"], str) or not re.fullmatch(\"[0-9a-f]{64}\", chunk[\"raw_sha256\"])\n                or not isinstance(chunk[\"path\"], str) or not re.fullmatch(r\"/assets/ngs_launcher/package_jobs/package-[0-9a-f]{32}/chunks/\" + chunk[\"sha256\"] + r\"\\.bin\", chunk[\"path\"])):\n            raise ValueError(\"Invalid compressed HTTP package.\")\n        total += chunk[\"raw_size\"] if compressed else chunk[\"size\"]\n    if total != document.get('archive_size', document[\"total_size\"]):\n        raise ValueError(\"HTTP package sizes do not match the game size.\")\n    return transport\n\ndef validate_manifest(document, card=None):\n    if not isinstance(document, dict) or type(document.get(\"schema\")) is not int or document[\"schema\"] != 1:\n        raise ValueError(\"Unknown manifest format.\")\n    identity = game_id(document.get(\"game_id\"))\n    source = relative_game_path(document.get(\"source_folder\"))\n    executable = relative_game_path(document.get(\"executable\"))\n    if not executable.lower().endswith(\".exe\"):\n        raise ValueError(\"The executable must be an EXE file.\")\n    cwd = relative_game_path(document.get(\"working_directory\", \"\"), allow_empty=True)\n    arguments = document.get(\"arguments\", [])\n    if (not isinstance(arguments, list) or len(arguments) > 64 or any(\n            not isinstance(arg, str) or len(arg) > 2048 or \"\\0\" in arg for arg in arguments)):\n        raise ValueError(\"Invalid launch arguments.\")\n    files, directories = document.get(\"files\"), document.get(\"directories\", [])\n    if (not isinstance(files, list) or not files or len(files) > 200_000\n            or not isinstance(directories, list) or len(directories) > 200_000):\n        raise ValueError(\"Invalid manifest file list.\")\n    kinds, normalized, total = {}, [], 0\n    for directory in directories:\n        relative_game_path(directory)\n        key = directory.casefold()\n        if key in kinds or key.split(\"/\")[0] == RECEIPT:\n            raise ValueError(\"Manifest path conflict: \" + directory)\n        kinds[key] = \"directory\"\n    for item in files:\n        if not isinstance(item, dict):\n            raise ValueError(\"Invalid file record.\")\n        path = relative_game_path(item.get(\"path\"))\n        size, digest = item.get(\"size\"), item.get(\"sha256\")\n        if (type(size) is not int or not 0 <= size <= 2 ** 50 or not isinstance(digest, str)\n                or not re.fullmatch(r\"[0-9a-f]{64}\", digest)):\n            raise ValueError(\"Invalid file size or SHA256.\")\n        key = path.casefold()\n        if key in kinds or key.split(\"/\")[0] == RECEIPT:\n            raise ValueError(\"Manifest path conflict: \" + path)\n        kinds[key] = \"file\"\n        normalized.append({\"path\": path, \"size\": size, \"sha256\": digest})\n        total += size\n    for path in kinds:\n        parts = path.split(\"/\")\n        if any(kinds.get(\"/\".join(parts[:i])) == \"file\" for i in range(1, len(parts))):\n            raise ValueError(\"A file is used as a folder: \" + path)\n    if executable not in {item[\"path\"] for item in normalized}:\n        raise ValueError(\"The executable is missing from the manifest.\")\n    if cwd and cwd.casefold() not in {path for path, kind in kinds.items() if kind == \"directory\"}:\n        raise ValueError(\"The working directory is missing from the manifest.\")\n    if type(document.get(\"total_size\")) is not int or document[\"total_size\"] != total:\n        raise ValueError(\"The manifest total size does not match its file list.\")\n    if card and (identity != card[\"id\"] or source != card[\"source_folder\"] or executable != card[\"executable\"]):\n        raise ValueError(\"The manifest belongs to a different game or executable.\")\n    result = {\"schema\": 1, \"game_id\": identity, \"source_folder\": source, \"executable\": executable,\n            \"working_directory\": cwd, \"arguments\": arguments, \"files\": normalized,\n            \"directories\": directories, \"total_size\": total}\n    if \"http\" in document:\n        result[\"http\"] = validate_transport(document[\"http\"], result)\n    return result\n"))
