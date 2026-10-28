"""Hangi olayların klip olacağını belirleyen kurallar.

Yeni bir highlight türü eklemek için: Rule'dan türet, match() yaz,
@register ile kaydet, config.toml'daki [rules].enabled listesine ekle.
"""
from __future__ import annotations

from typing import Iterable

from .models import GameEvent, Highlight, MatchData

RULES: dict[str, type["Rule"]] = {}


def register(cls: type["Rule"]) -> type["Rule"]:
    RULES[cls.name] = cls
    return cls


class Rule:
    name = "base"

    def __init__(self, pre: float = 10.0, post: float = 5.0, **_):
        self.pre = pre
        self.post = post

    def match(self, ev: GameEvent, m: MatchData) -> Highlight | None:
        raise NotImplementedError

    def _hl(self, tag: str, ev: GameEvent, m: MatchData) -> Highlight:
        killer = m.participants.get(ev.killer_id)
        victim = m.participants.get(ev.victim_id)
        label = f"{killer.champion if killer else 'Minion/Kule'} -> {victim.champion if victim else '?'}"
        return Highlight(tag, ev.game_time, ev, self.pre, self.post, label)


@register
class MyKills(Rule):
    name = "my_kills"

    def match(self, ev, m):
        if ev.kind == "CHAMPION_KILL" and ev.killer_id == m.me.participant_id:
            return self._hl("kill", ev, m)
        return None


@register
class MyDeaths(Rule):
    name = "my_deaths"

    def match(self, ev, m):
        if ev.kind == "CHAMPION_KILL" and ev.victim_id == m.me.participant_id:
            return self._hl("death", ev, m)
        return None


@register
class MyAssists(Rule):
    name = "my_assists"

    def match(self, ev, m):
        if ev.kind == "CHAMPION_KILL" and m.me.participant_id in ev.assist_ids:
            return self._hl("assist", ev, m)
        return None


def build_rules(cfg: dict) -> list[Rule]:
    enabled = cfg.get("enabled", ["my_kills", "my_deaths"])
    defaults = {"pre": cfg.get("pre", 10.0), "post": cfg.get("post", 5.0)}
    out = []
    for name in enabled:
        if name not in RULES:
            raise ValueError(f"Bilinmeyen kural: {name}. Mevcut: {', '.join(RULES)}")
        out.append(RULES[name](**{**defaults, **cfg.get(name, {})}))
    return out


def find_highlights(m: MatchData, rules: Iterable[Rule]) -> list[Highlight]:
    hls = []
    for ev in m.events:
        for r in rules:
            if (h := r.match(ev, m)) is not None:
                hls.append(h)
    return sorted(hls, key=lambda h: h.game_time)
