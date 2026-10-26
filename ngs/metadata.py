"""Bounded store and community metadata readers shared with the owned TACTIC script."""


METADATA_REVISION = 13


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


def metadata_fetch(url, limit, missing_ok=False, timeout=12, cancel=None):
    import sys, time, types, threading
    from collections import OrderedDict
    import urllib.error
    import urllib.request
    from urllib.parse import urlsplit
    context = metadata_fetch.__dict__.get('context')
    cancel = cancel if cancel is not None else getattr(context, 'cancel', None)
    if cancel is not None and cancel.is_set(): raise InterruptedError('Metadata request cancelled')
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
        if cancel is not None and cancel.is_set(): raise InterruptedError('Metadata request cancelled')
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
    context = metadata_fetch.__dict__.get('context')
    cancel = getattr(context, 'cancel', None)
    sources = {context: tuple(dict.fromkeys(url for url in urls if url)) for context, urls in sources.items()}
    if any(not urls for urls in sources.values()):
        raise RuntimeError("The provider returned no game images.")
    def download(urls):
        for index, url in enumerate(urls):
            try:
                options = dict(cancel=cancel) if cancel is not None else {}
                data = metadata_fetch(url, 8 * 1024 * 1024, timeout=4 if index + 1 < len(urls) else 12, **options)
                return {"base64": base64.b64encode(data).decode("ascii"), "sha256": hashlib.sha256(data).hexdigest()}
            except (RuntimeError, OSError):
                continue
        raise RuntimeError("The artwork provider did not return an image. Try downloading again.")
    # Only network reads run in these threads; TACTIC writes stay on its request thread.
    unique = list(dict.fromkeys(sources.values()))
    with ThreadPoolExecutor(max_workers=2) as pool:
        images = dict(zip(unique, pool.map(download, unique)))
    return {context: images[urls] for context, urls in sources.items()}


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
    context = metadata_fetch.__dict__.get('context')
    language = getattr(context, 'language', 'russian')
    query = urlencode({"appids": app_id, "l": "english" if operation == "search" else language, "cc": "US"})
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


def search_title(value):
    import re
    value = re.sub(r"(?i)^\s*(?:\[(?:dixen18|xatab|fitgirl|dodi|elamigos|dl|p|l|gog|steam|repack|portable)\]\s*)+", "", value)
    value = re.sub(r"(?i)(?:\s*\[(?:p|l|dl|rus|eng|multi\d*|repack|portable|gog|steam)\b[^\]]*\])+\s*$", "", value)
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
    link = re.fullmatch(r"https://(?:www\.)?gog\.com/(?:[a-z]{2}/)?game/([a-z0-9_]+)/?", value.strip())
    if link:
        return gog_metadata("search", link.group(1).replace('_', ' '))
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


def resolve_remote(client, operation, value):
    if operation not in ("search", "resolve"):
        raise ValueError("Invalid metadata operation")
    result = client.call("execute_python_script", {"script_path": "ngs/metadata",
                         "kwargs": {"operation": operation, "value": value}}, timeout=90)
    if not isinstance(result, dict) or "info" not in result:
        raise RuntimeError("The NGS metadata handler is not installed or returned an invalid response.")
    return result["info"]


def custom_metadata(storage, name, description, cover, background, seed, cancel):
    import hashlib, uuid
    from ngs.catalog import ArtworkCache, validate_card
    from ngs.catalogizer_ui import custom_artwork
    from ngs.install import check_cancel
    from ngs.i18n import tr
    check_cancel(cancel)
    if not name.strip() or len(name) > 160 or len(description) > 3000:
        raise ValueError(tr('Enter a title up to 160 characters and a description up to 3,000 characters.'))
    card = dict(id=seed['id'] if seed.get('id', '').startswith('custom-') else 'custom-' + uuid.uuid4().hex,
                name=name.strip(), description=description, subtitle='', genre=seed.get('genre', ''),
                accent=seed.get('accent', '#419964'), source_folder='', executable='', metadata_url='',
                available_on_nas=False, revision=1)
    cache, images = ArtworkCache(storage), {}
    for kind, source in (('cover', cover), ('background', background)):
        check_cancel(cancel)
        if source:
            data = custom_artwork(source, kind)
            digest = hashlib.sha256(data).hexdigest()
            card[kind] = dict(path='/assets/ngs_launcher/local/' + digest + '.image', size=len(data), sha256=digest)
            images[kind] = data
        elif seed.get(kind) and cache.image_valid(seed[kind]):
            card[kind] = seed[kind]
        elif kind == 'background':
            card[kind] = card['cover']
        else:
            raise ValueError(tr('Choose a cover on this device.'))
    card = validate_card(card)
    for kind, data in images.items():
        check_cancel(cancel)
        cache.cache_image(card[kind], data)
    check_cancel(cancel)
    return card


def provider_metadata(storage, operation, value, cancel, progress=None):
    import base64, hashlib, threading
    from ngs.catalog import ArtworkCache, MAX_IMAGE, validate_card
    from ngs.install import check_cancel
    from ngs.i18n import tr
    check_cancel(cancel)
    if operation not in ('search', 'resolve'):
        raise ValueError('Invalid metadata operation')
    if progress: progress('metadata_provider', 0, 1, tr('Searching game providers…') if operation == 'search' else tr('Loading description and artwork…'))
    context = metadata_fetch.__dict__.setdefault('context', threading.local())
    previous = getattr(context, 'cancel', None)
    previous_language = getattr(context, 'language', 'russian')
    context.cancel = cancel
    context.language = 'russian' if storage.read_settings().get('language') == 'ru' else 'english'
    try:
        result = game_metadata('details' if operation == 'resolve' else 'search', value)
    finally:
        context.cancel = previous
        context.language = previous_language
    check_cancel(cancel)
    if operation == 'search': return result
    card = dict(result['card'], revision=1)
    images = {}
    for kind in ('cover', 'background'):
        image = result[kind]
        encoded = image.get('base64', '')
        if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_IMAGE + 2) // 3):
            raise ValueError('Artwork exceeds its size limit')
        data = base64.b64decode(encoded, validate=True)
        digest = hashlib.sha256(data).hexdigest()
        if image.get('sha256') != digest:
            raise ValueError('Artwork checksum mismatch')
        card[kind] = dict(path='/assets/ngs_launcher/local/' + digest + '.image', size=len(data), sha256=digest)
        images[kind] = data
    card = validate_card(card)
    cache = ArtworkCache(storage)
    for kind, data in images.items():
        check_cancel(cancel)
        cache.cache_image(card[kind], data)
    check_cancel(cancel)
    return dict(card=card, provider_id=result['provider_id'])


def client_metadata(storage, base_url, operation, value, cancel, progress=None, *, offline=False):
    import re, hashlib, json, time
    from ngs.api import MetadataApi
    from ngs.catalog import CatalogCache, validate_card
    from ngs.install import check_cancel, safe_path
    from ngs.catalog import atomic_write
    from ngs.i18n import tr
    check_cancel(cancel)
    local = not base_url
    def validate(result):
        if operation == "resolve":
            return dict(result, card=validate_card(result["card"]))
        if (not isinstance(result, list) or len(result) > 12 or any(not isinstance(row, dict)
                or set(row) != {"app_id", "name"} or not isinstance(row["app_id"], str)
                or not isinstance(row["name"], str) or not 1 <= len(row["name"]) <= 160 for row in result)):
            raise ValueError("Invalid NGS search results")
        for row in result:
            metadata_reference(row["app_id"])
        return result
    source = ('providers:ru' if storage.read_settings().get('language') == 'ru' else 'providers:en') if local else base_url.rstrip('/')
    key = hashlib.sha256(json.dumps([source, operation, value, METADATA_REVISION]).encode("utf8")).hexdigest()
    cached = safe_path(storage.cache, "local-metadata-" + key + ".json")
    result, stale = None, None
    try:
        if cached.is_file() and cached.stat().st_size <= 128 * 1024:
            document = json.loads(cached.read_bytes())
            stale = validate(document["result"])
            if 0 <= time.time() - document["saved"] < 900:
                result = stale
    except (ValueError, OSError, KeyError, TypeError):
        pass
    if local and operation == 'resolve' and result is not None and not offline:
        from ngs.catalog import ArtworkCache
        cache = ArtworkCache(storage)
        if any(not cache.image_valid(result['card'][key]) for key in ('cover', 'background')):
            result = None
    if result is None:
        if offline:
            if stale is None:
                raise ConnectionError(tr("No cached artwork yet. Connect to the Internet to download it.") if local else "No cached artwork for this search yet. Add the game without it; search becomes available when connected to the server.")
            result = stale
    if result is None:
        if progress and not local:
            progress("metadata_server", 0, 0, "The server is searching for the game…" if operation == "search"
                     else "The server is fetching description and artwork from the provider…")
        try:
            result = validate(provider_metadata(storage, operation, value, cancel, progress) if local
                              else MetadataApi(storage, base_url).call(operation, value))
        except Exception as error:
            check_cancel(cancel)
            from ngs.http_retry import transient_network_error
            if not transient_network_error(error) and not (local and isinstance(error, RuntimeError)): raise
            if stale is None:
                raise ConnectionError(tr("Game providers are unavailable. Your local games remain playable; try artwork search again when connected.") if local else "The artwork server is offline. Add the game without artwork and search again when connected.") from None
            result, offline = stale, True
        if result and not offline:
            atomic_write(cached, json.dumps(dict(saved=time.time(), result=result), ensure_ascii=False).encode("utf8"))
    check_cancel(cancel)
    if operation == "resolve":
        card = validate_card(result["card"])
        if progress and not local:
            progress("metadata_artwork", 0, 2, "Description received. Loading images from NAS…")
        if not offline and not local:
            try:
                CatalogCache(storage, base_url).cache_artwork(card, cancel, progress=progress)
            except Exception as error:
                from ngs.http_retry import transient_network_error
                if not transient_network_error(error): raise
                offline = True
        if local:
            return dict(result, card=card, message=tr("Cached artwork · offline.") if offline else tr("Description and images saved on this device."))
        return dict(result, card=card, message="Cached artwork · server offline." if offline
                    else "Description and images cached on your server.")
    return result
