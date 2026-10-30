"""Oyun yüklenirken 10 oyuncunun lig, LP, kazanma oranı, şampiyon ustalığı ve son dereceli
maçlarında o şampiyonla kazanma oranı.

Oyuncu listesi oyun başladıktan sonra (isimler herkese açıkken) LCU gameflow oturumundan,
o olmazsa Live Client Data API'den alınır; geri kalanı Riot web API'si. Şampiyon seçiminde
gizli olan isimlere bakılmaz. Oyuna dokunmaz, overlay yoktur; bilgi uygulama penceresinde görünür.
"""
from __future__ import annotations

import threading

import requests

from . import ddragon, stats
from .lcu import Lcu
from .riot_api import KeyExpired, RiotApi

RECENT = 20  # şampiyon kazanma oranı için bakılan son dereceli (Solo/Duo + Flex) maç sayısı


def _champ_by_name(name: str) -> int | None:
    for cid, c in ddragon.champions().items():
        if name in (c["name"], c["id"]):
            return cid
    return None


def live_players(lcu: Lcu, api: RiotApi) -> list[dict] | None:
    """Oyundaki 10 oyuncu; oyun yoksa None."""
    if lcu.phase() not in ("InProgress", "Reconnect"):
        return None
    me = (lcu.get("/lol-summoner/v1/current-summoner") or {}).get("puuid")
    gd = (lcu.get("/lol-gameflow/v1/session") or {}).get("gameData", {})
    out = []
    for team, key in ((100, "teamOne"), (200, "teamTwo")):
        for p in gd.get(key) or []:
            if p.get("puuid"):
                out.append({"puuid": p["puuid"], "team": team, "champion_id": p.get("championId"),
                            "role": (p.get("selectedPosition") or "").upper().replace("NONE", ""),
                            "name": p.get("gameName") or p.get("summonerName") or ""})
    if len(out) < 10:
        out = []
        try:
            r = requests.get("https://127.0.0.1:2999/liveclientdata/playerlist", verify=False, timeout=2)
            plist = r.json() if r.ok else []
        except (requests.RequestException, ValueError):
            plist = []
        for p in plist:
            name, _, tag = (p.get("riotId") or "").partition("#")
            acc = api.account(name, tag) if name and tag else None
            if acc:
                out.append({"puuid": acc["puuid"], "team": 100 if p.get("team") == "ORDER" else 200,
                            "champion_id": _champ_by_name(p.get("championName", "")),
                            "role": p.get("position") or "", "name": name})
    for p in out:
        p["me"] = p["puuid"] == me
    return out or None


def match_players(info: dict, me: str | None) -> list[dict]:
    """Geçmiş bir maçın oyuncuları (canlı oyun yokken ekranı denemek için)."""
    return [{"puuid": p["puuid"], "team": p["teamId"], "champion_id": p["championId"],
             "role": p.get("teamPosition") or "", "name": p.get("riotIdGameName") or "",
             "me": p["puuid"] == me} for p in info["info"]["participants"]]


class Scout:
    """Bilgiyi arka planda doldurur; arayüz `snapshot()` ile okur."""

    def __init__(self, api: RiotApi, players: list[dict], source: str):
        self.api, self.source = api, source
        champs = ddragon.champions()
        for p in players:
            c = champs.get(p["champion_id"]) or {}
            p.update(champion=c.get("id", ""), champion_name=c.get("name", ""),
                     solo=None, flex=None, mastery=None, champ=None)
        self.players = players
        self.status = "loading"
        self.error = ""
        self.key = frozenset(p["puuid"] for p in players)
        self._stop = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> dict:
        return {"source": self.source, "status": self.status, "error": self.error, "players": self.players}

    def _run(self) -> None:
        try:
            # önce hızlı olanlar: lig ve ustalık (oyuncu başı 2 istek)
            for p in self.players:
                for e in self.api.league(p["puuid"]):
                    entry = {"tier": e["tier"], "rank": e["rank"], "lp": e["leaguePoints"],
                             "wins": e["wins"], "losses": e["losses"]}
                    if e["queueType"] == "RANKED_SOLO_5x5":
                        p["solo"] = entry
                    elif e["queueType"] == "RANKED_FLEX_SR":
                        p["flex"] = entry
                m = self.api.mastery(p["puuid"], p["champion_id"]) if p["champion_id"] else None
                p["mastery"] = {"level": m["championLevel"], "points": m["championPoints"]} if m else \
                    {"level": 0, "points": 0}
                if self._stop.is_set():
                    return
            # sonra son dereceli maçlar; oyuncular arasında sırayla, herkes eşit dolsun
            self.status = "recent games"
            ids = {p["puuid"]: self.api.match_ids(p["puuid"], RECENT, queue=None, kind="ranked") for p in self.players}
            todo = {k: [m for m in v if m not in stats.known(v)] for k, v in ids.items()}
            for p in self.players:
                p["champ"] = stats.player_champion(p["puuid"], p["champion_id"], ids[p["puuid"]])
            while any(todo.values()) and not self._stop.is_set():
                for p in self.players:
                    if todo[p["puuid"]]:
                        mid = todo[p["puuid"]].pop(0)
                        if not stats.known([mid]) and (info := self.api.match(mid)):
                            stats.store(info, "scout")
                        p["champ"] = stats.player_champion(p["puuid"], p["champion_id"], ids[p["puuid"]])
            self.status = "done"
        except KeyExpired as e:
            self.status, self.error = "error", str(e)
        except Exception as e:
            self.status, self.error = "error", f"{type(e).__name__}: {e}"
