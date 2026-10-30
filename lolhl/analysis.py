"""Maç analizi verisi: Riot Match-V5 detayı + timeline -> tek model.

Grafikler ve ileride eklenecek koçluk analizleri (dövüş anı, objektif kararları,
kimin focuslanması gerektiği...) yalnızca bu modeli okur. Veri maç bittikten sonra
Riot'un web API'sinden gelir; oyunla hiçbir teması yoktur.

Model iki eksende:
  series  oyuncu başına dakikalık durum: gold, CS, orman CS, XP, seviye, hasar, konum
  events  saniyesi belli olaylar: kill (kimden ne hasar aldığıyla), ejder, baron,
          herald, grubs, atakhan, kule, plaka, inhibitör, ejder ruhu
Ham veri maç klasöründe (riot.json) saklanır; model değişince yeniden üretilir,
API anahtarının süresi dolsa bile.
"""
from __future__ import annotations

import json
from pathlib import Path

VERSION = 1
RAW_FILE = "riot.json"

MONSTERS = {"DRAGON": "dragon", "BARON_NASHOR": "baron", "RIFTHERALD": "herald",
            "HORDE": "grubs", "ATAKHAN": "atakhan"}
DRAGONS = {"FIRE_DRAGON": "fire", "WATER_DRAGON": "ocean", "EARTH_DRAGON": "mountain",
           "AIR_DRAGON": "cloud", "HEXTECH_DRAGON": "hextech", "CHEMTECH_DRAGON": "chemtech",
           "ELDER_DRAGON": "elder"}
LANES = {"TOP_LANE": "top", "MID_LANE": "mid", "BOT_LANE": "bot"}

# Orman kampları: harita konumu, ilk doğma, yeniden doğma (sn). timers.js ile aynı süreler.
# Riot kamp kesimini kaydetmez; dakikalık orman CS'i ve konumdan tahmin edilir.
CAMPS = {
    "blue_100": (3821, 8101, 55, 300), "gromp_100": (2288, 8448, 67, 135),
    "wolves_100": (3783, 6495, 55, 135), "raptors_100": (6996, 5426, 55, 135),
    "red_100": (7772, 4028, 55, 300), "krugs_100": (8394, 2641, 67, 135),
    "blue_200": (10984, 6960, 55, 300), "gromp_200": (12703, 6443, 67, 135),
    "wolves_200": (11008, 8387, 55, 135), "raptors_200": (7852, 9471, 55, 135),
    "red_200": (7101, 10900, 55, 300), "krugs_200": (6447, 12277, 67, 135),
    "scuttle_top": (4400, 9600, 175, 150), "scuttle_bot": (10500, 5100, 175, 150),
}
CAMP_CS = 4          # 26.x'te her kamp (ve epik canavar) 4 orman CS'i sayılıyor
CAMP_REACH = 3500    # dakika başı/sonu konumlarını birleştiren yola bu kadar uzak kamplar aday değil
CLEAR_COST = 1500    # kamp kesme süresinin yol uzunluğu karşılığı (zaman paylaştırmada)


def save_raw(folder: Path, info: dict, timeline: dict, puuid: str) -> None:
    if info and timeline:
        (folder / RAW_FILE).write_text(json.dumps({"puuid": puuid, "match": info, "timeline": timeline}),
                                       encoding="utf-8")


def load_raw(folder: Path) -> dict | None:
    try:
        return json.loads((folder / RAW_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _other(team: int) -> int:
    return 300 - team  # 100 <-> 200


def _participants(info: dict) -> list[dict]:
    out = []
    for p in info["info"]["participants"]:
        out.append({
            "id": p["participantId"], "puuid": p["puuid"],
            "name": p.get("riotIdGameName") or p.get("summonerName", ""),
            "champion": p["championName"], "team": p["teamId"],
            "role": p.get("teamPosition") or p.get("individualPosition") or "",
            "win": p["win"], "level": p.get("champLevel", 0),
            "kda": [p["kills"], p["deaths"], p["assists"]],
            "cs": p.get("totalMinionsKilled", 0) + p.get("neutralMinionsKilled", 0),
            "gold": p.get("goldEarned", 0),
            "damage": p.get("totalDamageDealtToChampions", 0),
            "damage_types": {"physical": p.get("physicalDamageDealtToChampions", 0),
                             "magic": p.get("magicDamageDealtToChampions", 0),
                             "true": p.get("trueDamageDealtToChampions", 0)},
            "taken": p.get("totalDamageTaken", 0),
            "mitigated": p.get("damageSelfMitigated", 0),
            "objective_damage": p.get("damageDealtToObjectives", 0),
            "vision": p.get("visionScore", 0),
            "items": [p.get(f"item{i}", 0) for i in range(7)],
        })
    return out


def _series(frames: list[dict], ids: list[int]) -> dict[str, dict[str, list]]:
    out = {}
    for pid in ids:
        rows = [f["participantFrames"].get(str(pid), {}) for f in frames]
        dmg = lambda r, k: r.get("damageStats", {}).get(k, 0)  # noqa: E731
        out[str(pid)] = {
            "gold": [r.get("totalGold", 0) for r in rows],
            "cs": [r.get("minionsKilled", 0) + r.get("jungleMinionsKilled", 0) for r in rows],
            "jungle_cs": [r.get("jungleMinionsKilled", 0) for r in rows],
            "xp": [r.get("xp", 0) for r in rows],
            "level": [r.get("level", 1) for r in rows],
            "damage": [dmg(r, "totalDamageDoneToChampions") for r in rows],
            "taken": [dmg(r, "totalDamageTaken") for r in rows],
            "x": [r.get("position", {}).get("x") for r in rows],
            "y": [r.get("position", {}).get("y") for r in rows],
        }
    return out


def _events(frames: list[dict], team_of: dict[int, int]) -> list[dict]:
    out = []
    for f in frames:
        for e in f["events"]:
            t = round(e["timestamp"] / 1000, 2)
            pos = e.get("position") or {}
            base = {"t": t, "x": pos.get("x"), "y": pos.get("y")}
            kind = e["type"]
            if kind == "CHAMPION_KILL":
                victim = e.get("victimId")
                out.append({**base, "type": "kill", "killer": e.get("killerId") or None, "victim": victim,
                            "assists": e.get("assistingParticipantIds", []),
                            "team": _other(team_of[victim]) if victim in team_of else None,
                            "bounty": e.get("bounty", 0), "shutdown": e.get("shutdownBounty", 0),
                            # ölen oyuncunun son saniyelerde kimden hangi yetenekle ne hasar aldığı
                            "damage_received": [{
                                "from": d.get("participantId") or None, "spell": d.get("spellName", ""),
                                "slot": d.get("spellSlot"), "physical": d.get("physicalDamage", 0),
                                "magic": d.get("magicDamage", 0), "true": d.get("trueDamage", 0),
                            } for d in e.get("victimDamageReceived", [])]})
            elif kind == "ELITE_MONSTER_KILL":
                out.append({**base, "type": MONSTERS.get(e.get("monsterType"), e.get("monsterType", "").lower()),
                            "sub": DRAGONS.get(e.get("monsterSubType")), "team": e.get("killerTeamId"),
                            "killer": e.get("killerId") or None})
            elif kind == "BUILDING_KILL":
                lost = e.get("teamId")  # binayı kaybeden takım
                out.append({**base, "type": "inhibitor" if e.get("buildingType") == "INHIBITOR_BUILDING" else "tower",
                            "lane": LANES.get(e.get("laneType")), "tier": e.get("towerType"),
                            "team": _other(lost) if lost else None, "killer": e.get("killerId") or None})
            elif kind == "TURRET_PLATE_DESTROYED":
                lost = e.get("teamId")
                out.append({**base, "type": "plate", "lane": LANES.get(e.get("laneType")),
                            "team": _other(lost) if lost else None, "killer": e.get("killerId") or None})
            elif kind == "DRAGON_SOUL_GIVEN":
                # takımsız (teamId 0) gelirse ruh değil, haritanın o ejder tipine dönüşmesidir
                team = e.get("teamId") or None
                out.append({**base, "type": "soul" if team else "rift", "sub": (e.get("name") or "").lower(),
                            "team": team})
            elif kind == "CHAMPION_SPECIAL_KILL":
                out.append({**base, "type": "special", "kind": e.get("killType"),
                            "multi": e.get("multiKillLength"), "killer": e.get("killerId") or None})
    return out


def _dist(a, b) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def _seg_dist(p, a, b) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    n = dx * dx + dy * dy
    s = 0 if n == 0 else max(0, min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / n))
    return _dist(p, (a[0] + s * dx, a[1] + s * dy))


def _camp_kills(frames: list[dict], ids: list[int], events: list[dict]) -> list[dict]:
    """Kamp kesimlerini tahmin eder (yaklaşık, ±30 sn). Her oyuncunun her dakikada
    kaç kamp kestiği orman CS artışından (epik canavarlar düşülerek) çıkar; hangi kamplar
    olduğu, o dakikanın başındaki ve sonundaki konumu birleştiren en kısa yoldan seçilir.
    Kamp o anda doğmuş olmalı; kesilen kamp yeniden doğana kadar başkasına verilmez."""
    from itertools import permutations
    spawn = {c: v[2] for c, v in CAMPS.items()}  # kampın bir sonraki doğma anı
    out = []
    for i in range(1, len(frames)):
        t0, t1 = frames[i - 1]["timestamp"] / 1000, frames[i]["timestamp"] / 1000
        for pid in ids:
            f0, f1 = frames[i - 1]["participantFrames"].get(str(pid), {}), frames[i]["participantFrames"].get(str(pid), {})
            a, b = f0.get("position"), f1.get("position")
            if not a or not b:
                continue
            epics = sum(1 for e in events if e.get("killer") == pid and t0 < e["t"] <= t1
                        and e["type"] in ("dragon", "baron", "herald", "grubs", "atakhan"))
            k = round(max(0, f1.get("jungleMinionsKilled", 0) - f0.get("jungleMinionsKilled", 0) - epics * CAMP_CS) / CAMP_CS)
            if not k:
                continue
            a, b = (a["x"], a["y"]), (b["x"], b["y"])
            cands = sorted((c for c, v in CAMPS.items() if spawn[c] < t1 and _seg_dist(v[:2], a, b) < CAMP_REACH),
                           key=lambda c: _seg_dist(CAMPS[c][:2], a, b))[:7]

            def length(route):
                pts = [a, *(CAMPS[c][:2] for c in route), b]
                return sum(_dist(p, q) for p, q in zip(pts, pts[1:]))
            route = min(permutations(cands, min(k, len(cands))), key=length, default=())
            # dakikanın zamanını yol + kesim süresine göre paylaştır
            total, walked, prev = length(route) + CLEAR_COST * len(route), 0.0, a
            for c in route:
                walked += _dist(prev, CAMPS[c][:2]) + CLEAR_COST
                prev = CAMPS[c][:2]
                t = round(max(t0 + (t1 - t0) * walked / total if total else t1, spawn[c] + 10), 1)
                out.append({"t": t, "type": "camp", "camp": c, "killer": pid, "estimated": True})
                spawn[c] = t + CAMPS[c][3]
    return sorted(out, key=lambda e: e["t"])


def build(info: dict, timeline: dict, puuid: str) -> dict:
    parts = _participants(info)
    me = next((p for p in parts if p["puuid"] == puuid), None)
    if me is None:
        raise ValueError("Your account is not in this match")
    # karşı koridordaki rakip: diğer takımda aynı roldeki oyuncu
    opp = next((p for p in parts if me["role"] and p["team"] != me["team"] and p["role"] == me["role"]), None)
    frames = timeline["info"]["frames"]
    i = info["info"]
    events = _events(frames, {p["id"]: p["team"] for p in parts})
    return {
        "version": VERSION,
        "match_id": info["metadata"]["matchId"],
        "duration": i["gameDuration"],
        "game_version": i.get("gameVersion", ""),
        "queue": i.get("queueId"),
        "me": me["id"], "opponent": opp["id"] if opp else None, "my_team": me["team"],
        "participants": parts,
        "t": [round(f["timestamp"] / 1000, 1) for f in frames],
        "series": _series(frames, [p["id"] for p in parts]),
        "events": events,
        "camps": _camp_kills(frames, [p["id"] for p in parts], events),
    }
