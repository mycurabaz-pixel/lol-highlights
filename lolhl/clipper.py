"""Highlight'ları ffmpeg ile videodan keser."""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .models import Highlight


@dataclass
class Clip:
    start: float  # video zamanı (saniye)
    end: float
    highlights: list[Highlight] = field(default_factory=list)

    @property
    def name(self) -> str:
        tags = [h.tag for h in self.highlights]
        counts = {t: tags.count(t) for t in dict.fromkeys(tags)}
        tag = "+".join(f"{n}x{t}" if n > 1 else t for t, n in counts.items())
        t = int(self.highlights[0].game_time)
        return f"{t // 60:02d}m{t % 60:02d}s_{tag}"


def plan_clips(hls: list[Highlight], offset: float, merge_gap: float,
               video_len: float | None = None) -> list[Clip]:
    """offset: video_zamanı = oyun_zamanı + offset.
    Birbirine yakın anları (multikill, teamfight) tek klipte birleştirir."""
    clips: list[Clip] = []
    for h in sorted(hls, key=lambda h: h.game_time):
        s = max(0.0, h.game_time + offset - h.pre)
        e = h.game_time + offset + h.post
        if video_len is not None:
            e = min(e, video_len)
            if s >= e:
                continue
        if clips and s <= clips[-1].end + merge_gap:
            clips[-1].end = max(clips[-1].end, e)
            clips[-1].highlights.append(h)
        else:
            clips.append(Clip(s, e, [h]))
    return clips


def ffmpeg_path() -> str:
    p = shutil.which("ffmpeg")
    if not p:
        raise RuntimeError("ffmpeg bulunamadı. Kur: winget install Gyan.FFmpeg")
    return p


def video_duration(video: str) -> float | None:
    probe = shutil.which("ffprobe")
    if not probe:
        return None
    out = subprocess.run([probe, "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", video], capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return None


def cut(video: str, clips: list[Clip], out_dir: Path, reencode: bool) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    ff = ffmpeg_path()
    paths = []
    for c in clips:
        dst = out_dir / f"{c.name}.mp4"
        codec = (["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac"]
                 if reencode else ["-c", "copy", "-avoid_negative_ts", "make_zero"])
        cmd = [ff, "-y", "-loglevel", "error", "-ss", f"{c.start:.3f}", "-i", video,
               "-t", f"{c.end - c.start:.3f}", *codec, str(dst)]
        subprocess.run(cmd, check=True)
        paths.append(dst)
    return paths
