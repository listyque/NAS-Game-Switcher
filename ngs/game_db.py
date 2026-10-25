"""Known game <-> executable <-> provider database, store hint readers, and remote sync."""

from ngs.catalog import relative_game_path
from ngs.install import is_link, safe_path
from ngs.metadata import search_queries, search_title


DEFAULT_KNOWN_GAMES_URL = "https://raw.githubusercontent.com/listy/ngs-known-games/main/known-games.json"


def title_key(value):
    import unicodedata
    value = str(value or "").replace("™", "").replace("®", "").replace("©", "")
    value = unicodedata.normalize("NFKD", search_title(value).casefold())
    return "".join(c for c in value if c.isalnum() and not unicodedata.combining(c))


def folder_store_hint(source, folder):
    import json, re
    from pathlib import Path
    root = safe_path(Path(source), relative_game_path(folder))
    if not root.is_dir():
        return {}
    subdirs = [""]
    try:
        import os
        with os.scandir(root) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False) and not is_link(entry.stat(follow_symlinks=False)):
                    if entry.name.casefold() not in ("@eadir", "#recycle", "engine", "redist", "_commonredist"):
                        subdirs.append(entry.name)
    except OSError:
        pass
    if "Crack/Game" not in subdirs:
        subdirs.append("Crack/Game")
    steam_ids, gog_ids, epic_ids, gog_exe, gog_title = set(), set(), set(), "", ""
    steam_files = ("steam_appid.txt", "steam_settings/steam_appid.txt", "steam_emu.ini", "steam_api.ini",
                   "steam_api64.ini", "tenoke.ini", "codex.ini", "flt.ini", "rune.ini", "onlinefix.ini")
    for sub in subdirs[:24]:
        try:
            base = safe_path(root, sub) if sub else root
            if not base.is_dir():
                continue
        except (OSError, ValueError):
            continue
        for name in steam_files:
            try:
                path = safe_path(base, name)
                with path.open("rb") as stream:
                    raw = stream.read(65537)
                if len(raw) > 65536:
                    continue
                text = raw.decode("utf-8-sig", errors="replace").strip()
                ids = ({text} if name.endswith("steam_appid.txt") else
                       set(re.findall(r"(?im)^\s*(?:AppId|SteamAppId)\s*=\s*([1-9][0-9]{0,9})\s*(?:[;#].*)?$", text)))
                if len(ids) == 1 and re.fullmatch(r"[1-9][0-9]{0,9}", next(iter(ids))):
                    steam_ids.add(next(iter(ids)))
                elif len(ids) > 1:
                    return {}
            except (OSError, ValueError):
                continue
        try:
            path = safe_path(base, "epic_emu.ini")
            with path.open("rb") as stream:
                raw = stream.read(65537)
            if len(raw) <= 65536:
                text = raw.decode("utf-8-sig", errors="replace")
                for slug in re.findall(r"(?im)^\s*AppName\s*=\s*([A-Za-z0-9_-]{2,64})\s*$", text):
                    norm = re.sub(r"(?<=[a-z])(?=[A-Z0-9])", "-", slug).lower().strip("-")
                    if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,68}[a-z0-9]", norm):
                        epic_ids.add("epic:" + norm)
        except (OSError, ValueError):
            pass
        try:
            import os
            with os.scandir(base) as entries:
                for entry in entries:
                    if not entry.name.lower().startswith("goggame-") or not entry.name.lower().endswith(".info"):
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    info_path = safe_path(base, entry.name)
                    with info_path.open("rb") as stream:
                        raw = stream.read(65537)
                    if len(raw) > 65536:
                        continue
                    doc = json.loads(raw.decode("utf-8-sig", errors="replace"))
                    if not isinstance(doc, dict):
                        continue
                    gid = str(doc.get("rootGameId") or doc.get("gameId") or "").strip()
                    if re.fullmatch(r"[1-9][0-9]{0,19}", gid):
                        # Only primary game info (where gameId == rootGameId or rootGameId is absent) sets title/exe
                        if str(doc.get("gameId") or gid) == gid:
                            gog_ids.add("gog:" + gid)
                            if isinstance(doc.get("name"), str) and doc["name"].strip():
                                gog_title = doc["name"].strip()[:160]
                            for task in doc.get("playTasks") or ():
                                if isinstance(task, dict) and task.get("category") == "game" and task.get("isPrimary", True):
                                    tpath = str(task.get("path") or "").replace("\\", "/").lstrip("/")
                                    if tpath.lower().endswith(".exe"):
                                        rel = (sub + "/" + tpath if sub else tpath).strip("/")
                                        try:
                                            if safe_path(root, rel).is_file():
                                                gog_exe = relative_game_path(rel)
                                                break
                                        except (OSError, ValueError):
                                            pass
        except (OSError, ValueError):
            pass
    result = {}
    if len(steam_ids) == 1:
        result["provider_id"] = "steam:" + next(iter(steam_ids))
    elif len(gog_ids) == 1:
        result["provider_id"] = next(iter(gog_ids))
    elif len(epic_ids) == 1:
        result["provider_id"] = next(iter(epic_ids))
    if gog_exe:
        result["executable"] = gog_exe
    if gog_title:
        result["name"] = gog_title
    return result


def select_best_executable(folder, candidates, known_exes=()):
    import re
    from pathlib import Path
    usable = [item for item in candidates if not item.get("utility")]
    if not usable:
        return None
    if len(usable) == 1:
        return usable[0]["path"]
    by_lower = {item["path"].casefold(): item["path"] for item in usable}
    by_name = {}
    for item in usable:
        name = item["path"].rsplit("/", 1)[-1].casefold()
        by_name.setdefault(name, []).append(item["path"])
    for known in known_exes:
        norm = str(known or "").replace("\\", "/").lstrip("/").casefold()
        if not norm:
            continue
        if norm in by_lower:
            return by_lower[norm]
        leaf = norm.rsplit("/", 1)[-1]
        if leaf in by_name and len(by_name[leaf]) == 1:
            return by_name[leaf][0]
        suffix_matches = [item["path"] for item in usable if item["path"].casefold().endswith("/" + norm)]
        if len(suffix_matches) == 1:
            return suffix_matches[0]
    # Unreal Engine pattern: RootWrapper.exe + .../Binaries/Win64/<Stem>-Win64-Shipping.exe
    shipping_stems = set()
    for item in usable:
        m = re.search(r"(?:^|/)binaries/win(?:64|gdk)/([a-z0-9_]+)-win(?:64|gdk)-shipping\.exe$", item["path"], re.I)
        if m:
            shipping_stems.add(m.group(1).casefold())
    if shipping_stems:
        wrappers = [item["path"] for item in usable if Path(item["path"]).stem.casefold() in shipping_stems
                    and "/binaries/" not in item["path"].casefold()]
        if len(wrappers) == 1:
            return wrappers[0]
    # Check if a single root executable matches the cleaned folder title or stem of a subfolder binary
    clean_key = title_key(folder)
    exact_stem = [item["path"] for item in usable if title_key(Path(item["path"]).stem) == clean_key]
    if len(exact_stem) == 1:
        return exact_stem[0]
    # If x64 vs x86/x64Vk variants of the same executable name exist (e.g. x64/Hades.exe, x86/Hades.exe)
    stems = {Path(item["path"]).name.casefold() for item in usable}
    if len(stems) == 1:
        x64 = [item["path"] for item in usable if re.search(r"(?:^|/)x64/", item["path"], re.I)]
        if len(x64) == 1:
            return x64[0]
        root_level = [item["path"] for item in usable if "/" not in item["path"]]
        if len(root_level) == 1:
            return root_level[0]
    # DX12 / Vulkan / Trial variants of the same game binary in the same folder
    no_trial = [item for item in usable if not re.search(r"(?:trial|_x86|_eac|launcher)\.exe$", item["path"], re.I)]
    if len(no_trial) == 1:
        return no_trial[0]["path"]
    if len(no_trial) == 2:
        s0, s1 = Path(no_trial[0]["path"]).stem.casefold(), Path(no_trial[1]["path"]).stem.casefold()
        if s1 in (s0 + "_dx11", s0 + "_dx12", s0 + "_vk"):
            return no_trial[0]["path"]
        if s0 in (s1 + "_dx11", s1 + "_dx12", s1 + "_vk"):
            return no_trial[1]["path"]
    return None


def load_bundled_known_games():
    import base64, json, sys, types, zlib
    from pathlib import Path
    cache_name = "_ngs_launcher_known_games_mem"
    cached = sys.modules.get(cache_name)
    if cached is not None and hasattr(cached, "games"):
        return cached.games
    games = []
    try:
        from ngs.compression import open_compressed
        for candidate in (Path(__file__).resolve().parents[1] / "assets/known-games.zst",
                          Path(sys.executable).resolve().parent / "assets/known-games.zst",
                          Path(sys.executable).resolve().parent / "runtime/assets/known-games.zst"):
            if candidate.is_file():
                with open_compressed(candidate) as stream:
                    doc = json.loads(stream.read(16 * 1024 * 1024).decode("utf-8"))
                games = doc.get("games", [])
                break
    except Exception:
        pass
    if not games:
        try:
            from server.plugin.known_games_data import KNOWN_GAMES_ZLIB_B64 as b64_blob
        except Exception:
            try:
                from ngs_launcher_plugin.known_games_data import KNOWN_GAMES_ZLIB_B64 as b64_blob
            except Exception:
                b64_blob = ""
        if b64_blob:
            try:
                raw = zlib.decompress(base64.b64decode(b64_blob))
                games = json.loads(raw.decode("utf-8")).get("games", [])
            except Exception:
                games = []
    if not games:
        try:
            from pyasm.common import Environment
            p = Path(Environment.get_asset_dir()).resolve() / "ngs_launcher/known-games.json"
            if p.is_file() and not p.is_symlink() and p.stat().st_size <= 8 * 1024 * 1024:
                games = json.loads(p.read_bytes()).get("games", [])
        except Exception:
            games = []
    mod = types.ModuleType(cache_name)
    mod.games = games
    sys.modules[cache_name] = mod
    return games


def match_known_game(folder, candidates, extra_entries=(), source=None):
    from pathlib import Path
    hint = folder_store_hint(source, folder) if source else {}
    entries = list(extra_entries) + list(load_bundled_known_games())
    by_title = {}
    by_provider = {}
    by_exe = {}
    for entry in entries:
        name = entry.get("n") or entry.get("name") or ""
        if not name:
            continue
        prov = entry.get("p") or entry.get("provider_id") or ""
        exes = entry.get("e") or entry.get("executables") or ()
        if isinstance(exes, str):
            import json
            try:
                exes = json.loads(exes)
            except Exception:
                exes = [exes] if exes else []
        aliases = entry.get("a") or entry.get("aliases") or ()
        if isinstance(aliases, str):
            import json
            try:
                aliases = json.loads(aliases)
            except Exception:
                aliases = [aliases] if aliases else []
        norm_entry = {"name": name, "executables": list(exes), "aliases": list(aliases), "provider_id": prov}
        if prov:
            by_provider.setdefault(prov, []).append(norm_entry)
        for text in (name, *aliases):
            k = title_key(text)
            if k:
                by_title.setdefault(k, []).append(norm_entry)
        for ep in exes:
            norm_ep = str(ep).replace("\\", "/").lstrip("/").casefold()
            if norm_ep:
                leaf = norm_ep.rsplit("/", 1)[-1]
                generic = leaf in ("game.exe", "play.exe", "start.exe", "launcher.exe", "hl2.exe", "dosbox.exe", "unreal.exe", "main.exe", "client.exe", "app.exe", "setup.exe", "install.exe")
                if "/" in norm_ep or not generic:
                    by_exe.setdefault(norm_ep, []).append(norm_entry)
                if not generic:
                    by_exe.setdefault(leaf, []).append(norm_entry)
    matched_entry = None
    if hint.get("provider_id") and hint["provider_id"] in by_provider:
        matched_entry = by_provider[hint["provider_id"]][0]
    if not matched_entry:
        for query in (folder, *search_queries(folder)):
            k = title_key(query)
            if k and k in by_title:
                candidates_for_title = by_title[k]
                # Prefer an entry whose executable is present in the folder
                for cand in candidates_for_title:
                    if select_best_executable(folder, candidates, cand["executables"]):
                        matched_entry = cand
                        break
                if not matched_entry:
                    matched_entry = candidates_for_title[0]
                break
    # If folder name didn't match (e.g. abbreviated folder like 'plucky' or 'g18'), check unique executable match
    if not matched_entry:
        usable = [item for item in candidates if not item.get("utility")]
        exe_matches = []
        for item in usable:
            p_low = item["path"].casefold()
            leaf = p_low.rsplit("/", 1)[-1]
            for key in (p_low, leaf):
                if key in by_exe and len({e["name"] for e in by_exe[key]}) == 1:
                    exe_matches.append(by_exe[key][0])
                    break
        if len({e["name"] for e in exe_matches}) == 1:
            matched_entry = exe_matches[0]
    known_exes = list(matched_entry["executables"]) if matched_entry else []
    if hint.get("executable") and hint["executable"] not in known_exes:
        known_exes.insert(0, hint["executable"])
    best_exe = select_best_executable(folder, candidates, known_exes)
    provider_id = hint.get("provider_id") or (matched_entry["provider_id"] if matched_entry else "")
    canonical_name = (matched_entry["name"] if matched_entry else "") or hint.get("name") or search_title(folder)
    return {
        "name": canonical_name,
        "executable": best_exe,
        "provider_id": provider_id,
        "matched": matched_entry is not None or bool(hint.get("provider_id")),
        "known_executables": known_exes,
    }


def fetch_remote_known_games(url="", timeout=3):
    import json, re
    import urllib.error
    import urllib.request
    from urllib.parse import urlsplit
    target = (url or DEFAULT_KNOWN_GAMES_URL).strip()
    if not target:
        return []
    parsed = urlsplit(target)
    if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443)
            or parsed.fragment or parsed.hostname not in ("raw.githubusercontent.com", "gist.githubusercontent.com", "github.com")):
        return []
    try:
        req = urllib.request.Request(target, headers={"User-Agent": "NGS-Catalogizer/1.0", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            return []
        doc = json.loads(raw.decode("utf-8"))
        rows = doc.get("games", doc) if isinstance(doc, dict) else doc
        if not isinstance(rows, list):
            return []
        valid = []
        for item in rows[:5000]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("n") or item.get("name") or "").strip()[:160]
            if not name:
                continue
            exes = item.get("e") or item.get("executables") or []
            if isinstance(exes, str):
                exes = [exes]
            exes = [str(x).replace("\\", "/").lstrip("/")[:256] for x in exes if isinstance(x, str) and x.lower().endswith(".exe")][:16]
            aliases = item.get("a") or item.get("aliases") or []
            if isinstance(aliases, str):
                aliases = [aliases]
            aliases = [str(a).strip()[:160] for a in aliases if isinstance(a, str) and a.strip()][:16]
            prov = str(item.get("p") or item.get("provider_id") or "").strip()[:80]
            if prov and not re.fullmatch(r"(?:steam:[1-9][0-9]{0,9}|gog:[1-9][0-9]{0,19}|epic:[a-z0-9][a-z0-9-]{0,68}[a-z0-9]|lutris:[a-z0-9](?:[a-z0-9-]{0,70}[a-z0-9])?|wikipedia:(?:en|ru):[1-9][0-9]{0,11})", prov):
                prov = ""
            if exes or prov:
                valid.append({"n": name, "e": exes, "a": aliases, "p": prov})
        return valid
    except Exception:
        return []

