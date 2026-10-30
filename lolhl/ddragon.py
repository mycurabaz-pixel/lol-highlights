"""Data Dragon: Riot'un herkese açık statik verisi (şampiyon, item, rün adları ve görselleri).
Sürüm başına bir kez indirilip cache/ klasöründe saklanır."""
from __future__ import annotations

import json
from pathlib import Path

import requests

CDN = "https://ddragon.leagueoflegends.com"
CACHE = Path(__file__).resolve().parents[1] / "cache"
_mem: dict[str, object] = {}


def version() -> str:
    if "version" not in _mem:
        try:
            _mem["version"] = requests.get(f"{CDN}/api/versions.json", timeout=10).json()[0]
        except (requests.RequestException, ValueError, IndexError):
            # çevrimdışı: en son indirilen sürüm
            have = sorted(CACHE.glob("ddragon_*_champion.json"))
            if not have:
                raise RuntimeError("Can't reach Data Dragon")
            _mem["version"] = have[-1].name.split("_")[1]
    return _mem["version"]


def _load(name: str) -> dict:
    key = f"{version()}_{name}"
    if key not in _mem:
        f = CACHE / f"ddragon_{key}.json"
        if f.exists():
            _mem[key] = json.loads(f.read_text(encoding="utf-8"))
        else:
            d = requests.get(f"{CDN}/cdn/{version()}/data/en_US/{name}.json", timeout=20).json()
            CACHE.mkdir(exist_ok=True)
            f.write_text(json.dumps(d), encoding="utf-8")
            _mem[key] = d
    return _mem[key]


def champions() -> dict[int, dict]:
    """championId -> {"id": "MonkeyKing", "name": "Wukong"}"""
    return {int(c["key"]): {"id": c["id"], "name": c["name"]} for c in _load("champion")["data"].values()}


def items() -> dict[int, dict]:
    return {int(k): v for k, v in _load("item")["data"].items()}


def runes() -> dict[int, dict]:
    """Rün ve rün ağacı kimliği -> {"name", "icon"}"""
    out = {}
    for tree in _load("runesReforged"):
        out[tree["id"]] = {"name": tree["name"], "icon": tree["icon"]}
        for slot in tree["slots"]:
            for r in slot["runes"]:
                out[r["id"]] = {"name": r["name"], "icon": r["icon"]}
    return out


SHARDS = {5008: "Adaptive Force", 5005: "Attack Speed", 5007: "Ability Haste", 5001: "Health Scaling",
          5010: "Move Speed", 5011: "Health", 5013: "Tenacity and Slow Resist", 5002: "Armor", 5003: "Magic Resist"}


def is_trinket(item_id: int) -> bool:
    return "Trinket" in items().get(item_id, {}).get("tags", [])


def item_kind(item_id: int) -> str | None:
    """'boots', 'legendary' ya da None (bileşen, iksir, totem...)."""
    it = items().get(item_id)
    if not it or not it.get("maps", {}).get("11", False):
        return None
    tags = it.get("tags", [])
    if "Boots" in tags:
        return "boots" if it.get("from") else None  # sade botlar sayılmaz
    if it.get("into") or "Consumable" in tags or "Trinket" in tags:
        return None
    return "legendary" if it.get("gold", {}).get("total", 0) >= 2000 else None
