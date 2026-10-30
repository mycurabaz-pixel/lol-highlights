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
from .riot_api import KeyExpired, RiotApi

DB = Path(__file__).resolve().parents[1] / "stats.db"
ROLES = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")
MIN_GAMES = 8        # bu kadar maçtan azsa öneri "az veri" uyarısıyla gösterilir
_lock = threading.Lock()


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


def store(info: dict, source: str = "apex") -> None:
    """Match-V5 maç detayını veritabanına ekler (zaten varsa dokunmaz)."""
    i = info["info"]
    if i.get("gameDuration", 0) < 600:  # remake / erken teslim
        return
    mid = info["metadata"]["matchId"]
    parts = i["participants"]
    by_role = {(p["teamId"], p.get("teamPosition")): p["championId"] for p in parts}
    rows = []
    for p in parts:
        role = p.get("teamPosition") or ""
        rows.append((mid, p["puuid"], p["championId"], role, int(p["win"]), json.dumps(_runes(p)),
                     json.dumps([p.get(f"item{k}", 0) for k in range(6) if p.get(f"item{k}")]),
                     by_role.get((300 - p["teamId"], role))))
    with _lock, _db() as c:
        if c.execute("SELECT 1 FROM matches WHERE id=?", (mid,)).fetchone():
            return
        c.execute("INSERT INTO matches (id, patch, queue, created, source) VALUES (?,?,?,?,?)",
                  (mid, patch_of(i.get("gameVersion", "")), i.get("queueId"), i.get("gameCreation", 0) / 1000, source))
        c.executemany("INSERT INTO parts VALUES (?,?,?,?,?,?,?,?)", rows)


def known(match_ids: list[str]) -> set[str]:
    if not match_ids:
        return set()
    with _db() as c:
        q = ",".join("?" * len(match_ids))
        return {r[0] for r in c.execute(f"SELECT id FROM matches WHERE id IN ({q})", match_ids)}


def summary() -> dict:
    with _db() as c:
        rows = c.execute("SELECT patch, COUNT(*) FROM matches WHERE queue=420 AND source='apex' GROUP BY patch").fetchall()
    if not rows:
        return {"matches": 0, "patch": None}
    patch = max(rows, key=lambda r: [int(x) for x in r[0].split(".") if x.isdigit()])
    return {"matches": sum(n for _, n in rows), "patch": patch[0], "patch_matches": patch[1]}


def player_champion(puuid: str, champion: int, match_ids: list[str]) -> dict:
    """Oyuncunun verilen maçlarından (veritabanına inmiş olanlar) o şampiyonla kazanma durumu."""
    if not match_ids:
        return {"games": 0, "wins": 0, "of": 0}
    q = ",".join("?" * len(match_ids))
    with _db() as c:
        rows = c.execute(f"SELECT champion, win FROM parts WHERE puuid=? AND match_id IN ({q})",
                         (puuid, *match_ids)).fetchall()
    on = [w for ch, w in rows if ch == champion]
    return {"games": len(on), "wins": sum(on), "of": len(rows)}


def main_role(champion: int) -> str:
    with _db() as c:
        r = c.execute("SELECT role, COUNT(*) n FROM parts p JOIN matches m ON m.id=p.match_id "
                      "WHERE m.source='apex' AND champion=? AND role!='' GROUP BY role "
                      "ORDER BY n DESC LIMIT 1", (champion,)).fetchone()
    return r[0] if r else ""


def suggest(champion: int, role: str = "", opponent: int | None = None) -> dict:
    """Şampiyon (+ rol) için en çok kullanılan rün sayfaları ve item yolu.
    Son yamada yeterli veri yoksa tüm yamalara bakılır."""
    role = role or main_role(champion)
    cur = summary().get("patch")
    with _db() as c:
        def rows(patch_only: bool, vs: int | None = None):
            sql = ("SELECT p.win, p.runes, p.items FROM parts p JOIN matches m ON m.id=p.match_id "
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
        vs = rows(False, opponent) if opponent else []

    games = len(data)
    wr = lambda w, n: round(100 * w / n, 1) if n else None  # noqa: E731

    pages = defaultdict(lambda: [0, 0])
    for win, r, _ in data:
        if r and r != "null":
            pages[r][0] += 1
            pages[r][1] += win
    top_pages = sorted(pages.items(), key=lambda kv: kv[1][0], reverse=True)[:3]

    legend, boots = defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0])
    for win, _, items in data:
        for it in set(json.loads(items)):
            kind = ddragon.item_kind(it)
            if kind:
                bucket = legend if kind == "legendary" else boots
                bucket[it][0] += 1
                bucket[it][1] += win
    rank = lambda d: sorted(d.items(), key=lambda kv: kv[1][0], reverse=True)  # noqa: E731
    item = lambda kv: {"id": kv[0], "games": kv[1][0], "pick": wr(kv[1][0], games), "wr": wr(kv[1][1], kv[1][0])}  # noqa: E731

    return {
        "champion": champion, "role": role, "games": games, "scope": scope,
        "wr": wr(sum(d[0] for d in data), games), "enough": games >= MIN_GAMES,
        "vs": {"champion": opponent, "games": len(vs), "wr": wr(sum(d[0] for d in vs), len(vs))} if opponent else None,
        "runes": [{**json.loads(k), "games": v[0], "pick": wr(v[0], games), "wr": wr(v[1], v[0])}
                  for k, v in top_pages],
        "core": [item(kv) for kv in rank(legend)[:3]],
        "boots": [item(kv) for kv in rank(boots)[:2]],
        "situational": [item(kv) for kv in rank(legend)[3:9]],
    }


class Collector:
    """Arka planda Master+ maçlarını toplar. Anahtar geçersizse bekler, uygulama kapanınca durur."""

    def __init__(self, cfg: dict, log):
        self.cfg, self.log = cfg, log
        self.state = "idle"
        self.added = 0
        self._stop = threading.Event()
        self._wake = threading.Event()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def update(self, cfg: dict) -> None:
        """Ayarlar (ör. yeni API anahtarı) kaydedilince beklemeden devam et."""
        self.cfg = cfg
        self._wake.set()

    def _wait(self, seconds: float) -> None:
        self._wake.wait(seconds)
        self._wake.clear()

    def run(self) -> None:
        players: list[str] = []
        refreshed = 0.0
        while not self._stop.is_set():
            try:
                r = self.cfg["riot"]
                api = RiotApi(r["api_key"], r["platform"], reserve=35)  # 35 isteği ön plana bırak
                if time.time() - refreshed > 3600 or not players:
                    players = api.apex_puuids()
                    random.shuffle(players)
                    refreshed = time.time()
                    if not players:
                        self.state = "no players"
                        self._wait(600)
                        continue
                self.state = "collecting"
                puuid = players.pop()
                ids = api.match_ids(puuid, count=10)
                for mid in [m for m in ids if m not in known(ids)]:
                    if self._stop.is_set():
                        return
                    if (info := api.match(mid)):
                        store(info)
                        self.added += 1
                        if self.added % 100 == 0:
                            self.log(f"Stats: {summary()['matches']} high-elo matches collected")
            except KeyExpired:
                self.state = "key expired"
                self._wait(600)
            except Exception as e:  # ağ hatası vb.; toplayıcı ölmesin
                self.state = f"error: {e}"
                self._wait(60)
