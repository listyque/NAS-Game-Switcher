"""Build one published game's manifest by reading its NAS folder, without changing it."""

import argparse
import json
import threading
import time
from pathlib import Path

from ngs.catalog import CatalogCache, MAX_FEED, atomic_write, validate_feed
from ngs.install import build_manifest, game_id, manifest_bytes
from ngs.portable import PortableStorage, game_source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game", required=True, help="Published NGS game code")
    parser.add_argument("--working-directory", default="", help="Relative folder inside the game; default is its root")
    parser.add_argument("--arg", action="append", default=[], help="One executable argument; use --arg=-option")
    args = parser.parse_args()
    identity = game_id(args.game)
    storage = PortableStorage(Path(__file__).resolve().parents[1])
    storage.configure_process()
    settings = storage.read_settings()
    cache = CatalogCache(storage, settings["tactic_url"])
    cancel = threading.Event()
    feed = validate_feed(json.loads(cache.fetch("/assets/ngs_launcher/catalog.json", MAX_FEED, cancel, time.monotonic() + 45)))
    card = next((card for card in feed["games"] if card["id"] == identity), None)
    if card is None:
        parser.error("Game is not in the published NGS catalog")
    last = 0
    def progress(phase, done, total, path):
        nonlocal last
        now = time.monotonic()
        if now - last >= 2:
            print("SHA256: %.1f%%" % (100 * done / max(1, total)), flush=True)
            last = now
    manifest = build_manifest(Path(game_source(settings, card)), card, cancel, progress,
                              arguments=args.arg, working_directory=args.working_directory)
    output = storage.temp / (identity + "-manifest.json")
    atomic_write(output, manifest_bytes(manifest))
    print(json.dumps({"game": identity, "files": len(manifest["files"]), "bytes": manifest["total_size"],
                      "manifest": str(output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
