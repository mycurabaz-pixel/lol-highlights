"""Maç verisi + video + offset -> klipler. Hem watcher hem elle işleme bunu kullanır."""
from __future__ import annotations

import json
from pathlib import Path

from .clipper import cut, plan_clips, video_duration
from .models import MatchData
from .rules import build_rules, find_highlights


def process(m: MatchData, video: str, offset: float, cfg: dict) -> list[Path]:
    hls = find_highlights(m, build_rules(cfg["rules"]))
    tool_dir = cfg["clips"].get("ffmpeg_dir", "")
    clips = plan_clips(hls, offset, cfg["clips"]["merge_gap"], video_duration(video, tool_dir))

    out_dir = Path(cfg["clips"]["output_dir"]) / f"{m.match_id}_{m.me.champion}"
    print(f"{m.match_id} | {m.me.champion} | {len(hls)} an -> {len(clips)} klip")
    for c in clips:
        print(f"  {c.name}: " + ", ".join(h.label for h in c.highlights))

    paths = cut(video, clips, out_dir, cfg["clips"]["reencode"], tool_dir)
    (out_dir / "highlights.json").write_text(json.dumps({
        "match_id": m.match_id, "video": video, "offset": offset,
        "clips": [{"file": p.name, "start": c.start, "end": c.end,
                   "moments": [{"tag": h.tag, "game_time": h.game_time, "label": h.label}
                               for h in c.highlights]}
                  for p, c in zip(paths, clips)],
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    return paths
