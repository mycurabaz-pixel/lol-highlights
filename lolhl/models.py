"""Kaynaktan bağımsız ortak veri modelleri.

Riot API, ileride Live Client veya başka bir kaynak — hepsi veriyi bu
modellere çevirir; kurallar ve klipleyici yalnızca bunları bilir.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Participant:
    participant_id: int
    puuid: str
    champion: str
    team_id: int  # 100 = mavi, 200 = kırmızı
    riot_id: str = ""


@dataclass(frozen=True)
class GameEvent:
    kind: str  # "CHAMPION_KILL", ileride "ELITE_MONSTER_KILL", "BUILDING_KILL"...
    game_time: float  # saniye, oyun saatine göre
    killer_id: int | None = None
    victim_id: int | None = None
    assist_ids: tuple[int, ...] = ()
    position: tuple[int, int] | None = None
    raw: dict = field(default_factory=dict, compare=False, repr=False)


@dataclass
class MatchData:
    match_id: str
    participants: dict[int, Participant]
    events: list[GameEvent]
    me: Participant

    def teammates(self) -> list[Participant]:
        return [p for p in self.participants.values()
                if p.team_id == self.me.team_id and p.participant_id != self.me.participant_id]


@dataclass
class Highlight:
    """Bir kuralın 'bu an klip olmalı' dediği tek bir an."""
    tag: str  # dosya adında kullanılır: "kill", "death", "teammate_kill"...
    game_time: float
    event: GameEvent
    pre: float  # olaydan önce kaç saniye
    post: float  # olaydan sonra kaç saniye
    label: str = ""
