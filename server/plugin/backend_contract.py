import re, json, hashlib, unicodedata
from pathlib import PurePosixPath, PureWindowsPath
from urllib.parse import urlsplit, unquote
IMPORT_FIELDS = ('name', 'subtitle', 'genre', 'accent', 'description', 'source_folder', 'executable', 'metadata_url')
DEFAULT_KNOWN_GAMES_URL = 'https://raw.githubusercontent.com/listy/ngs-known-games/main/known-games.json'
PACKAGE_SIZE = 268435456
PACKAGE_PREFIX = '/assets/ngs_launcher/packages/'
MAX_IMAGE = 8 * 1024 * 1024
RECEIPT = '.ngs-install.json'
def folder_key(value):
    return unicodedata.normalize("NFC", value).casefold()

def path_key(value):
    import ntpath, unicodedata
    return unicodedata.normalize("NFC", ntpath.normpath(str(value))).casefold()

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

def metadata_reference(value):
    import re
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,9}", value):
        return "steam:" + value
    if isinstance(value, str) and re.fullmatch(r"epic:[a-z0-9][a-z0-9-]{0,68}[a-z0-9]", value):
        return value
    if isinstance(value, str) and re.fullmatch(r"gog:[1-9][0-9]{0,19}", value):
        return value
    if isinstance(value, str) and re.fullmatch(r"lutris:[a-z0-9](?:[a-z0-9-]{0,70}[a-z0-9])?", value):
        return value
    if isinstance(value, str) and re.fullmatch(r"wikipedia:(?:en|ru):[1-9][0-9]{0,11}", value):
        return value
    raise ValueError("Invalid game reference")

def metadata_fetch(url, limit, missing_ok=False, timeout=12):
    import sys, time, types, threading
    from collections import OrderedDict
    import urllib.error
    import urllib.request
    from urllib.parse import urlsplit
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443)
            or parsed.fragment or parsed.hostname not in ("store.steampowered.com", "shared.akamai.steamstatic.com",
                "cdn.akamai.steamstatic.com", "shared.fastly.steamstatic.com", "cdn.cloudflare.steamstatic.com",
                "store-content.ak.epicgames.com", "cdn2.unrealengine.com", "cdn1.epicgames.com",
                "catalog.gog.com", "api.gog.com", "images.gog-statics.com", "images-1.gog-statics.com",
                "images-2.gog-statics.com", "images-3.gog-statics.com", "images-4.gog-statics.com",
                "lutris.net", "images.igdb.com", "en.wikipedia.org", "ru.wikipedia.org", "upload.wikimedia.org")):
        raise ValueError("Untrusted metadata URL")
    name = "_ngs_launcher_provider_cache"
    cache = sys.modules.get(name)
    if cache is None:
        candidate = types.ModuleType(name)
        candidate.items, candidate.lock = OrderedDict(), threading.RLock()
        cache = sys.modules.setdefault(name, candidate)
    with cache.lock:
        now = time.monotonic()
        for key, (saved, data) in list(cache.items.items()):
            if now - saved >= 300: del cache.items[key]
        if url in cache.items:
            saved, data = cache.items[url]
            if len(data) > limit: raise ValueError("Metadata response exceeds size limit")
            cache.items.move_to_end(url)
            return data
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise ValueError("Metadata redirects are disabled")
    request = urllib.request.Request(url, headers={"User-Agent": "NGS-Catalogizer/1.0", "Accept": "application/json,image/*"})
    for attempt in range(2):
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout) as response:
                data = response.read(limit + 1)
                if len(data) > limit:
                    raise ValueError("Metadata response exceeds size limit")
                # Public provider bytes only; API authentication and catalog records stay uncached.
                with cache.lock:
                    cache.items[url] = (time.monotonic(), data)
                    cache.items.move_to_end(url)
                    while len(cache.items) > 32 or sum(len(item[1]) for item in cache.items.values()) > 16 * 1024 * 1024:
                        cache.items.popitem(last=False)
                return data
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            if missing_ok and code == 404:
                return b"{}"
            raise RuntimeError("Metadata HTTP %s" % code) from None
        except urllib.error.URLError as error:
            # A fresh connection can recover the NAS's transient unavailable-address error.
            if attempt or getattr(error.reason, "errno", None) not in (99, 101, 10049, 10051):
                raise

def metadata_artwork(sources):
    import base64, hashlib
    from concurrent.futures import ThreadPoolExecutor
    sources = {context: tuple(dict.fromkeys(url for url in urls if url)) for context, urls in sources.items()}
    if any(not urls for urls in sources.values()):
        raise RuntimeError("The provider returned no game images.")
    def download(urls):
        for index, url in enumerate(urls):
            try:
                data = metadata_fetch(url, 8 * 1024 * 1024, timeout=4 if index + 1 < len(urls) else 12)
                return {"base64": base64.b64encode(data).decode("ascii"), "sha256": hashlib.sha256(data).hexdigest()}
            except (RuntimeError, OSError):
                continue
        raise RuntimeError("The artwork provider did not return an image. Try downloading again.")
    # Only network reads run in these threads; TACTIC writes stay on its request thread.
    unique = list(dict.fromkeys(sources.values()))
    with ThreadPoolExecutor(max_workers=2) as pool:
        images = dict(zip(unique, pool.map(download, unique)))
    return {context: images[urls] for context, urls in sources.items()}

def search_title(value):
    import re
    value = re.sub(r"(?i)^\s*(?:\[(?:dixen18|xatab|fitgirl|dodi|elamigos|dl|p|l|gog|steam|repack|portable)\]\s*)+", "", value)
    value = re.sub(r"(?i)(?:\s*\[(?:p|l|dl|rus|eng|multi\d*|repack|portable|gog|steam)[^\]]*\])+\s*$", "", value)
    value = re.sub(r"(?i)(?:\s*\((?:rus|eng|multi\d*|repack|portable|gog|steam|\d{4,6})[^)]*\))+\s*$", "", value)
    value = value.replace("_", " ")
    value = re.sub(r"(?<=[a-z])(?=[A-Z][a-z]|RTX\b)", " ", value)
    value = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", value)
    value = re.sub(r"(?i)[ -]+(?:win[ -]+)?gog\b.*$", "", value)
    value = re.sub(r"(?i)[ -]+(?:by\s+)?(?:insane\s*ram\s*zes|fitgirl|dodi|elamigos|xatab|dixen18|rune|codex|tenoke|flt|kaos|rg\s+mechanics|chovka|selezen|wanterlude)\b.*$", "", value)
    value = re.split(r"(?i)\s+(?:v\d|build\s*\d|repack\b|(?:win\s+)?gog\b|steam\b|rip\b|\d+\.\d{3,})", value, maxsplit=1)[0]
    value = re.sub(r"\s*\(\d{4}(?:-\d{4})?\)\s*$", "", value)
    value = re.sub(r"\b(\d)\s+0\b", r"\1.0", value)
    return " ".join(value.split())[:160]

def search_queries(value):
    import re
    clean = search_title(value)
    clean = re.sub(r"(?i)\btmnt\b", "Teenage Mutant Ninja Turtles", clean)
    original = clean
    clean = re.sub(r"[-‐‑‒–—―−﹘﹣－:/\\|]+", " ", clean)
    clean = re.sub(r"['’‘\"“”«»™®©]", "", clean)
    clean = " ".join(clean.split())
    base = re.sub(r"(?i)\s+(?:(?:digital\s+)?(?:deluxe|ultimate|premium|gold|complete|definitive|standard|game of the year|goty)\s*(?:edition)?|campaign only)\b.*$", "", clean)
    remake = re.sub(r"(?i)\s+(?:remake|ремейк|ремэйк)\b", "", base)
    collection = re.sub(r"(?i)\s+(?:collection|anthology|dilogy|trilogy|дилогия|трилогия|антология|сборник)$", "", remake)
    # Some release folders prepend "game"; search the full title first to keep Game Dev Tycoon intact.
    package = re.sub(r"(?i)^game\s+(?=\S)", "", collection)
    return list(dict.fromkeys(q.strip() for q in (clean, original, base, remake, collection, package) if q.strip()))

def steam_metadata(operation, value):
    import html
    import json
    import re
    from urllib.parse import urlencode
    fetch = metadata_fetch

    def clean(value, limit):
        return " ".join(html.unescape(re.sub(r"<[^>]*>", " ", str(value or ""))).split())[:limit]

    if operation == "search":
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 160 or any(ord(c) < 32 for c in value):
            raise ValueError("The search title must contain 1 to 160 characters.")
        if not re.fullmatch(r"[1-9][0-9]{0,9}", value.strip()):
            query = urlencode({"term": value, "l": "russian" if re.search(r"[А-Яа-яЁё]", value) else "english", "cc": "US"})
            result = json.loads(fetch("https://store.steampowered.com/api/storesearch/?" + query, 2 * 1024 * 1024))
            return [{"app_id": str(item["id"]), "name": clean(item["name"], 160)}
                    for item in result.get("items", [])[:12] if str(item.get("id", "")).isdigit() and item.get("type", "app") == "app"]
        value = value.strip()
    if operation not in ("search", "details") or not re.fullmatch(r"[1-9][0-9]{0,9}", str(value)):
        raise ValueError("Invalid Steam app ID")
    app_id = str(value)
    query = urlencode({"appids": app_id, "l": "english" if operation == "search" else "russian", "cc": "US"})
    result = json.loads(fetch("https://store.steampowered.com/api/appdetails?" + query, 4 * 1024 * 1024)).get(app_id, {})
    game = result.get("data", {})
    if not result.get("success") or game.get("type") != "game":
        if operation == "search":
            return []
        raise ValueError("Steam did not return game details. Choose another result.")
    if operation == "search":
        return [{"app_id": app_id, "name": clean(game.get("name"), 160)}]
    card = {"id": "steam-" + app_id, "name": clean(game.get("name"), 160),
            "subtitle": clean(game.get("short_description"), 200),
            "description": clean(game.get("about_the_game") or game.get("short_description"), 3000),
            "genre": clean(" · ".join(g["description"] for g in game.get("genres", [])), 100),
            "accent": "#d9ff82", "source_folder": "", "executable": "", "available_on_nas": False,
            "metadata_url": "https://store.steampowered.com/app/" + app_id + "/"}
    assets = "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/" + app_id
    artwork = metadata_artwork({"cover": [assets + "/library_600x900.jpg", game.get("header_image")],
                                "background": [assets + "/library_hero.jpg", game.get("background_raw"), game.get("header_image")]})
    return {"card": card, "provider_id": "steam:" + app_id, **artwork}

def epic_metadata(operation, value):
    import html, json, re
    if operation not in ("search", "details"):
        raise ValueError("Invalid metadata operation")
    reference = metadata_reference(value)
    if not reference.startswith("epic:"):
        raise ValueError("Expected an Epic reference")
    slug = reference[5:]
    # shortcut: Epic exposes product slugs, not a documented public search; accept an official product URL for other slugs.
    document = json.loads(metadata_fetch("https://store-content.ak.epicgames.com/api/ru/content/products/" + slug,
                                         2 * 1024 * 1024, missing_ok=True))
    pages = document.get("pages", [])
    page = next((p for p in pages if p.get("_slug") == "home"), {})
    data = page.get("data", {})
    about, hero = data.get("about", {}), data.get("hero", {})
    def clean(text, limit):
        return " ".join(html.unescape(re.sub(r"<[^>]*>", " ", str(text or ""))).split())[:limit]
    name = clean(document.get("productName") or page.get("productName") or document.get("_title", "").replace("-", " "), 160)
    if document.get("_slug") != slug or not name or not about.get("description"):
        if operation == "search": return []
        raise ValueError("Epic did not return game details. Check the title or URL.")
    if operation == "search":
        return [{"app_id": reference, "name": name}]
    card = dict(id="epic-" + slug, name=name, subtitle=clean(about.get("shortDescription"), 200),
                description=clean(about["description"], 3000), genre="", accent="#d9ff82",
                source_folder="", executable="", available_on_nas=False,
                metadata_url="https://store.epicgames.com/p/" + slug)
    cover = hero.get("portraitBackgroundImageUrl") or about.get("image", {}).get("src")
    background = hero.get("backgroundImageUrl") or cover
    artwork = metadata_artwork({"cover": [cover], "background": [background]})
    return dict(card=card, provider_id=reference, **artwork)

def gog_metadata(operation, value):
    import html, json, re, unicodedata
    from urllib.parse import urlencode
    def clean(text, limit):
        return " ".join(html.unescape(re.sub(r"<[^>]*>", " ", str(text or ""))).split())[:limit]
    def catalog(query):
        query = "".join(c for c in unicodedata.normalize("NFKD", query) if not unicodedata.combining(c))
        query = " ".join(re.findall(r"\w+", query))
        params = urlencode(dict(query=query.lower(), limit=12, countryCode="US",
                                locale="en-US", currencyCode="USD", order="desc:score"))
        return json.loads(metadata_fetch("https://catalog.gog.com/v1/catalog?" + params, 2 * 1024 * 1024)).get("products", [])
    if operation not in ("search", "details") or not isinstance(value, str):
        raise ValueError("Invalid GOG operation")
    if operation == "search" and not value.startswith("gog:"):
        return [{"app_id": "gog:" + str(p["id"]), "name": clean(p["title"], 160)} for p in catalog(value)
                if p.get("productType") in ("game", "pack") and re.fullmatch(r"[1-9][0-9]{0,19}", str(p.get("id", "")))][:12]
    reference = metadata_reference(value)
    if not reference.startswith("gog:"):
        raise ValueError("Expected a GOG reference")
    product = json.loads(metadata_fetch("https://api.gog.com/products/" + reference[4:] + "?expand=description,screenshots",
                                       2 * 1024 * 1024))
    if str(product.get("id")) != reference[4:] or product.get("game_type") not in ("game", "pack"):
        raise ValueError("GOG did not return game details.")
    name = clean(product.get("title"), 160)
    if not name:
        raise ValueError("GOG did not return a game title.")
    if operation == "search": return [{"app_id": reference, "name": name}]
    art = next((p for p in catalog(name) if str(p.get("id")) == reference[4:]), {})
    description = product.get("description", {})
    text = clean(description.get("full") or description.get("lead"), 3000)
    if not text:
        raise ValueError("GOG did not return a game description.")
    slug = product.get("slug", "")
    if not re.fullmatch(r"[a-z0-9_]{1,160}", slug):
        raise ValueError("Invalid GOG product slug")
    images = product.get("images", {})
    cover = art.get("coverVertical") or images.get("logo2x") or images.get("logo")
    background = art.get("galaxyBackgroundImage") or images.get("background") or cover
    artwork = metadata_artwork({context: ["https:" + url if url and url.startswith("//") else url]
                               for context, url in (("cover", cover), ("background", background))})
    card = dict(id="gog-" + reference[4:], name=name, subtitle=clean(description.get("lead"), 200),
                description=text, genre=clean(" · ".join(g.get("name", "") for g in art.get("genres", [])), 100),
                accent="#d9ff82", source_folder="", executable="", available_on_nas=False,
                metadata_url="https://www.gog.com/game/" + slug)
    return dict(card=card, provider_id=reference, **artwork)

def lutris_metadata(operation, value):
    import html, json, re
    from urllib.parse import urlencode
    def clean(text, limit):
        return " ".join(html.unescape(re.sub(r"<[^>]*>", " ", str(text or ""))).split())[:limit]
    if operation not in ("search", "details") or not isinstance(value, str):
        raise ValueError("Invalid Lutris operation")
    if operation == "search" and not value.startswith("lutris:"):
        document = json.loads(metadata_fetch("https://lutris.net/api/games?" + urlencode(dict(search=value)), 2 * 1024 * 1024))
        return [{"app_id": "lutris:" + row["slug"], "name": clean(row.get("name"), 160)}
                for row in document.get("results", [])[:12] if isinstance(row, dict)
                and re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,70}[a-z0-9])?", str(row.get("slug", "")))
                and clean(row.get("name"), 160)]
    reference = metadata_reference(value)
    if not reference.startswith("lutris:"):
        raise ValueError("Expected a Lutris reference")
    slug = reference[7:]
    data = json.loads(metadata_fetch("https://lutris.net/api/games/" + slug, 2 * 1024 * 1024, missing_ok=operation == "search"))
    name = clean(data.get("name"), 160)
    if data.get("slug") != slug or not name:
        if operation == "search": return []
        raise ValueError("Lutris did not return game details.")
    if operation == "search": return [{"app_id": reference, "name": name}]
    text = clean(data.get("description"), 3000)
    if not text:
        raise ValueError("Lutris did not return a game description.")
    cover, background = data.get("coverart"), data.get("banner_url")
    covers = [cover or background]
    image_id = re.fullmatch(r"https://lutris\.net/media/igdb/cover_big/(co[a-zA-Z0-9]+)\.jpg", cover or "")
    if image_id:
        covers.insert(0, "https://images.igdb.com/igdb/image/upload/t_cover_big_2x/" + image_id.group(1) + ".jpg")
    artwork = metadata_artwork({"cover": covers, "background": [background or cover]})
    card = dict(id="lutris-" + slug, name=name, subtitle=clean(data.get("year"), 200),
                description=text, genre=clean(" · ".join(row.get("name", "") for row in data.get("genres", [])), 100),
                accent="#d9ff82", source_folder="", executable="", available_on_nas=False,
                metadata_url="https://lutris.net/games/" + slug + "/")
    return dict(card=card, provider_id=reference, **artwork)

def wikipedia_metadata(operation, value):
    import html, json, re
    from urllib.parse import urlencode, urlsplit, quote, unquote
    def clean(text, limit):
        return " ".join(html.unescape(re.sub(r"<[^>]*>", " ", str(text or ""))).split())[:limit]
    def is_game(description):
        return (re.search(r"(?:video game|computer game|компьютерн.{0,16}игр|видеоигр)", description or "", re.I)
                and not re.search(r"series|franchise|character|серия|персонаж", description or "", re.I))
    if operation not in ("search", "details") or not isinstance(value, str):
        raise ValueError("Invalid Wikipedia operation")
    if operation == "search" and not value.startswith("wikipedia:"):
        link = re.fullmatch(r"https://(en|ru)\.wikipedia\.org/wiki/([^?#]{1,400})", value)
        language = link.group(1) if link else "ru" if re.search(r"[А-Яа-яЁё]", value) else "en"
        if link:
            data = json.loads(metadata_fetch("https://" + language + ".wikipedia.org/api/rest_v1/page/summary/"
                                             + quote(unquote(link.group(2)), safe=""), 2 * 1024 * 1024))
            return ([{"app_id": "wikipedia:" + language + ":" + str(data["pageid"]), "name": clean(data["title"], 160)}]
                    if type(data.get("pageid")) is int and 0 < data["pageid"] < 10**12 and is_game(data.get("description")) else [])
        rows = json.loads(metadata_fetch("https://" + language + ".wikipedia.org/w/rest.php/v1/search/page?"
                                        + urlencode(dict(q=value, limit=12)), 2 * 1024 * 1024)).get("pages", [])
        return [{"app_id": "wikipedia:" + language + ":" + str(row["id"]), "name": clean(row.get("title"), 160)}
                for row in rows if isinstance(row, dict) and type(row.get("id")) is int and 0 < row["id"] < 10**12
                and is_game(row.get("description"))][:12]
    reference = metadata_reference(value)
    if not reference.startswith("wikipedia:"):
        raise ValueError("Expected a Wikipedia reference")
    _, language, page_id = reference.split(":")
    params = dict(action="query", format="json", formatversion=2, pageids=page_id,
                  prop="extracts|pageimages|description", exintro=1, explaintext=1, exchars=1200,
                  piprop="original|thumbnail", pithumbsize=1200, pilicense="any")
    pages = json.loads(metadata_fetch("https://" + language + ".wikipedia.org/w/api.php?" + urlencode(params),
                                     2 * 1024 * 1024)).get("query", {}).get("pages", [])
    page = next((row for row in pages if row.get("pageid") == int(page_id) and row.get("ns") == 0), {})
    name, text = clean(page.get("title"), 160), clean(page.get("extract"), 2400)
    if not name or not text or not is_game(page.get("description")):
        if operation == "search": return []
        raise ValueError("Wikipedia did not return a game description.")
    if operation == "search": return [{"app_id": reference, "name": name}]
    image = page.get("original", {}).get("source", "")
    if not re.search(r"\.(?:jpg|jpeg|png|webp)$", urlsplit(image).path, re.I):
        image = page.get("thumbnail", {}).get("source")
    artwork = metadata_artwork({"cover": [image], "background": [image]})
    url = "https://" + language + ".wikipedia.org/?curid=" + page_id
    card = dict(id="wikipedia-" + language + "-" + page_id, name=name, subtitle="Wikipedia",
                description=text + "\n\nSource: Wikipedia, " + url + " · CC BY-SA 4.0.", genre="",
                accent="#d9ff82", source_folder="", executable="", available_on_nas=False, metadata_url=url)
    return dict(card=card, provider_id=reference, **artwork)

def game_metadata(operation, value):
    import difflib, re, unicodedata
    if operation == "details":
        reference = metadata_reference(value)
        if reference.startswith("epic:"): return epic_metadata(operation, reference)
        if reference.startswith("gog:"): return gog_metadata(operation, reference)
        if reference.startswith("lutris:"): return lutris_metadata(operation, reference)
        if reference.startswith("wikipedia:"): return wikipedia_metadata(operation, reference)
        return steam_metadata(operation, value)
    if (operation != "search" or not isinstance(value, str) or not 1 <= len(value.strip()) <= 160
            or any(ord(c) < 32 for c in value)):
        raise ValueError("The search title must contain 1 to 160 characters.")
    steam_link = re.fullmatch(r"https://store\.steampowered\.com/app/([1-9][0-9]{0,9})(?:/[A-Za-z0-9_-]*)?/?", value.strip())
    if steam_link or re.fullmatch(r"[1-9][0-9]{0,9}", value.strip()):
        return steam_metadata("search", steam_link.group(1) if steam_link else value.strip())
    link = re.fullmatch(r"https://store\.epicgames\.com/(?:[a-z]{2}(?:-[A-Z]{2})?/)?p/([a-z0-9-]+)/?", value.strip())
    if link:
        return epic_metadata("search", "epic:" + link.group(1))
    if value.startswith("gog:"):
        return gog_metadata("search", value)
    link = re.fullmatch(r"https://lutris\.net/games/([a-z0-9](?:[a-z0-9-]{0,70}[a-z0-9])?)/?", value.strip())
    if link or value.startswith("lutris:"):
        return lutris_metadata("search", "lutris:" + link.group(1) if link else value)
    link = re.fullmatch(r"https://(en|ru)\.wikipedia\.org/\?curid=([1-9][0-9]{0,11})", value.strip())
    if link or value.startswith("wikipedia:"):
        return wikipedia_metadata("search", "wikipedia:" + link.group(1) + ":" + link.group(2) if link else value)
    if re.fullmatch(r"https://(?:en|ru)\.wikipedia\.org/wiki/[^?#]{1,400}", value.strip()):
        return wikipedia_metadata("search", value.strip())
    queries, failures, matches = search_queries(value), [], []
    if not queries:
        return []
    numerals = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10"}
    def words(text):
        text = "".join(c for c in unicodedata.normalize("NFKD", text.casefold()) if not unicodedata.combining(c))
        return [numerals.get(w, w) for w in re.findall(r"\w+", text.replace("'", "").replace("’", ""))]
    def plausible(row, query):
        target, candidate = words(query), words(row["name"])
        extra = set(candidate) - set(target)
        # Short titles need a whole first word: Bo matches Bō: Path..., never Box or Bodycam.
        if len(target) == 1 and len(target[0]) <= 2:
            similarity = target == candidate[:1]
        else:
            similarity = (target == candidate[:len(target)]
                          or difflib.SequenceMatcher(None, "".join(target), "".join(candidate)).ratio() >= .5)
        return (not extra.intersection({"soundtrack", "soundtracks", "ost", "dlc", "demo", "upgrade", "artbook"})
                and {w for w in target if w.isdigit()} <= set(candidate)
                and similarity)
    for query in queries:
        try:
            matches = [r for r in steam_metadata("search", query) if plausible(r, query)]
        except (RuntimeError, OSError) as error:
            failures.append(error)
            break
        if matches:
            target = "".join(words(query))
            return sorted(matches, key=lambda r: -difflib.SequenceMatcher(None, target, "".join(words(r["name"]))).ratio())[:12]
    query = queries[-1]
    broad = " ".join(w for w in words(query) if not w.isdigit())
    for gog_query in dict.fromkeys((query, broad)):
        if not gog_query: continue
        try:
            expected = set(words(query))
            matches = [r for r in gog_metadata("search", gog_query) if expected <= set(words(r["name"])) and plausible(r, query)]
            if matches: return matches[:12]
        except (RuntimeError, OSError) as error:
            failures.append(error)
            break
    for query in dict.fromkeys((queries[-1], queries[0])):
        slug = re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,68}[a-z0-9]", slug):
            continue
        try:
            matches = epic_metadata("search", "epic:" + slug)
        except (RuntimeError, OSError) as error:
            failures.append(error)
            continue
        if matches: return matches
    for provider in (lutris_metadata, wikipedia_metadata):
        try:
            matches = [row for row in provider("search", queries[0]) if plausible(row, queries[0])]
            if matches:
                target = "".join(words(queries[0]))
                return sorted(matches, key=lambda row: -difflib.SequenceMatcher(None, target, "".join(words(row["name"]))).ratio())[:12]
        except (RuntimeError, OSError) as error:
            failures.append(error)
    if failures:
        raise RuntimeError("Artwork providers are temporarily unavailable. Search again.") from None
    return []

def relative_game_path(value, allow_empty=False):
    if allow_empty and value == "":
        return value
    if (not isinstance(value, str) or not value or len(value) > 512
            or re.search(r'[\x00-\x1f\\:<>"|?*]', value) or value.startswith("/")
            or any(part in ("", ".", "..") or part.endswith((" ", "."))
                   or re.fullmatch(r"(?i:con|conin\$|conout\$|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])(?:\..*)?", part)
                   for part in value.split("/"))):
        raise ValueError("Invalid relative game path.")
    return value

def asset_path(path):
    if (not isinstance(path, str) or not path.startswith("/assets/ngs_launcher/")
            or urlsplit(path).query or urlsplit(path).fragment or "%" in path
            or unquote(path) != path or "\\" in path or any(ord(c) < 33 for c in path)
            or any(p in ("", ".", "..") for p in path.split("/")[1:])):
        raise ValueError("Image must belong to the NGS asset directory.")
    return path

def validate_card(card):
    if not isinstance(card, dict):
        raise ValueError("Expected a game card.")
    if "kind" in card and card["kind"] != "installer":
        raise ValueError("Invalid catalog kind.")
    if "game_id" in card and (card.get("kind") != "installer" or not isinstance(card["game_id"], str)
                             or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", card["game_id"])):
        raise ValueError("Invalid installer game identity.")
    limits = {"id": 80, "name": 160, "subtitle": 200, "description": 3000,
              "genre": 100, "source_folder": 512, "executable": 512, "metadata_url": 500}
    for key, limit in limits.items():
        value = card.get(key)
        if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 and c != "\n" for c in value):
            raise ValueError("Invalid card field: " + key)
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", card["id"]) or not card["name"].strip():
        raise ValueError("Invalid game identity.")
    if not isinstance(card.get("accent"), str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", card["accent"]):
        raise ValueError("Invalid accent color.")
    available = card.get("available_on_nas", True)
    root = card.get("source_root", "")
    if (not isinstance(root, str) or len(root) > 512 or any(ord(c) < 32 for c in root)
            or (root and (not PureWindowsPath(root).is_absolute() or ".." in PureWindowsPath(root).parts))
            or (root and not available)):
        raise ValueError("Invalid NAS source root")
    if type(available) is not bool or (not available and (card["source_folder"] or card["executable"] or "manifest" in card)):
        raise ValueError("Metadata-only games cannot declare NAS files")
    if "delivery" in card and (card["delivery"] != "on_request" or not available or not card["executable"]):
        raise ValueError("Invalid game delivery")
    if "total_size" in card and (type(card["total_size"]) is not int or not 0 <= card["total_size"] <= 2**50 or not available):
        raise ValueError("Invalid game size")
    relative_game_path(card["source_folder"], allow_empty=not available)
    relative_game_path(card["executable"], allow_empty=True)
    if type(card.get("revision")) is not int or card["revision"] < 1:
        raise ValueError("Invalid card revision.")
    for key, limit in (("cover", MAX_IMAGE), ("background", MAX_IMAGE), ("manifest", 32 * 1024 * 1024)):
        if key == "manifest" and key not in card:
            continue
        image = card.get(key, {})
        if (not isinstance(image, dict) or not isinstance(image.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", image["sha256"])
                or type(image.get("size")) is not int or not 0 < image["size"] <= limit):
            raise ValueError("Invalid image reference.")
        asset_path(image.get("path", ""))
    return {key: card[key] for key in (*limits, "accent", "revision", "cover", "background", "manifest", "available_on_nas", "source_root", "delivery", "total_size", "kind", "game_id") if key in card}

def validate_feed(feed):
    if (not isinstance(feed, dict) or feed.get("schema") != 1
            or feed.get("project") != "ngs_launcher" or not isinstance(feed.get("games"), list)
            or len(feed["games"]) > 1000):
        raise ValueError("Unsupported catalog format.")
    cards = [validate_card(card) for card in feed["games"]]
    if len({card["id"] for card in cards}) != len(cards):
        raise ValueError("Duplicate game identity.")
    return {"schema": 1, "project": "ngs_launcher", "games": cards}

def game_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", value):
        raise ValueError("Invalid game ID.")
    return value

def is_link(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)

def safe_path(root, relative=""):
    root = Path(root)
    if not root.is_absolute() or is_link(root.lstat()):
        raise ValueError("The game root must be a normal folder with an absolute path.")
    if relative:
        relative_game_path(relative)
    target = root
    for component in relative.split("/") if relative else ():
        target = target / component
        try:
            info = target.lstat()
        except FileNotFoundError:
            continue
        if is_link(info):
            raise ValueError("Links and reparse points are not allowed in game files: " + relative)
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError("The path escapes the game folder.")
    return target

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

def validate_transport(transport, document):
    fields = {"schema", "manifest_sha256", "chunks", "signature"}
    if (not isinstance(transport, dict) or set(transport) not in (fields, fields | {"key"})
            or type(transport["schema"]) is not int or transport["schema"] not in (1, 2)
            or not isinstance(transport["signature"], str) or len(transport["signature"]) != 344):
        raise ValueError("Invalid HTTP package description.")
    if "key" in transport:
        key = transport["key"]
        if (not isinstance(key, dict) or set(key) != {"public", "signature"}
                or any(not isinstance(key[k], str) for k in key)
                or len(key["public"]) != 380 or len(key["signature"]) != 344):
            raise ValueError("Invalid HTTP package key certificate.")
    core = {key: value for key, value in document.items() if key != "http"}
    digest = hashlib.sha256(json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf8")).hexdigest()
    if transport["manifest_sha256"] != digest:
        raise ValueError("HTTP packages belong to a different file list.")
    chunks = transport["chunks"]
    if not isinstance(chunks, list) or len(chunks) > 100_000:
        raise ValueError("Invalid HTTP package list.")
    total = 0
    for chunk in chunks:
        compressed = transport["schema"] == 2
        if (not isinstance(chunk, dict) or set(chunk) != ({"path", "size", "sha256", "raw_size", "raw_sha256", "codec"} if compressed else {"path", "size", "sha256"})
                or type(chunk["size"]) is not int or not 0 < chunk["size"] <= PACKAGE_SIZE + (1024 * 1024 if compressed else 0)
                or not isinstance(chunk["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", chunk["sha256"])
                or (not compressed and chunk["path"] != PACKAGE_PREFIX + chunk["sha256"] + ".bin")):
            raise ValueError("Invalid HTTP package.")
        if compressed and (chunk["codec"] != "zstd" or type(chunk["raw_size"]) is not int or not 0 < chunk["raw_size"] <= PACKAGE_SIZE
                or not isinstance(chunk["raw_sha256"], str) or not re.fullmatch("[0-9a-f]{64}", chunk["raw_sha256"])
                or not isinstance(chunk["path"], str) or not re.fullmatch(r"/assets/ngs_launcher/package_jobs/package-[0-9a-f]{32}/chunks/" + chunk["sha256"] + r"\.bin", chunk["path"])):
            raise ValueError("Invalid compressed HTTP package.")
        total += chunk["raw_size"] if compressed else chunk["size"]
    if total != document.get('archive_size', document["total_size"]):
        raise ValueError("HTTP package sizes do not match the game size.")
    return transport

def validate_manifest(document, card=None):
    if not isinstance(document, dict) or type(document.get("schema")) is not int or document["schema"] != 1:
        raise ValueError("Unknown manifest format.")
    identity = game_id(document.get("game_id"))
    source = relative_game_path(document.get("source_folder"))
    executable = relative_game_path(document.get("executable"))
    if not executable.lower().endswith(".exe"):
        raise ValueError("The executable must be an EXE file.")
    cwd = relative_game_path(document.get("working_directory", ""), allow_empty=True)
    arguments = document.get("arguments", [])
    if (not isinstance(arguments, list) or len(arguments) > 64 or any(
            not isinstance(arg, str) or len(arg) > 2048 or "\0" in arg for arg in arguments)):
        raise ValueError("Invalid launch arguments.")
    files, directories = document.get("files"), document.get("directories", [])
    if (not isinstance(files, list) or not files or len(files) > 200_000
            or not isinstance(directories, list) or len(directories) > 200_000):
        raise ValueError("Invalid manifest file list.")
    kinds, normalized, total = {}, [], 0
    for directory in directories:
        relative_game_path(directory)
        key = directory.casefold()
        if key in kinds or key.split("/")[0] == RECEIPT:
            raise ValueError("Manifest path conflict: " + directory)
        kinds[key] = "directory"
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("Invalid file record.")
        path = relative_game_path(item.get("path"))
        size, digest = item.get("size"), item.get("sha256")
        if (type(size) is not int or not 0 <= size <= 2 ** 50 or not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise ValueError("Invalid file size or SHA256.")
        key = path.casefold()
        if key in kinds or key.split("/")[0] == RECEIPT:
            raise ValueError("Manifest path conflict: " + path)
        kinds[key] = "file"
        normalized.append({"path": path, "size": size, "sha256": digest})
        total += size
    for path in kinds:
        parts = path.split("/")
        if any(kinds.get("/".join(parts[:i])) == "file" for i in range(1, len(parts))):
            raise ValueError("A file is used as a folder: " + path)
    if executable not in {item["path"] for item in normalized}:
        raise ValueError("The executable is missing from the manifest.")
    if cwd and cwd.casefold() not in {path for path, kind in kinds.items() if kind == "directory"}:
        raise ValueError("The working directory is missing from the manifest.")
    if type(document.get("total_size")) is not int or document["total_size"] != total:
        raise ValueError("The manifest total size does not match its file list.")
    if card and (identity != card["id"] or source != card["source_folder"] or executable != card["executable"]):
        raise ValueError("The manifest belongs to a different game or executable.")
    result = {"schema": 1, "game_id": identity, "source_folder": source, "executable": executable,
            "working_directory": cwd, "arguments": arguments, "files": normalized,
            "directories": directories, "total_size": total}
    if "http" in document:
        result["http"] = validate_transport(document["http"], result)
    return result
