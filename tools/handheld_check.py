"""Offline UI measurements in disposable portable storage; never starts a game."""

import argparse
import ctypes
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch


def memory_mib():
    from ctypes import wintypes
    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in
            ("peak", "working", "paged_peak", "paged", "nonpaged_peak", "nonpaged", "pagefile", "pagefile_peak")]
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    kernel.K32GetProcessMemoryInfo.restype = wintypes.BOOL
    if not kernel.K32GetProcessMemoryInfo(ctypes.c_void_p(-1), ctypes.byref(counters), counters.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    return round(counters.working / 1024 ** 2, 2)


def percentile(values, percent):
    if not values:
        return None
    return round(sorted(values)[min(len(values) - 1, int((len(values) - 1) * percent))], 3)


def seed_catalog(repo, storage, count):
    from ngs.catalog import CatalogCache, MAX_FEED, validate_feed
    cache = CatalogCache(storage, "http://127.0.0.1:9/tactic")
    # Use cached public artwork when available; never read credentials or contact the server.
    feeds = sorted((repo / "data/cache/catalog").glob("*.json"))
    source = None
    for path in feeds:
        try:
            if not path.resolve().is_relative_to(repo) or path.stat().st_size > MAX_FEED:
                continue
            feed = validate_feed(json.loads(path.read_bytes()))
            if any(card.get("kind") == "installer" for card in feed["games"]):
                continue
            def available(image):
                artwork = path.parent / (image["sha256"] + ".image")
                return (artwork.resolve().is_relative_to(repo / "data/cache")
                        and artwork.is_file() and artwork.stat().st_size == image["size"])
            if feed["games"] and all(available(card[key]) for card in feed["games"] for key in ("cover", "background")):
                source = (feed["games"], path.parent)
                break
        except (ValueError, OSError, KeyError):
            continue
    if source is None:
        card = json.loads((repo / "examples/black-mesa.json").read_text(encoding="utf8"))
        raw = (repo / "assets/orbital.svg").read_bytes()
        image = dict(path="/assets/ngs_launcher/check.svg", size=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        card.update(cover=image, background=image, revision=1)
        (cache.folder / (image["sha256"] + ".image")).write_bytes(raw)
        cards, artwork = [card], "demo"
    else:
        cards, folder = source
        for digest in {card[key]["sha256"] for card in cards for key in ("cover", "background")}:
            shutil.copyfile(folder / (digest + ".image"), cache.folder / (digest + ".image"))
        artwork = "cached"
    games = [dict(cards[i % len(cards)], id="check-%04d" % i, available_on_nas=True,
                  source_folder="fixture", source_root="", description=cards[i % len(cards)]["description"][:300])
             for i in range(count)]
    feed = validate_feed(dict(schema=1, project="ngs_launcher", games=games))
    raw = json.dumps(feed, ensure_ascii=False, separators=(",", ":")).encode("utf8")
    if len(raw) > MAX_FEED:
        raise ValueError("Fixture exceeds the catalog size limit")
    cache.index.write_bytes(raw)
    return dict(artwork=artwork, distinct_games=len(cards), repeated_to=count)


def main():
    parser = argparse.ArgumentParser(description="Offline NGS handheld performance check (Windows)")
    parser.add_argument("--games", type=int, default=1000, choices=range(5, 1001), metavar="5..1000")
    parser.add_argument("--seconds", type=int, default=60, choices=range(5, 121), metavar="5..120")
    parser.add_argument("--output", type=Path, help="Report inside the NGS folder")
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("Windows is required for process memory measurements")
    repo = Path(__file__).resolve().parents[1]
    output = (args.output or repo / "data/logs/handheld-check.json").resolve()
    if not output.is_relative_to(repo) or output.suffix != ".json":
        parser.error("The JSON report must stay inside the NGS folder")
    from ngs.portable import PortableStorage
    temporary_root = (repo / "data/tmp").resolve()
    if not temporary_root.is_relative_to(repo):
        parser.error("The temporary directory must stay inside the NGS folder")
    temporary_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="handheld-", dir=temporary_root) as folder:
        root = Path(folder)
        for name in ("qml", "assets"):
            shutil.copytree(repo / name, root / name)
        storage = PortableStorage(root)
        storage.configure_process()
        settings = storage.read_settings()
        settings["tactic_url"] = "http://127.0.0.1:9/tactic"
        storage.write_settings(settings)
        fixture = seed_catalog(repo, storage, args.games)
        started = time.perf_counter()
        from PySide6.QtCore import QObject, Qt, QEvent, QSysInfo, qVersion
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtTest import QTest
        from ngs.app import create_window
        app = QGuiApplication([])
        frames = []
        with patch("ngs.catalog.CatalogCache.refresh", side_effect=ConnectionError("Offline check")):
            engine, window = create_window(storage)
            if len(engine.bridge.games) != args.games:
                raise RuntimeError("The offline fixture did not load completely")
            engine.gamepad.set_active(False)
            window.frameSwapped.connect(lambda: frames.append(time.perf_counter()), Qt.ConnectionType.QueuedConnection)
            window.showFullScreen()
            try:
                limit = time.perf_counter() + 10
                while not frames and time.perf_counter() < limit:
                    QTest.qWait(10)
                if not frames or not window.isExposed():
                    raise RuntimeError("No exposed window/frame; run on an interactive Windows desktop")
                first_frame_ms = round((frames[0] - started) * 1000, 1)
                QTest.qWait(1500)
                screen = window.screen()
                result = dict(schema=1, platform=QSysInfo.prettyProductName(), qt=qVersion(),
                              logical_cpus=os.cpu_count(), fixture=fixture, first_frame_ms=first_frame_ms,
                              screen=dict(width=screen.size().width(), height=screen.size().height(),
                                          dpr=screen.devicePixelRatio(), refresh_hz=screen.refreshRate()),
                              graphics_api=str(window.rendererInterface().graphicsApi()), phases={})

                def phase(name, duration):
                    print("Checking %s (%ss)..." % (name, duration), flush=True)
                    start, cpu, first = time.perf_counter(), time.process_time(), len(frames)
                    rss = []
                    while time.perf_counter() - start < duration:
                        QTest.qWait(250)
                        rss.append(memory_mib())
                    elapsed = time.perf_counter() - start
                    result["phases"][name] = dict(seconds=round(elapsed, 2),
                        cpu_total_percent=round((time.process_time() - cpu) / elapsed / (os.cpu_count() or 1) * 100, 3),
                        working_set_mib=max(rss), frames=len(frames) - first,
                        exposed=window.isExposed(), visibility=str(window.visibility()))

                phase("idle", args.seconds)
                frames.clear()
                latency, dispatch = [], []
                for i in range(70):
                    before = time.perf_counter()
                    engine.bridge.action.emit("right" if (i // 20) % 2 == 0 else "left")
                    dispatch.append((time.perf_counter() - before) * 1000)
                    first = next((stamp for stamp in frames if stamp >= before), None)
                    limit = before + .12
                    while first is None and time.perf_counter() < limit:
                        QTest.qWait(1)
                        first = next((stamp for stamp in frames if stamp >= before), None)
                    if first is not None:
                        latency.append((first - before) * 1000)
                    QTest.qWait(max(1, int((before + .13 - time.perf_counter()) * 1000)))
                gaps = [(b - a) * 1000 for a, b in zip(frames, frames[1:])]
                result["navigation"] = dict(frames=len(frames), frame_gap_p95_ms=percentile(gaps, .95),
                    dispatch_p95_ms=percentile(dispatch, .95), next_frame_p95_ms=percentile(latency, .95),
                    next_frame_samples=len(latency), working_set_mib=memory_mib())
                window.setProperty("selectedIndex", args.games - 1)
                QTest.qWait(1200)
                backdrop = window.findChild(QObject, "gameBackdrop")
                result["last_card"] = dict(index=window.property("selectedIndex"), pan=backdrop.property("pan"))
                output.parent.mkdir(parents=True, exist_ok=True)
                window.grabWindow().save(str(output.with_suffix(".png")))
                window.showMinimized()
                QTest.qWait(1500)
                phase("minimized", max(5, args.seconds // 4))
                window.showNormal()
                window.showFullScreen()
                QTest.qWait(1000)
                engine.bridge.gameStarted.emit()
                QTest.qWait(500)
                if not window.isVisible() or window.findChild(QObject, "launcherScene").property("visible"):
                    raise RuntimeError("Expected a visible static game background with the catalog disabled")
                phase("game_background", max(5, args.seconds // 4))
                restored, previous = time.perf_counter(), len(frames)
                engine.bridge.gameReturned.emit()
                while len(frames) == previous and time.perf_counter() - restored < 5:
                    QTest.qWait(10)
                result["restore"] = dict(frames=len(frames) - previous, exposed=window.isExposed(),
                    visibility=str(window.visibility()), initialized=window.isSceneGraphInitialized(),
                    scene_visible=window.findChild(QObject, "launcherScene").property("visible"))
                result["restore_first_frame_ms"] = (round((frames[previous] - restored) * 1000, 1)
                                                    if len(frames) > previous else None)
                result["physical_checks"] = "Not performed: physical controller/touch/sleep/VPN/game return"
                result["measurement_limits"] = [
                    "Startup starts before Qt imports; fixture preparation/process creation are excluded",
                    "Frame timings are GUI-thread delivery times of queued frameSwapped signals, not GPU/presentation timings",
                    "Artwork is repeated from the cache; descriptions are limited to 300 characters",
                    "No download, NAS, real game or server request is involved",
                ]
                output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf8")
                if (len(frames) == previous or window.visibility() != window.Visibility.FullScreen
                        or not result["restore"]["scene_visible"]):
                    raise RuntimeError("Fullscreen did not render after returning from the game background; see the report")
                QTest.qWait(650)
                cards = window.findChild(QObject, "gameCards")
                item = cards.property("currentItem")
                result["restore"]["selection_visible"] = (item is not None and item.property("index") == args.games - 1
                    and item.property("x") >= cards.property("contentX") - 2
                    and item.property("x") + item.property("width") <= cards.property("contentX") + cards.property("width") + 2)
                output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf8")
                print(json.dumps(result, ensure_ascii=False, indent=2))
                if not result["restore"]["selection_visible"]:
                    raise RuntimeError("The selected card is outside the viewport after restoring")
                window.grabWindow().save(str(output.with_name(output.stem + "-restored.png")))
            finally:
                engine.bridge._closing = True
                engine.gamepad.set_active(False)
                engine.bridge.stop()
                engine.bridge.download_queue.close()
                window.hide()
                engine.deleteLater()
                app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                app.processEvents()
                app.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
