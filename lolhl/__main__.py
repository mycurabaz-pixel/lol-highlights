"""Kullanım:
  python -m lolhl watch                       arka planda maçları izle
  python -m lolhl process VIDEO [--match ID] [--offset S]
                                               kaydedilmiş bir videoyu tekrar işle
"""
from __future__ import annotations

import argparse
import json
import tomllib
from pathlib import Path

from .riot_api import RiotApi
from .pipeline import process


def load_config(path: str) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def main() -> None:
    ap = argparse.ArgumentParser(prog="lolhl")
    ap.add_argument("--config", default="config.toml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("watch")
    p = sub.add_parser("process")
    p.add_argument("video")
    p.add_argument("--match", help="e.g. TR1_1234567890 (otherwise the .json next to the video)")
    p.add_argument("--offset", type=float, help="video_time - game_time (seconds)")
    args = ap.parse_args()
    cfg = load_config(args.config)

    if args.cmd == "watch":
        from .watcher import run
        run(cfg)
        return

    sidecar = Path(args.video).with_suffix(".json")
    meta = json.loads(sidecar.read_text()) if sidecar.exists() else {}
    match_id = args.match or meta.get("match_id")
    offset = args.offset if args.offset is not None else meta.get("offset")
    if match_id is None or offset is None:
        ap.error("--match and --offset are required (no .json next to the video)")

    api = RiotApi(cfg["riot"]["api_key"], cfg["riot"]["platform"])
    puuid = api.puuid(cfg["riot"]["game_name"], cfg["riot"]["tag_line"])
    process(api.load(match_id, puuid), args.video, offset, cfg)


if __name__ == "__main__":
    main()
