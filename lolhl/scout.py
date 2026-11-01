"""Oyun yüklenirken 10 oyuncunun lig, LP, kazanma oranı, şampiyon ustalığı ve bu sezonki
dereceli maçlarında (Solo/Duo ve Flex ayrı) o şampiyonla kazanma oranı.

Riot şampiyon başına sezon istatistiği vermiyor; sezonun maçları Match-V5'ten tek tek
çekilip sayılır (yeniden eskiye, oyuncular arasında sırayla). Çekilen maçlar stats.db'de
kalır, aynı oyuncu bir daha gelince anında hazırdır.

Oyuncu listesi oyun başladıktan sonra (isimler herkese açıkken) LCU gameflow oturumundan,
o olmazsa Live Client Data API'den alınır; geri kalanı Riot web API'si. Şampiyon seçiminde
gizli olan isimlere bakılmaz. Oyuna dokunmaz, overlay yoktur; bilgi uygulama penceresinde görünür.
"""
from __future__ import annotations

import threading
import time

import requests

from . import ddragon, stats
from .lcu import Lcu
from .riot_api import KeyExpired, RiotApi

SEASON_START = int(time.mktime((time.localtime().tm_year, 1, 1, 0, 0, 0, 0, 0, -1)))  # sezon ocakta başlar
QUEUES = {"solo": 420, "flex": 440}


def _champ_by_name(name: str) -> int | None:
    for cid, c in ddragon.champions().items():
        if name in (c["name"], c["id"]):
            return cid
    return None


def live_players(lcu: Lcu, api: RiotApi) -> list[dict] | None:
    """Oyundaki 10 oyuncu; oyun yoksa None.

    Client'ın verdiği puuid'ler kendi iç biçiminde (36 karakter); Riot web API'si onları
    tanımaz (400). Bu yüzden her oyuncunun Riot ID'si (ad#etiket) alınıp web API'sinin
    puuid'i Account-V1'den sorulur. İsimler oyun başladıktan sonra herkese açıktır."""
    if lcu.phase() not in ("InProgress", "Reconnect"):
        return None
    cur = lcu.get("/lol-summoner/v1/current-summoner") or {}
    me = (cur.get("gameName", "").lower(), cur.get("tagLine", "").lower())
    gd = (lcu.get("/lol-gameflow/v1/session") or {}).get("gameData", {})
    found = []  # (ad, etiket, takım, şampiyon, rol)
    for team, key in ((100, "teamOne"), (200, "teamTwo")):
        for p in gd.get(key) or []:
            s = lcu.get(f"/lol-summoner/v2/summoners/puuid/{p['puuid']}") if p.get("puuid") else None
            if s and s.get("gameName"):
                found.append((s["gameName"], s.get("tagLine", ""), team, p.get("championId"),
                              (p.get("selectedPosition") or "").upper().replace("NONE", "")))
    if len(found) < 10:
        # client vermediyse oyunun kendi yerel listesi (oyun yüklendikten sonra açılır)
        found = []
        try:
            r = requests.get("https://127.0.0.1:2999/liveclientdata/playerlist", verify=False, timeout=2)
            plist = r.json() if r.ok else []
        except (requests.RequestException, ValueError):
            plist = []
        for p in plist:
            name, _, tag = (p.get("riotId") or "").partition("#")
            if name and tag:
                found.append((name, tag, 100 if p.get("team") == "ORDER" else 200,
                              _champ_by_name(p.get("championName", "")), p.get("position") or ""))
    out = []
    for name, tag, team, champ, role in found:
        acc = api.account(name, tag)
        if acc:
            out.append({"puuid": acc["puuid"], "team": team, "champion_id": champ, "role": role,
                        "name": name, "me": (name.lower(), tag.lower()) == me})
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
                     solo=None, flex=None, mastery=None, champ=None, season=None)
        self.players = players
        self.status = "loading"
        self.error = ""
        self.key = frozenset(p["puuid"] for p in players)
        self.game = 0  # oyun kimliği (canlı oyunda); yeni oyunda yeniden başlatmak için
        self._stop = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        stats.SCOUTING.clear()

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
            # sonra bu sezonun bütün dereceli maçları; oyuncular arasında sırayla, yeniden eskiye
            self.status = "season games"
            stats.SCOUTING.set()
            ids = {}
            for p in self.players:
                per = {k: self.api.season_ids(p["puuid"], q, SEASON_START) for k, q in QUEUES.items()}
                p["season"] = {k: len(v) for k, v in per.items()}
                # Solo ve Flex'i tarihe göre birleştir (kimlikler artan sayı: büyük = yeni)
                ids[p["puuid"]] = sorted(per["solo"] + per["flex"], key=lambda m: int(m.split("_")[1]), reverse=True)
                if self._stop.is_set():
                    return
            todo = {}
            for p in self.players:
                all_ids = ids[p["puuid"]]
                have = stats.known(all_ids)
                todo[p["puuid"]] = [m for m in all_ids if m not in have]
                p["champ"] = stats.player_champion(p["puuid"], p["champion_id"], all_ids)
            while any(todo.values()) and not self._stop.is_set():
                for p in self.players:
                    if todo[p["puuid"]]:
                        mid = todo[p["puuid"]].pop(0)
                        if (info := self.api.match(mid)):
                            stats.store(info, "scout")
                        else:
                            ids[p["puuid"]].remove(mid)  # silinmiş maç
                        p["champ"] = stats.player_champion(p["puuid"], p["champion_id"], ids[p["puuid"]])
            self.status = "done"
        except KeyExpired as e:
            self.status, self.error = "error", str(e)
        except Exception as e:
            self.status, self.error = "error", f"{type(e).__name__}: {e}"
        finally:
            stats.SCOUTING.clear()
