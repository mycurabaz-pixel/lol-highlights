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


def tool_path(name: str, tool_dir: str = "") -> str | None:
    """Önce config'teki klasöre, sonra PATH'e bakar."""
    if tool_dir and (p := Path(tool_dir) / f"{name}.exe").is_file():
        return str(p)
    return shutil.which(name)


def video_duration(video: str, tool_dir: str = "") -> float | None:
    probe = tool_path("ffprobe", tool_dir)
    if not probe:
        return None
    out = subprocess.run([probe, "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", video], capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return None


ENCODERS = {
    # NVIDIA kartta GPU ile hızlı; yoksa işlemci
    "h264_nvenc": ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "21", "-b:v", "0"],
    "libx264": ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"],
}


def cut(video: str, clips: list[Clip], out_dir: Path, reencode: bool,
        tool_dir: str = "") -> list[Path]:
    """reencode=True: kesim tam saniyesinde olur. False (copy): hızlıdır ama başlangıç
    önceki anahtar kareye (birkaç saniye geriye) kayar ve olay klibin dışında kalabilir."""
    ff = tool_path("ffmpeg", tool_dir)
    if not ff:
        raise RuntimeError("ffmpeg bulunamadı. config.toml'da clips.ffmpeg_dir'i ayarla.")
    out_dir.mkdir(parents=True, exist_ok=True)
    order = ["h264_nvenc", "libx264"]
    paths = []
    for c in clips:
        dst = out_dir / f"{c.name}.mp4"
        base = [ff, "-y", "-loglevel", "error", "-ss", f"{c.start:.3f}", "-i", video,
                "-t", f"{c.end - c.start:.3f}"]
        if not reencode:
            subprocess.run([*base, "-c", "copy", "-avoid_negative_ts", "make_zero", str(dst)],
                           check=True)
        else:
            for enc in list(order):
                r = subprocess.run([*base, *ENCODERS[enc], "-c:a", "aac", "-b:a", "160k",
                                    "-movflags", "+faststart", str(dst)], capture_output=True)
                if r.returncode == 0:
                    break
                order.remove(enc)  # bu kodlayıcı yok/çalışmıyor; sonrakini dene
            else:
                raise RuntimeError(f"Klip kesilemedi: {r.stderr.decode(errors='replace')[-300:]}")
        paths.append(dst)
    return paths
