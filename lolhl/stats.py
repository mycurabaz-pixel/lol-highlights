"""Rün / item önerileri için istatistik veritabanı ve toplayıcı.

Riot API öneri vermez; Blitz/Porofessor gibi biz de maç verisinden çıkarırız. Toplayıcı
uygulama açıkken arka planda Master+ oyuncuların son dereceli maçlarını Match-V5'ten
çeker (istek sınırının bir payını ön plana bırakarak) ve her oyuncudan rün sayfasını,
bitmiş itemlerini, rolünü ve sonucunu stats.db'ye yazar. Oyuncu bilgisi ekranında
çekilen maçlar da buraya eklenir. Öneriler yalnızca şampiyon seçiminde, oyun başlamadan
verilir; oyun sırasında öneri yoktur.
"""
from __future__ import annotations

import json
import random
import sqlite3
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path

from . import ddragon
from .riot_api import REGIONAL, KeyExpired, RiotApi

DB = Path(__file__).resolve().parents[1] / "stats.db"
# Toplanan bölgeler. Match-V5 istekleri bölgesel kümeye gider (TR ve EUW ikisi de "europe"),
# Riot'un sınırı küme başına ayrı olduğu için her küme kendi iş parçacığında toplanır.
CLUSTERS = {"europe": ("TR1", "EUW1"), "asia": ("KR",)}
REGION_NAMES = {"TR1": "TR", "EUW1": "EUW", "KR": "KR"}
START_WINDOW = 60_000  # ilk 60 sn'de alınanlar başlangıç itemi sayılır (ms)
ROLES = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")
SCOUTING = threading.Event()  # oyuncu bilgisi yüklenirken toplayıcı bekler
MIN_GAMES = 8        # bu kadar maçtan azsa öneri "az veri" uyarısıyla gösterilir
MIN_MATCHUP = 5      # eşleşme önerisi için eşik (eşleşme verisi çok daha seyrek)
_lock = threading.Lock()


def _cluster_of(platform: str) -> str:
    return REGIONAL[platform.upper()]


def _db() -> sqlite3.Connection:
    c = sqlite3.connect(DB, timeout=10)
    c.executescript("""
        CREATE TABLE IF NOT EXISTS matches (id TEXT PRIMARY KEY, patch TEXT, queue INT, created REAL);
        CREATE TABLE IF NOT EXISTS parts (match_id TEXT, puuid TEXT, champion INT, role TEXT, win INT,
            runes TEXT, items TEXT, opponent INT);
        CREATE INDEX IF NOT EXISTS parts_champ ON parts (champion, role);
        CREATE INDEX IF NOT EXISTS parts_puuid ON parts (puuid);
    """)
    # source: 'apex' = toplayıcının Master+ maçları (öneriler bunlardan), 'scout' = oyuncu bilgisi için çekilenler
    if "source" not in [r[1] for r in c.execute("PRAGMA table_info(matches)")]:
        c.execute("ALTER TABLE matches ADD COLUMN source TEXT")
    # timeline'dan: başlangıç itemleri ve satın alma sırası (timeline'ı olmayan eski maçlarda boş)
    cols = [r[1] for r in c.execute("PRAGMA table_info(parts)")]
    for col, kind in (("start", "TEXT"), ("build", "TEXT"), ("bound", "INT")):
        if col not in cols:
            c.execute(f"ALTER TABLE parts ADD COLUMN {col} {kind}")
    return c


def patch_of(game_version: str) -> str:
    return ".".join(game_version.split(".")[:2])


def _runes(p: dict) -> dict | None:
    perks = p.get("perks") or {}
    styles = perks.get("styles") or []
    if len(styles) < 2:
        return None
    sp = perks.get("statPerks") or {}
    return {"primary": styles[0]["style"], "sub": styles[1]["style"],
            "perks": [s["perk"] for s in styles[0]["selections"]] + [s["perk"] for s in styles[1]["selections"]],
            "shards": [sp.get("offense"), sp.get("flex"), sp.get("defense")]}


def purchases(timeline: dict) -> dict[int, tuple[list[int], list[int]]]:
    """participantId -> (başlangıç itemleri, sırayla alınan bütün itemler). Geri alınan
    (undo) alışverişler çıkarılır."""
    events = sorted((e for f in timeline["info"]["frames"] for e in f["events"]
                     if e["type"] in ("ITEM_PURCHASED", "ITEM_UNDO")), key=lambda e: e["timestamp"])
    bought: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for e in events:
        pid = e.get("participantId")
        if e["type"] == "ITEM_PURCHASED":
            bought[pid].append((e["timestamp"], e["itemId"]))
        elif e.get("beforeId"):  # alışverişi geri al: o itemin son alımını sil
            seq = bought[pid]
            for i in range(len(seq) - 1, -1, -1):
                if seq[i][1] == e["beforeId"]:
                    del seq[i]
                    break
    return {pid: ([it for t, it in seq if t <= START_WINDOW], [it for _, it in seq]) for pid, seq in bought.items()}


def store(info: dict, source: str = "apex", timeline: dict | None = None) -> None:
    """Match-V5 maç detayını (ve varsa timeline'ını) veritabanına ekler (zaten varsa dokunmaz)."""
    i = info["info"]
    if i.get("gameDuration", 0) < 600:  # remake / erken teslim
        return
    mid = info["metadata"]["matchId"]
    parts = i["participants"]
    by_role = {(p["teamId"], p.get("teamPosition")): p["championId"] for p in parts}
    buys = purchases(timeline) if timeline else {}
    rows = []
    for p in parts:
        role = p.get("teamPosition") or ""
        start, build = buys.get(p["participantId"], (None, None))
        rows.append((mid, p["puuid"], p["championId"], role, int(p["win"]), json.dumps(_runes(p)),
                     json.dumps([p.get(f"item{k}", 0) for k in range(6) if p.get(f"item{k}")]),
                     by_role.get((300 - p["teamId"], role)),
                     json.dumps(start) if start is not None else None, json.dumps(build) if build is not None else None,
                     # rol görevi yuvası: ADC'nin botu 6 item yuvasında değil burada durur
                     p.get("roleBoundItem") or 0))
    with _lock, _db() as c:
        if c.execute("SELECT 1 FROM matches WHERE id=?", (mid,)).fetchone():
            return
        c.execute("INSERT INTO matches (id, patch, queue, created, source) VALUES (?,?,?,?,?)",
                  (mid, patch_of(i.get("gameVersion", "")), i.get("queueId"), i.get("gameCreation", 0) / 1000, source))
        c.executemany("INSERT INTO parts (match_id, puuid, champion, role, win, runes, items, opponent, start, build, bound) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)


def known(match_ids: list[str]) -> set[str]:
    if not match_ids:
        return set()
    with _db() as c:
        q = ",".join("?" * len(match_ids))
        return {r[0] for r in c.execute(f"SELECT id FROM matches WHERE id IN ({q})", match_ids)}


def summary() -> dict:
    with _db() as c:
        rows = c.execute("SELECT patch, COUNT(*) FROM matches WHERE queue=420 AND source='apex' GROUP BY patch").fetchall()
        regions = c.execute("SELECT substr(id, 1, instr(id, '_') - 1) r, COUNT(*) FROM matches "
                            "WHERE queue=420 AND source='apex' GROUP BY r").fetchall()
    if not rows:
        return {"matches": 0, "patch": None, "regions": {}}
    patch = max(rows, key=lambda r: [int(x) for x in r[0].split(".") if x.isdigit()])
    return {"matches": sum(n for _, n in rows), "patch": patch[0], "patch_matches": patch[1],
            "regions": {REGION_NAMES.get(r, r): n for r, n in regions}}


def player_champion(puuid: str, champion: int, match_ids: list[str]) -> dict:
    """Oyuncunun verilen maçlarından (veritabanına inmiş olanlar) o şampiyonla kazanma durumu,
    kuyruk başına: {"solo": {games, wins, of}, "flex": {...}}. `of` = incelenen maç sayısı."""
    out = {q: {"games": 0, "wins": 0, "of": 0} for q in ("solo", "flex")}
    if not match_ids:
        return out
    q = ",".join("?" * len(match_ids))
    with _db() as c:
        rows = c.execute(f"SELECT p.champion, p.win, m.queue FROM parts p JOIN matches m ON m.id=p.match_id "
                         f"WHERE p.puuid=? AND p.match_id IN ({q})", (puuid, *match_ids)).fetchall()
    for ch, win, queue in rows:
        if queue not in (420, 440):
            continue
        o = out["solo" if queue == 420 else "flex"]
        o["of"] += 1
        if ch == champion:
            o["games"] += 1
            o["wins"] += win
    return out


def main_role(champion: int) -> str:
    with _db() as c:
        r = c.execute("SELECT role, COUNT(*) n FROM parts p JOIN matches m ON m.id=p.match_id "
                      "WHERE m.source='apex' AND champion=? AND role!='' GROUP BY role "
                      "ORDER BY n DESC LIMIT 1", (champion,)).fetchone()
    return r[0] if r else ""


def suggest(champion: int, role: str = "", opponent: int | None = None) -> dict:
    """Şampiyon (+ rol) için en çok kullanılan rün sayfaları ve item yolu. Son yamada
    yeterli veri yoksa tüm yamalara bakılır. `opponent` verilirse ayrıca yalnızca o rakibe
    karşı (aynı koridorda) oynanan maçlardan bir eşleşme önerisi döner (`matchup`)."""
    role = role or main_role(champion)
    cur = summary().get("patch")
    with _db() as c:
        def rows(patch_only: bool, vs: int | None = None):
            sql = ("SELECT p.win, p.runes, p.items, p.start, p.build, p.bound, p.role FROM parts p JOIN matches m ON m.id=p.match_id "
                   "WHERE m.queue=420 AND m.source='apex' AND p.champion=?")
            args: list = [champion]
            if role:
                sql += " AND p.role=?"; args.append(role)
            if patch_only and cur:
                sql += " AND m.patch=?"; args.append(cur)
            if vs:
                sql += " AND p.opponent=?"; args.append(vs)
            return c.execute(sql, args).fetchall()
        data = rows(True)
        scope = cur
        if len(data) < MIN_GAMES:
            data, scope = rows(False), "all patches"
        # eşleşme verisi az olur; tüm yamalara bakılır
        vs = rows(False, opponent) if opponent else []

    out = {"champion": champion, "role": role, "scope": scope, **_aggregate(data, MIN_GAMES)}
    out["matchup"] = {"opponent": opponent, "scope": "all patches", **_aggregate(vs, MIN_MATCHUP)} if opponent else None
    return out


def _aggregate(data: list[tuple], enough: int) -> dict:
    """Maç satırlarından rün sayfaları, başlangıç itemleri, item sırası ve itemler."""
    games = len(data)
    wr = lambda w, n: round(100 * w / n, 1) if n else None  # noqa: E731

    pages = defaultdict(lambda: [0, 0])
    for win, r, *_ in data:
        if r and r != "null":
            pages[r][0] += 1
            pages[r][1] += win
    top_pages = sorted(pages.items(), key=lambda kv: kv[1][0], reverse=True)[:3]

    legend, boots = defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0])
    boot_games = 0
    for win, _, items, _, _, bound, row_role in data:
        # rol yuvası kaydedilmeden toplanan ADC maçlarında bot bilinmiyor; bot hesabına girmez
        known_boots = row_role != "BOTTOM" or bound is not None
        boot_games += known_boots
        for it in set(json.loads(items)) | ({bound} if bound else set()):
            kind = ddragon.item_kind(it)
            if kind:
                if kind == "boots" and not known_boots:
                    continue
                bucket = legend if kind == "legendary" else boots
                bucket[it][0] += 1
                bucket[it][1] += win
    rank = lambda d: sorted(d.items(), key=lambda kv: kv[1][0], reverse=True)  # noqa: E731
    item = lambda kv: {"id": kv[0], "games": kv[1][0], "pick": wr(kv[1][0], games), "wr": wr(kv[1][1], kv[1][0])}  # noqa: E731

    # timeline'ı olan maçlardan: başlangıç seti ve ilk 3 bitmiş itemin alınma sırası
    timed = [(win, json.loads(st), json.loads(bd)) for win, _, _, st, bd, *_ in data if st is not None and bd]
    starts, orders = defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0])
    for win, st, bd in timed:
        st = [i for i in st if not ddragon.is_trinket(i)]  # totem herkese bedava, seti bölmesin
        if st:
            k = tuple(sorted(st))
            starts[k][0] += 1
            starts[k][1] += win
        seen: list[int] = []
        for it in bd:
            if ddragon.item_kind(it) == "legendary" and it not in seen:
                seen.append(it)
        if len(seen) >= 3:
            orders[tuple(seen[:3])][0] += 1
            orders[tuple(seen[:3])][1] += win
    n_starts = sum(v[0] for v in starts.values())
    n_orders = sum(v[0] for v in orders.values())
    group = lambda kv, n: {"items": [{"id": i, "count": kv[0].count(i)} for i in dict.fromkeys(kv[0])],  # noqa: E731
                           "games": kv[1][0], "pick": wr(kv[1][0], n), "wr": wr(kv[1][1], kv[1][0])}

    return {
        "games": games, "wr": wr(sum(d[0] for d in data), games), "enough": games >= enough,
        "runes": [{**json.loads(k), "games": v[0], "pick": wr(v[0], games), "wr": wr(v[1], v[0])}
                  for k, v in top_pages],
        "timed": len(timed),
        "start": [group(kv, n_starts) for kv in rank(starts)[:2]],
        "order": [group(kv, n_orders) for kv in rank(orders)[:3]],
        "core": [item(kv) for kv in rank(legend)[:3]],
        "boots": [{**item(kv), "pick": wr(kv[1][0], boot_games)} for kv in rank(boots)[:2]],
        "situational": [item(kv) for kv in rank(legend)[3:9]],
    }
