"""Maç verisi + video + offset -> klipler. Hem watcher hem elle işleme bunu kullanır."""
from __future__ import annotations

import json
import time
from pathlib import Path

from .analysis import save_raw
from .clipper import cut, plan_clips, video_duration
from .models import MatchData
from .rules import build_rules, find_highlights


def process(m: MatchData, video: str, offset: float, cfg: dict, log=print, skin: int | None = None) -> list[Path]:
    hls = find_highlights(m, build_rules(cfg["rules"]))
    tool_dir = cfg["clips"].get("ffmpeg_dir", "")
    clips = plan_clips(hls, offset, cfg["clips"]["merge_gap"], video_duration(video, tool_dir))

    out_dir = Path(cfg["clips"]["output_dir"]) / f"{m.match_id}_{m.me.champion}"
    log(f"{m.match_id} | {m.me.champion} | {len(hls)} moments -> {len(clips)} clips")
    for c in clips:
        log(f"  {c.name}: " + ", ".join(h.label for h in c.highlights))

    me = m.me.participant_id
    kills = [e for e in m.events if e.kind == "CHAMPION_KILL"]
    kda = [sum(e.killer_id == me for e in kills), sum(e.victim_id == me for e in kills),
           sum(me in e.assist_ids for e in kills)]

    paths = cut(video, clips, out_dir, cfg["clips"]["reencode"], tool_dir)
    save_raw(out_dir, m.info, m.timeline, m.me.puuid)
    (out_dir / "highlights.json").write_text(json.dumps({
        "match_id": m.match_id, "video": video, "offset": offset,
        "champion": m.me.champion, "skin": skin, "kda": kda, "created": time.time(),
        "clips": [{"file": p.name, "start": c.start, "end": c.end,
                   "moments": [{"tag": h.tag, "game_time": h.game_time, "label": h.label}
                               for h in c.highlights]}
                  for p, c in zip(paths, clips)],
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    return paths
