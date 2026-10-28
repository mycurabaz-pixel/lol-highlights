"""Riot Web API (Account-V1, Match-V5) -> MatchData."""
from __future__ import annotations

import time

import requests

from .models import GameEvent, MatchData, Participant

# Platform -> bölgesel yönlendirme
REGIONAL = {
    "TR1": "europe", "EUW1": "europe", "EUN1": "europe", "RU": "europe", "ME1": "europe",
    "NA1": "americas", "BR1": "americas", "LA1": "americas", "LA2": "americas",
    "KR": "asia", "JP1": "asia",
    "OC1": "sea", "SG2": "sea", "TW2": "sea", "VN2": "sea",
}


class RiotApi:
    def __init__(self, api_key: str, platform: str):
        if not api_key or api_key.startswith("RGAPI-xxxx"):
            raise ValueError("config.toml içinde geçerli bir riot.api_key gerekli.")
        self.platform = platform.upper()
        self.base = f"https://{REGIONAL[self.platform]}.api.riotgames.com"
        self.s = requests.Session()
        self.s.headers["X-Riot-Token"] = api_key

    def _get(self, path: str, retries: int = 5):
        for _ in range(retries):
            r = self.s.get(self.base + path, timeout=15)
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", 5)))
                continue
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"Rate limit aşılamadı: {path}")

    def puuid(self, game_name: str, tag_line: str) -> str:
        d = self._get(f"/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}")
        if not d:
            raise ValueError(f"Hesap bulunamadı: {game_name}#{tag_line}")
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
        raise TimeoutError(f"{match_id} API'de görünmedi")

    def load(self, match_id: str, puuid: str, timeout: float = 300) -> MatchData:
        info = self.wait_for_match(match_id, timeout)
        tl = self._get(f"/lol/match/v5/matches/{match_id}/timeline")
        if tl is None:
            raise RuntimeError(f"Timeline yok: {match_id}")
        return parse(match_id, info, tl, puuid)


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
        raise ValueError("Bu maçta senin puuid'in yok")

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
    return MatchData(match_id, parts, events, me)
