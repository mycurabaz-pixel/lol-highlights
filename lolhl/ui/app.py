"""Masaüstü arayüzü: watcher'ı arka planda çalıştırır, klipleri listeler ve oynatır.

Pencere Windows'un kendi WebView2'si (pywebview) ile açılır; videolar ve sayfa
yerel bir HTTP sunucusundan gelir (video ileri/geri sarma için Range desteği var).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import tomllib
from collections import deque
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import webview

from ..watcher import Watcher

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config.toml"
STATIC = Path(__file__).parent / "static"
DDRAGON_ALIASES = {"FiddleSticks": "Fiddlesticks"}


def load_config() -> dict:
    with open(CONFIG, "rb") as f:
        return tomllib.load(f)


def clips_dir(cfg: dict) -> Path:
    p = Path(cfg["clips"]["output_dir"])
    return p if p.is_absolute() else ROOT / p


# ---------- HTTP: sayfa + klipler ----------

class Handler(SimpleHTTPRequestHandler):
    clips_root: Path = ROOT / "clips"
    recordings_root: Path = ROOT / "recordings"

    def log_message(self, *_):
        pass

    def translate_path(self, path: str) -> str:
        rel = unquote(urlparse(path).path).lstrip("/")
        if rel.startswith("clips/"):
            base, rel = self.clips_root, rel[len("clips/"):]
        elif rel.startswith("recordings/"):
            base, rel = self.recordings_root, rel[len("recordings/"):]
        else:
            base, rel = STATIC, rel or "index.html"
        target = (base / rel).resolve()
        # klasör dışına çıkmayı engelle
        return str(target) if target.is_relative_to(base.resolve()) else str(STATIC / "404")

    def send_head(self):
        """<video> sarma yapabilsin diye Range isteklerini destekler."""
        path = Path(self.translate_path(self.path))
        rng = self.headers.get("Range")
        if not rng or not path.is_file():
            return super().send_head()
        size = path.stat().st_size
        m = re.match(r"bytes=(\d*)-(\d*)", rng)
        start = int(m.group(1) or 0)
        end = min(int(m.group(2)) if m.group(2) else size - 1, size - 1)
        f = open(path, "rb")
        f.seek(start)
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(str(path)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        self._remaining = end - start + 1
        return f

    def copyfile(self, source, outputfile):
        remaining = getattr(self, "_remaining", None)
        if remaining is None:
            return super().copyfile(source, outputfile)
        try:
            while remaining > 0 and (chunk := source.read(min(256 * 1024, remaining))):
                outputfile.write(chunk)
                remaining -= len(chunk)
        except (ConnectionResetError, BrokenPipeError):
            pass  # kullanıcı başka klibe geçti


def start_server(cfg: dict) -> int:
    Handler.clips_root = clips_dir(cfg)
    Handler.recordings_root = Path(cfg["obs"]["recordings_dir"])
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1]


# ---------- config.toml'u yorumları bozmadan güncelleme ----------

def set_toml(text: str, section: str, key: str, literal: str) -> str:
    sec = re.search(rf"^\[{re.escape(section)}\]\s*$", text, re.M)
    if not sec:
        return text + f"\n[{section}]\n{key} = {literal}\n"
    end = re.search(r"^\[", text[sec.end():], re.M)
    body_end = sec.end() + (end.start() if end else len(text) - sec.end())
    body = text[sec.end():body_end]
    line = re.compile(rf"^({re.escape(key)}\s*=\s*)([^#\n]*?)(\s*#.*)?$", re.M)
    if line.search(body):
        body = line.sub(lambda m: m.group(1) + literal + (m.group(3) or ""), body, count=1)
    else:
        body = f"\n{key} = {literal}" + body
    return text[:sec.end()] + body + text[body_end:]


# ---------- JS'in çağırdığı API ----------

class Api:
    def __init__(self):
        self.cfg = load_config()
        self.logs: deque[str] = deque(maxlen=300)
        self.watcher: Watcher | None = None
        self.thread: threading.Thread | None = None
        self.port = start_server(self.cfg)
        self.window: webview.Window | None = None

    def _log(self, msg: str) -> None:
        self.logs.append(time.strftime("%H:%M:%S ") + msg)

    # --- watcher ---
    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.cfg = load_config()
        self.watcher = Watcher(self.cfg, self._log)
        self.thread = threading.Thread(target=self.watcher.run, daemon=True)
        self.thread.start()

    def stop(self):
        if self.watcher:
            self.watcher.stop()

    def state(self, since: int = 0):
        w = self.watcher
        logs = list(self.logs)
        return {"state": w.state if w else "stopped", "error": w.error if w else "",
                "logs": logs[since:] if since <= len(logs) else logs, "log_count": len(logs)}

    # --- maçlar ---
    def _recordings(self) -> dict[str, dict]:
        """match_id -> tam maç kaydı (saklanan son kayıtlar)."""
        rec_dir = Path(self.cfg["obs"]["recordings_dir"])
        out = {}
        for sidecar in rec_dir.glob("*.json"):
            try:
                meta = json.loads(sidecar.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            video = Path(meta.get("video") or sidecar.with_suffix(".mp4"))
            if video.is_file() and video.parent.resolve() == rec_dir.resolve():
                out[meta["match_id"]] = {"url": f"/recordings/{video.name}",
                                         "offset": meta["offset"]}
        return out

    def matches(self):
        root = clips_dir(self.cfg)
        recordings = self._recordings()
        out = []
        for meta_file in root.glob("*/highlights.json"):
            folder = meta_file.parent
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            champ = meta.get("champion") or folder.name.rsplit("_", 1)[-1]
            clips = []
            for c in meta.get("clips", []):
                if not (folder / c["file"]).exists():
                    continue
                clips.append({
                    "file": c["file"],
                    "url": f"/clips/{folder.name}/{c['file']}",
                    "duration": round(c["end"] - c["start"], 1),
                    "time": c["moments"][0]["game_time"] if c["moments"] else 0,
                    "moments": c["moments"],
                })
            out.append({
                "folder": folder.name, "match_id": meta.get("match_id", folder.name),
                "champion": champ, "icon": DDRAGON_ALIASES.get(champ, champ),
                "kda": meta.get("kda"),
                "created": meta.get("created") or meta_file.stat().st_mtime,
                "clips": clips,
                "recording": recordings.get(meta.get("match_id")),
            })
        return sorted(out, key=lambda m: m["created"], reverse=True)

    def _folder(self, folder: str) -> Path:
        p = (clips_dir(self.cfg) / folder).resolve()
        if not p.is_relative_to(clips_dir(self.cfg).resolve()) or not p.is_dir():
            raise ValueError("Geçersiz klasör")
        return p

    def delete_clip(self, folder: str, file: str):
        d = self._folder(folder)
        (d / Path(file).name).unlink(missing_ok=True)
        meta_file = d / "highlights.json"
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        meta["clips"] = [c for c in meta["clips"] if c["file"] != file]
        meta_file.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    def delete_match(self, folder: str):
        shutil.rmtree(self._folder(folder))

    def open_folder(self, folder: str = "", file: str = ""):
        if not folder:
            os.startfile(clips_dir(self.cfg))
        elif file:
            subprocess.Popen(["explorer", "/select,", str(self._folder(folder) / Path(file).name)])
        else:
            os.startfile(self._folder(folder))

    # --- ayarlar ---
    def settings(self):
        c = load_config()
        return {
            "api_key": c["riot"]["api_key"],
            "game_name": c["riot"]["game_name"], "tag_line": c["riot"]["tag_line"],
            "rules": c["rules"].get("enabled", []),
            "pre": c["rules"].get("pre", 10), "post": c["rules"].get("post", 5),
            "death_pre": c["rules"].get("my_deaths", {}).get("pre", c["rules"].get("pre", 10)),
            "keep_recordings": c["obs"].get("keep_recordings", 1),
        }

    def save_settings(self, s: dict):
        t = CONFIG.read_text(encoding="utf-8")
        q = lambda v: json.dumps(str(v).strip(), ensure_ascii=False)
        t = set_toml(t, "riot", "api_key", q(s["api_key"]))
        t = set_toml(t, "riot", "game_name", q(s["game_name"]))
        t = set_toml(t, "riot", "tag_line", q(s["tag_line"]))
        t = set_toml(t, "rules", "enabled", json.dumps(s["rules"]))
        t = set_toml(t, "rules", "pre", str(float(s["pre"])))
        t = set_toml(t, "rules", "post", str(float(s["post"])))
        t = set_toml(t, "rules.my_deaths", "pre", str(float(s["death_pre"])))
        t = set_toml(t, "obs", "keep_recordings", str(int(s["keep_recordings"])))
        tomllib.loads(t)  # bozuk dosya yazma
        CONFIG.write_text(t, encoding="utf-8")
        self.cfg = load_config()
        # yeni ayarlar watcher yeniden başlayınca geçerli olur
        busy = self.watcher and self.watcher.state in ("recording", "processing")
        if not busy and self.thread and self.thread.is_alive():
            self.stop()
            self.thread.join(timeout=10)
            self.start()
        return {"restarted": not busy}


def main() -> None:
    os.chdir(ROOT)  # config'teki göreli yollar (clips/) proje köküne göre
    api = Api()
    window = webview.create_window(
        "LoL Highlights", f"http://127.0.0.1:{api.port}/", js_api=api,
        width=1280, height=800, min_size=(960, 600), background_color="#0e1015",
    )
    api.window = window

    def on_closing():
        if api.watcher and api.watcher.state == "recording":
            return window.create_confirmation_dialog(
                "Kayıt sürüyor", "Maç kaydediliyor. Kapatırsan kayıt durur ve klipler "
                "bir sonraki açılışta çıkarılır. Yine de kapatılsın mı?")
        return True

    window.events.closing += on_closing
    api.start()
    webview.start(gui="edgechromium", debug=os.environ.get("LOLHL_DEBUG") == "1")
    api.stop()


if __name__ == "__main__":
    main()
