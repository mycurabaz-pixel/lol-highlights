"""Riot Web API (Account-V1, Match-V5) -> MatchData."""
from __future__ import annotations

import threading
import time
from collections import deque

import requests

from .models import GameEvent, MatchData, Participant

# Platform -> bölgesel yönlendirme
REGIONAL = {
    "TR1": "europe", "EUW1": "europe", "EUN1": "europe", "RU": "europe", "ME1": "europe",
    "NA1": "americas", "BR1": "americas", "LA1": "americas", "LA2": "americas",
    "KR": "asia", "JP1": "asia",
    "OC1": "sea", "SG2": "sea", "TW2": "sea", "VN2": "sea",
}


class KeyExpired(RuntimeError):
    pass


class _Limiter:
    """Geliştirici anahtarı sınırı: 20 istek/sn, 100 istek/2 dk. Aynı anahtarı kullanan
    herkes (watcher, analiz, oyuncu bilgisi, istatistik toplayıcı) aynı sayacı paylaşır.
    Arka plan işleri `reserve` kadar payı ön plana bırakır."""
    WINDOWS = ((1.0, 18), (120.0, 92))

    def __init__(self):
        self.lock = threading.Lock()
        self.sent: deque[float] = deque()

    def acquire(self, reserve: int = 0) -> None:
        while True:
            with self.lock:
                now = time.time()
                while self.sent and now - self.sent[0] > 120:
                    self.sent.popleft()
                wait = 0.0
                for span, cap in self.WINDOWS:
                    cap -= reserve if span > 1 else 0
                    recent = [t for t in self.sent if now - t < span]
                    if len(recent) >= cap:
                        wait = max(wait, recent[len(recent) - cap] + span - now)
                if wait <= 0:
                    self.sent.append(now)
                    return
            time.sleep(min(wait, 5) + 0.05)


_limiters: dict[str, _Limiter] = {}


class RiotApi:
    def __init__(self, api_key: str, platform: str, reserve: int = 0):
        if not api_key or api_key.startswith("RGAPI-xxxx"):
            raise ValueError("A valid riot.api_key is required in config.toml.")
        self.platform = platform.upper()
        self.base = f"https://{REGIONAL[self.platform]}.api.riotgames.com"
        self.pbase = f"https://{self.platform.lower()}.api.riotgames.com"  # League, Mastery
        self.s = requests.Session()
        self.s.headers["X-Riot-Token"] = api_key
        self.limit = _limiters.setdefault(api_key, _Limiter())
        self.reserve = reserve

    def _get(self, path: str, retries: int = 5, platform: bool = False):
        for _ in range(retries):
            self.limit.acquire(self.reserve)
            r = self.s.get((self.pbase if platform else self.base) + path, timeout=15)
            if r.status_code in (401, 403):
                raise KeyExpired("Riot API key is invalid or expired; renew it in Settings.")
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", 5)))
                continue
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"Rate limit not cleared: {path}")

    def puuid(self, game_name: str, tag_line: str) -> str:
        d = self._get(f"/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}")
        if not d:
            raise ValueError(f"Account not found: {game_name}#{tag_line}")
        return d["puuid"]

    def match_id_from_game_id(self, game_id: int) -> str:
        return f"{self.platform}_{game_id}"

    def last_match_id(self, puuid: str) -> str | None:
        ids = self._get(f"/lol/match/v5/matches/by-puuid/{puuid}/ids?count=1")
        return ids[0] if ids else None

    def wait_for_match(self, match_id: str, timeout: float = 300, every: float = 15) -> dict:
        """Maç bitince API'ye düşmesi biraz sürebilir."""
        end = time.time() + timeout
        while True:  # timeout=0 olsa da en az bir kez sor
            if (d := self._get(f"/lol/match/v5/matches/{match_id}")):
                return d
            if time.time() + every > end:
                break
            time.sleep(every)
        raise TimeoutError(f"{match_id} did not show up in the API")

    def fetch(self, match_id: str, timeout: float = 300) -> tuple[dict, dict]:
        """Ham maç detayı + timeline (analiz için olduğu gibi saklanır)."""
        info = self.wait_for_match(match_id, timeout)
        tl = self._get(f"/lol/match/v5/matches/{match_id}/timeline")
        if tl is None:
            raise RuntimeError(f"No timeline: {match_id}")
        return info, tl

    def match(self, match_id: str) -> dict | None:
        return self._get(f"/lol/match/v5/matches/{match_id}")

    def match_ids(self, puuid: str, count: int = 20, queue: int | None = 420, kind: str = "",
                  start: int = 0, since: int | None = None) -> list[str]:
        q = (f"&queue={queue}" if queue else "") + (f"&type={kind}" if kind else "") + \
            (f"&start={start}" if start else "") + (f"&startTime={since}" if since else "")
        return self._get(f"/lol/match/v5/matches/by-puuid/{puuid}/ids?count={count}{q}") or []

    def season_ids(self, puuid: str, queue: int, since: int) -> list[str]:
        """Bir kuyruktaki `since`'ten bu yana bütün maç kimlikleri (yeniden eskiye)."""
        out: list[str] = []
        while True:
            page = self.match_ids(puuid, 100, queue=queue, start=len(out), since=since)
            out += page
            if len(page) < 100:
                return out

    def account(self, game_name: str, tag_line: str) -> dict | None:
        return self._get(f"/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}")

    def league(self, puuid: str) -> list[dict]:
        return self._get(f"/lol/league/v4/entries/by-puuid/{puuid}", platform=True) or []

    def mastery(self, puuid: str, champion_id: int) -> dict | None:
        return self._get(f"/lol/champion-mastery/v4/champion-masteries/by-puuid/{puuid}/by-champion/{champion_id}",
                         platform=True)

    def apex_puuids(self, queue: str = "RANKED_SOLO_5x5") -> list[str]:
        """Challenger + Grandmaster + Master oyuncuları (istatistik toplamak için)."""
        out = []
        for tier in ("challenger", "grandmaster", "master"):
            d = self._get(f"/lol/league/v4/{tier}leagues/by-queue/{queue}", platform=True) or {}
            out += [e["puuid"] for e in d.get("entries", []) if e.get("puuid")]
        return out

    def load(self, match_id: str, puuid: str, timeout: float = 300) -> MatchData:
        return parse(match_id, *self.fetch(match_id, timeout), puuid)


def parse(match_id: str, info: dict, timeline: dict, puuid: str) -> MatchData:
    parts = {}
    for p in info["info"]["participants"]:
        parts[p["participantId"]] = Participant(
            participant_id=p["participantId"],
            puuid=p["puuid"],
            champion=p["championName"],
            team_id=p["teamId"],
            riot_id=f'{p.get("riotIdGameName", "")}#{p.get("riotIdTagline", "")}',
        )
    me = next((p for p in parts.values() if p.puuid == puuid), None)
    if me is None:
        raise ValueError("Your puuid is not in this match")

    events = []
    for frame in timeline["info"]["frames"]:
        for e in frame["events"]:
            pos = e.get("position")
            events.append(GameEvent(
                kind=e["type"],
                game_time=e["timestamp"] / 1000.0,
                killer_id=e.get("killerId") or None,
                victim_id=e.get("victimId"),
                assist_ids=tuple(e.get("assistingParticipantIds", ())),
                position=(pos["x"], pos["y"]) if pos else None,
                raw=e,
            ))
    return MatchData(match_id, parts, events, me, info=info, timeline=timeline)
