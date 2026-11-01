"""Masaüstü arayüzü: watcher'ı arka planda çalıştırır, klipleri listeler ve oynatır.

Pencere Windows'un kendi WebView2'si (pywebview) ile açılır; videolar ve sayfa
yerel bir HTTP sunucusundan gelir (video ileri/geri sarma için Range desteği var).
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import tomllib
import uuid
from collections import deque
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests
import webview

from .. import analysis, ddragon, replay, scout, stats
from ..clipper import ENCODERS, tool_path
from ..lcu import Lcu
from ..riot_api import RiotApi
from ..watcher import Watcher

ROOT = Path(__file__).resolve().parents[2]
GAMES = ROOT / "games"  # oyun sırasında toplanan 10 oyuncunun lig/LP bilgisi, maç başına bir dosya
CONFIG = ROOT / "config.toml"
STATIC = Path(__file__).parent / "static"
DDRAGON_ALIASES = {"FiddleSticks": "Fiddlesticks"}
VIDEO_EXT = {".mp4", ".webm", ".mkv", ".mov"}
PREFIX = "LoL HL: "   # uygulamanın açtığı rün sayfaları ve item setleri bu adla başlar
POSITIONS = {"top": "TOP", "jungle": "JUNGLE", "middle": "MIDDLE", "bottom": "BOTTOM", "utility": "UTILITY"}
ROLE_NAMES = {"TOP": "Top", "JUNGLE": "Jungle", "MIDDLE": "Mid", "BOTTOM": "ADC", "UTILITY": "Support"}
DRAWN_DIR = "cizimli"  # clips/ altında; highlights.json olmadığı için maç listesine karışmaz


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
    game_clips_root: Path | None = None  # LoL'ün kendi kayıt klasörü (tekrardan alınan klipler)

    def log_message(self, *_):
        pass

    @classmethod
    def roots(cls) -> dict[str, Path]:
        r = {"clips": cls.clips_root, "recordings": cls.recordings_root}
        if cls.game_clips_root:
            r["lolclips"] = cls.game_clips_root
        return r

    @classmethod
    def resolve(cls, path: str) -> Path:
        rel = unquote(urlparse(path).path).lstrip("/")
        prefix, _, rest = rel.partition("/")
        if prefix in cls.roots():
            base, rel = cls.roots()[prefix], rest
        else:
            base, rel = STATIC, rel or "index.html"
        target = (base / rel).resolve()
        # klasör dışına çıkmayı engelle
        return target if target.is_relative_to(base.resolve()) else STATIC / "404"

    def translate_path(self, path: str) -> str:
        return str(self.resolve(path))

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


def notes_file(video: Path) -> Path:
    """Bir videonun çizimleri yanında durur: klip.mp4 -> klip.mp4.notes.json"""
    return video.with_name(video.name + ".notes.json")


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
        self._logs: deque[str] = deque(maxlen=300)
        self._watcher: Watcher | None = None
        self._thread: threading.Thread | None = None
        self.port = start_server(self.cfg)
        self._window: webview.Window | None = None
        self._puuid: str | None = None
        self._lcu = Lcu(self.cfg["lol"]["install_dir"])
        self._replays = replay.ReplayClient(self._lcu, ROOT / "replay_downloads.json")
        self._collector: stats.Collector | None = None
        self._scout: scout.Scout | None = None
        self._scout_lock = threading.Lock()  # canlı oyuncu listesi aynı anda bir kez alınır

    def _log(self, msg: str) -> None:
        self._logs.append(time.strftime("%H:%M:%S ") + msg)

    # --- watcher ---
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self.cfg = load_config()
        self._watcher = Watcher(self.cfg, self._log)
        self._thread = threading.Thread(target=self._watcher.run, daemon=True)
        self._thread.start()

    def stop(self):
        if self._watcher:
            self._watcher.stop()

    def state(self, since: int = 0):
        w = self._watcher
        logs = list(self._logs)
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
            if "win" not in meta:  # eski kayıtlar: sonucu kayıtlı ham maç verisinden bir kez çıkar
                raw = analysis.load_raw(folder) or {}
                meta["win"] = next((p.get("win") for p in (raw.get("match") or {}).get("info", {}).get("participants", [])
                                    if p.get("puuid") == raw.get("puuid")), None)
                try:
                    meta_file.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
                except OSError:
                    pass
            clips = []
            for c in meta.get("clips", []):
                if not (folder / c["file"]).exists():
                    continue
                clips.append({
                    "file": c["file"],
                    "url": f"/clips/{folder.name}/{c['file']}",
                    "duration": round(c["end"] - c["start"], 1),
                    "start": c["start"],  # tam kayıttaki saniye; oyun saati = start + t - offset
                    "time": c["moments"][0]["game_time"] if c["moments"] else 0,
                    "moments": c["moments"],
                })
            out.append({
                "folder": folder.name, "match_id": meta.get("match_id", folder.name),
                "champion": champ, "icon": DDRAGON_ALIASES.get(champ, champ), "skin": meta.get("skin") or 0,
                "kda": meta.get("kda"), "win": meta.get("win"), "offset": meta.get("offset", 0),
                "created": meta.get("created") or meta_file.stat().st_mtime,
                "clips": clips,
                "recording": recordings.get(meta.get("match_id")),
            })
        return sorted(out, key=lambda m: m["created"], reverse=True)

    def match_players(self, match_id: str):
        """O maç oynanırken kaydedilen 10 oyuncu (lig, LP, kazanma oranı, ustalık); yoksa None."""
        try:
            data = json.loads((GAMES / f"{Path(match_id).name}.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        return {"ddragon": ddragon.version(), "players": data.get("players"), "saved": data.get("saved")}

    def _folder(self, folder: str) -> Path:
        p = (clips_dir(self.cfg) / folder).resolve()
        if not p.is_relative_to(clips_dir(self.cfg).resolve()) or not p.is_dir():
            raise ValueError("Invalid folder")
        return p

    def delete_clip(self, folder: str, file: str):
        d = self._folder(folder)
        (d / Path(file).name).unlink(missing_ok=True)
        notes_file(d / Path(file).name).unlink(missing_ok=True)
        meta_file = d / "highlights.json"
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        meta["clips"] = [c for c in meta["clips"] if c["file"] != file]
        meta_file.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    def match_analysis(self, folder: str):
        """Grafiklerin verisi. Ham veri maç klasöründe yoksa (eski maçlar) Riot API'den
        bir kez çekilip saklanır."""
        d = self._folder(folder)
        raw = analysis.load_raw(d)
        if raw is None:
            meta = json.loads((d / "highlights.json").read_text(encoding="utf-8"))
            r = self.cfg["riot"]
            try:
                api = RiotApi(r["api_key"], r["platform"])
                if not self._puuid:
                    self._puuid = api.puuid(r["game_name"], r["tag_line"])
                info, tl = api.fetch(meta["match_id"], timeout=0)
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code in (401, 403):
                    raise RuntimeError("Riot API key is invalid or expired; renew it in Settings.")
                raise
            analysis.save_raw(d, info, tl, self._puuid)
            raw = analysis.load_raw(d)
        return analysis.build(raw["match"], raw["timeline"], raw["puuid"])

    def camps_load(self, folder: str):
        """Zamanlayıcıda elle işaretlenen orman kampı kesimleri (Riot verisinde yok)."""
        try:
            return json.loads((self._folder(folder) / "camps.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []

    def camps_save(self, folder: str, marks: list):
        (self._folder(folder) / "camps.json").write_text(json.dumps(marks), encoding="utf-8")

    def delete_match(self, folder: str):
        shutil.rmtree(self._folder(folder))

    def open_folder(self, folder: str = "", file: str = ""):
        if not folder:
            os.startfile(clips_dir(self.cfg))
        elif file:
            subprocess.Popen(["explorer", "/select,", str(self._folder(folder) / Path(file).name)])
        else:
            os.startfile(self._folder(folder))

    # --- tekrarlar (LCU) ---
    def _replay_folders(self) -> dict:
        f = self._replays.folders()
        Handler.game_clips_root = Path(f["highlights"])
        return f

    def replay_overview(self):
        return {**self._replays.status(), "quota": self._replays.quota(),
                "api_enabled": replay.replay_api_enabled(self.cfg["lol"]["install_dir"])}

    def replay_matches(self):
        return self._replays.matches()

    def replay_state(self, game: dict):
        return self._replays.replay_state(game)

    def replay_download(self, game: dict):
        return self._replays.download(game)

    def replay_watch(self, game_id: int):
        self._replays.watch(int(game_id))

    def local_replays(self):
        return replay.list_replays(Path(self._replay_folders()["replays"]))

    def game_clips(self):
        """LoL'ün kayıt klasöründeki videolar (Tekrar kontrolden alınan kayıtlar)."""
        folder = Path(self._replay_folders()["highlights"])
        out = []
        for p in folder.glob("*"):
            if p.suffix.lower() in VIDEO_EXT:
                st = p.stat()
                out.append({"file": p.name, "url": f"/lolclips/{p.name}", "size": st.st_size,
                            "mtime": st.st_mtime, "notes": notes_file(p).exists()})
        return sorted(out, key=lambda c: c["mtime"], reverse=True)

    def open_replay_folder(self, which: str):
        p = Path(self._replay_folders()["replays" if which == "replays" else "highlights"])
        p.mkdir(parents=True, exist_ok=True)
        os.startfile(p)

    # --- tekrar kontrol (Replay API; tekrar açıkken) ---
    def replay_status(self):
        return replay.replay_status()

    def replay_enable_api(self):
        return replay.enable_replay_api(self.cfg["lol"]["install_dir"])

    def replay_playback(self, patch: dict):
        return replay.playback(patch)

    def replay_render(self, patch: dict):
        return replay.render(patch)

    def replay_camera(self, preset: str):
        return replay.camera(preset)

    def replay_players(self):
        return replay.players()

    def replay_follow(self, names: list):
        return replay.follow(names)

    def replay_recording(self, patch: dict | None = None):
        return replay.recording(patch)

    # --- çizimler ---
    def _video(self, url: str) -> Path:
        """Arayüzdeki video adresini (/clips/..., /recordings/..., /lolclips/...) dosyaya çevirir."""
        p = Handler.resolve(url)
        if p.suffix.lower() not in VIDEO_EXT or not p.is_file():
            raise ValueError("Video not found")
        return p

    def notes_load(self, url: str):
        try:
            return json.loads(notes_file(self._video(url)).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"annotations": []}

    def notes_save(self, url: str, data: dict):
        f = notes_file(self._video(url))
        if data.get("annotations"):
            f.write_text(json.dumps({"version": 1, "annotations": data["annotations"]},
                                    ensure_ascii=False), encoding="utf-8")
        else:
            f.unlink(missing_ok=True)

    def export_drawn(self, url: str, layers: list[dict]):
        """Çizimleri ffmpeg ile videoya gömer. Her katman, videonun boyutunda saydam bir PNG
        ve görüneceği zaman aralığı; video yeniden kodlanır, ses olduğu gibi kopyalanır."""
        video = self._video(url)
        if not layers:
            raise ValueError("This video has no drawings")
        tool_dir = self.cfg["clips"].get("ffmpeg_dir", "")
        ff = tool_path("ffmpeg", tool_dir)
        if not ff:
            raise RuntimeError("ffmpeg not found. Set clips.ffmpeg_dir in config.toml.")
        out_dir = clips_dir(self.cfg) / DRAWN_DIR
        out_dir.mkdir(parents=True, exist_ok=True)
        dst = out_dir / f"{video.stem}-cizimli.mp4"

        with tempfile.TemporaryDirectory() as tmp:
            inputs, chains, last = [], [], "0:v"
            for i, layer in enumerate(layers, 1):
                png = Path(tmp) / f"{i}.png"
                png.write_bytes(base64.b64decode(layer["png"].split(",")[-1]))
                inputs += ["-i", str(png)]
                start, end = float(layer["start"]), float(layer["end"])
                chains.append(f"[{last}][{i}:v]overlay=0:0:enable='between(t,{start:.3f},{end:.3f})'[v{i}]")
                last = f"v{i}"
            script = Path(tmp) / "filter.txt"  # çok çizimde komut satırı sınırına takılmasın
            script.write_text(";".join(chains), encoding="utf-8")
            base = [ff, "-y", "-loglevel", "error", "-i", str(video), *inputs,
                    "-/filter_complex", str(script), "-map", f"[{last}]", "-map", "0:a?"]
            for enc in ("h264_nvenc", "libx264"):
                r = subprocess.run([*base, *ENCODERS[enc], "-c:a", "aac", "-b:a", "160k",
                                    "-movflags", "+faststart", str(dst)], capture_output=True,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
                if r.returncode == 0:
                    break
            else:
                raise RuntimeError(f"Export failed: {r.stderr.decode(errors='replace')[-300:]}")
        subprocess.Popen(["explorer", "/select,", str(dst)])
        return str(dst)

    def reveal_video(self, url: str):
        subprocess.Popen(["explorer", "/select,", str(self._video(url))])

    # --- canlı: şampiyon seçimi önerileri ve oyuncu bilgisi ---
    def _riot(self) -> RiotApi:
        r = self.cfg["riot"]
        return RiotApi(r["api_key"], r["platform"])

    def _start_collector(self) -> None:
        self._collector = stats.Collector(self.cfg, self._log)
        threading.Thread(target=self._collector.run, daemon=True).start()

    def live(self):
        """Live sekmesinin durumu: oyun fazı, şampiyon seçimindeki şampiyonun, oyundaki 10 oyuncu."""
        phase = self._lcu.phase()
        out = {"phase": phase, "pick": None, "scout": None, "ddragon": ddragon.version(),
               "stats": {**stats.summary(), "collector": self._collector.state if self._collector else "off"}}
        if phase == "ChampSelect":
            s = self._lcu.get("/lol-champ-select/v1/session") or {}
            me = next((p for p in s.get("myTeam", []) if p.get("cellId") == s.get("localPlayerCellId")), None)
            if me:
                # şampiyon seçmeden önce de dolu: karşı pick'ler tam o anda lazım
                cid = me.get("championId") or me.get("championPickIntent") or None
                role = POSITIONS.get(me.get("assignedPosition") or "", "")
                # rakip takımın şampiyonları seçimde herkese görünür (isimleri ve rolleri değil);
                # koridor rakibi, rakip şampiyonlar rollere birlikte dağıtılarak tahmin edilir
                enemies = [p["championId"] for p in s.get("theirTeam", []) if p.get("championId")]
                roles = stats.assign_roles(enemies) if role else {}
                guess = next((e for e, r in roles.items() if r == role), None)
                out["pick"] = {"champion_id": cid, "role": role, "locked": bool(me.get("championId")),
                               "enemies": enemies, "opponent": guess}
        if self._scout:
            out["scout"] = self._scout.snapshot()
        return out

    def _live_loop(self) -> None:
        """Live sekmesi açık olmasa da: oyun başlayınca 10 oyuncunun bilgisini toplar ve
        maç kayıtlarının altında gösterilmek üzere games/<maç>.json'a yazar."""
        saved = ""
        while True:
            try:
                phase = self._lcu.phase()
                if phase in ("InProgress", "Reconnect"):
                    game = (self._lcu.get("/lol-gameflow/v1/session") or {}).get("gameData", {}).get("gameId") or 0
                    sc = self._scout
                    if not sc or sc.source != "live" or (game and sc.game != game):
                        self._start_live_scout(game)
                sc = self._scout
                if sc and sc.source == "live" and sc.game:
                    snap = sc.snapshot()
                    body = json.dumps(snap["players"], sort_keys=True)
                    if body != saved:  # değiştikçe yaz; oyun bitince son hâli kalır
                        GAMES.mkdir(exist_ok=True)
                        mid = f"{self.cfg['riot']['platform'].upper()}_{sc.game}"
                        (GAMES / f"{mid}.json").write_text(json.dumps(
                            {"match_id": mid, "saved": time.time(), "players": snap["players"]},
                            ensure_ascii=False), encoding="utf-8")
                        saved = body
            except Exception as e:  # döngü ölmesin
                self._log(f"Live players: {e}")
            time.sleep(3)

    def _start_live_scout(self, game: int) -> None:
        # 2 sn'lik yoklamalar üst üste binerse ikinci bir bilgi toplayıcı başlatıp
        # yüklenmiş ligleri sıfırlamasın
        if not self._scout_lock.acquire(blocking=False):
            return
        try:
            sc = self._scout
            if sc and sc.source == "live" and (not game or sc.game == game):
                return
            players = scout.live_players(self._lcu, self._riot())
            if players:
                self._start_scout(players, "live", game)
        except Exception as e:  # ağ hatası vb.; sonraki yoklamada yeniden denenir
            self._log(f"Live players: {e}")
        finally:
            self._scout_lock.release()

    def _start_scout(self, players: list[dict], source: str, game: int = 0) -> None:
        if self._scout:
            self._scout.stop()
        self._scout = scout.Scout(self._riot(), players, source)
        self._scout.game = game

    def scout_preview(self):
        """Canlı oyun yokken ekranı denemek için son maçın 10 oyuncusu."""
        api = self._riot()
        r = self.cfg["riot"]
        if not self._puuid:
            self._puuid = api.puuid(r["game_name"], r["tag_line"])
        mid = api.last_match_id(self._puuid)
        info = api.match(mid) if mid else None
        if not info:
            raise RuntimeError("No recent match found")
        stats.store(info, "scout")
        self._start_scout(scout.match_players(info, self._puuid), "preview")

    def suggest(self, champion_id: int, role: str = "", opponent_id: int | None = None,
                enemies: list | None = None):
        """Genel öneri; `opponent_id` verilirse ayrıca `matchup` (yalnızca o rakibe karşı).
        `enemies` (seçimde kilitlenen rakipler) verilirse rakip takımın AP/AD grubu işaretlenir."""
        s = stats.suggest(int(champion_id), role, int(opponent_id) if opponent_id else None,
                          [int(e) for e in enemies or []])
        champs = ddragon.champions()
        self._names(s)
        items = ddragon.items()
        for g in s["vs_damage"]["groups"]:
            for key in ("core", "boots", "situational", "more"):
                for it in g[key]:
                    it["name"] = items.get(it["id"], {}).get("name", str(it["id"]))
        if s["matchup"]:
            self._names(s["matchup"])
            o = champs.get(int(opponent_id), {})
            s["matchup"].update(champion=o.get("id", ""), champion_name=o.get("name", ""))
        c = champs.get(int(champion_id), {})
        return {**s, "champion": c.get("id", ""), "champion_name": c.get("name", ""),
                "champion_id": int(champion_id), "role_name": ROLE_NAMES.get(s["role"], "")}

    def item_table(self, champion_id: int, role: str = "", opponent_id: int | None = None,
                   ap_min: float = 0.0, ap_max: float = 1.0, enemies: list | None = None,
                   center: float | None = None):
        """Koridor rakibine ve/veya rakip takımın AP oranına göre item tablosu; `center` verilirse
        aralık o AP oranının etrafında 20 maç olana kadar kendiliğinden genişler."""
        t = stats.item_table(int(champion_id), role, int(opponent_id) if opponent_id else None,
                             float(ap_min), float(ap_max), [int(e) for e in enemies or []],
                             float(center) if center is not None else None)
        items = ddragon.items()
        for key in ("boots", "items", "more"):
            for it in t[key]:
                it["name"] = items.get(it["id"], {}).get("name", str(it["id"]))
        champs = ddragon.champions()
        for k, cid in (("champion", t["champion"]), ("opponent", t["opponent"])):
            c = champs.get(cid, {}) if cid else {}
            t[f"{k}_key"], t[f"{k}_name"] = c.get("id", ""), c.get("name", "")
        return t

    def counters(self, opponent_id: int, role: str):
        """Koridor rakibine karşı en iyi / en kötü pick'ler (yüksek elo maçlarından)."""
        c = stats.counters(int(opponent_id), role)
        champs = ddragon.champions()
        for r in c["best"] + c["worst"]:
            ch = champs.get(r["id"], {})
            r.update(key=ch.get("id", ""), name=ch.get("name", str(r["id"])))
        o = champs.get(int(opponent_id), {})
        return {**c, "opponent_key": o.get("id", ""), "opponent_name": o.get("name", ""),
                "role_name": ROLE_NAMES.get(role, role)}

    @staticmethod
    def _names(s: dict) -> None:
        """Rün ve item kimliklerine ad ve görsel ekler."""
        names = ddragon.runes()
        items = ddragon.items()
        for page in s["runes"]:
            page["names"] = [names.get(i, {}).get("name", str(i)) for i in page["perks"]]
            page["icons"] = [names.get(i, {}).get("icon", "") for i in page["perks"]]
            page["tree_names"] = [names.get(page["primary"], {}).get("name", ""),
                                  names.get(page["sub"], {}).get("name", "")]
            page["tree_icons"] = [names.get(page["primary"], {}).get("icon", ""),
                                  names.get(page["sub"], {}).get("icon", "")]
            page["shard_names"] = [ddragon.SHARDS.get(i, str(i)) for i in page["shards"]]
        for key in ("core", "boots", "situational"):
            for it in s[key]:
                it["name"] = items.get(it["id"], {}).get("name", str(it["id"]))
        for key in ("start", "order"):
            for g in s[key]:
                for it in g["items"]:
                    it["name"] = items.get(it["id"], {}).get("name", str(it["id"]))

    def champions(self):
        return sorted(({"id": k, "key": v["id"], "name": v["name"]} for k, v in ddragon.champions().items()),
                      key=lambda c: c["name"])

    def _title(self, s: dict, vs: bool = False) -> str:
        t = f"{PREFIX}{s['champion_name']} {s['role_name']}".strip()
        return f"{t} vs {s['matchup']['champion_name']}" if vs else t

    def _pick(self, champion_id: int, role: str, opponent_id: int | None) -> tuple[dict, dict, bool]:
        """(tam öneri, kullanılacak kısım, eşleşme mi). Rakip verildiyse ve eşleşme verisi varsa o."""
        s = self.suggest(champion_id, role, opponent_id)
        m = s["matchup"]
        if opponent_id and m and m["games"]:
            return s, m, True
        return s, s, False

    def import_runes(self, champion_id: int, role: str = "", index: int = 0, opponent_id: int | None = None):
        """Seçilen rün sayfasını (öneri listesindeki sırası) client'a yazar; `opponent_id`
        verilirse eşleşme önerisinden. Önce uygulamanın kendi "LoL HL" sayfasını günceller;
        yoksa yeni sayfa açar; yer yoksa açık olan (ya da ilk) düzenlenebilir sayfanın üstüne yazar."""
        s, src, vs = self._pick(champion_id, role, opponent_id)
        if not src["runes"]:
            raise RuntimeError("No rune data for this champion yet")
        page = src["runes"][min(max(int(index), 0), len(src["runes"]) - 1)]
        pages = self._lcu.get("/lol-perks/v1/pages")
        if pages is None:
            raise RuntimeError("LoL client is not open")
        body = {"name": self._title(s, vs), "primaryStyleId": page["primary"], "subStyleId": page["sub"],
                "selectedPerkIds": page["perks"] + page["shards"], "current": True}
        editable = [p for p in pages if p.get("isEditable")]
        ours = next((p for p in editable if p.get("name", "").startswith(PREFIX)), None)
        can_add = (self._lcu.get("/lol-perks/v1/inventory") or {}).get("canAddCustomPage", True)
        target = ours or (None if can_add else next((p for p in editable if p.get("current")), None)
                          or (editable[0] if editable else None))
        if target:
            r = self._lcu.call("PUT", f"/lol-perks/v1/pages/{target['id']}", {**target, **body})
            if r is not None and r.ok:
                self._lcu.call("PUT", "/lol-perks/v1/currentpage", target["id"])
        else:
            r = self._lcu.call("POST", "/lol-perks/v1/pages", body)
        if r is None or not r.ok:
            raise RuntimeError(f"Client refused the rune page ({r.status_code if r is not None else 'closed'})")
        replaced = target and target is not ours
        return f"{body['name']}" + (f" (replaced your page \"{target.get('name', '')}\")" if replaced else "")

    def import_items(self, champion_id: int, role: str = "", opponent_id: int | None = None):
        """Önerilen item yolunu client'a item seti olarak ekler (mağazada önerilenlerin yanında);
        `opponent_id` verilirse eşleşme önerisinden."""
        s, src, vs = self._pick(champion_id, role, opponent_id)
        if not src["core"]:
            raise RuntimeError("No item data for this champion yet")
        me = self._lcu.get("/lol-summoner/v1/current-summoner")
        if not me:
            raise RuntimeError("LoL client is not open")
        path = f"/lol-item-sets/v1/item-sets/{me['summonerId']}/sets"
        data = self._lcu.get(path) or {"accountId": me.get("accountId"), "itemSets": []}
        blk = lambda name, its: {"type": name, "items": [{"id": str(i["id"]), "count": i.get("count", 1)} for i in its]}  # noqa: E731
        # timeline verisi varsa: en sık başlangıç seti ve en sık item sırası; yoksa en çok yapılanlar
        start = [blk("Starting items", src["start"][0]["items"])] if src["start"] else []
        core = (blk("Build order", src["order"][0]["items"]) if src["order"]
                else blk(f"Core ({src['games']} high-elo games)", src["core"]))
        # aynı şampiyon için önceki setimizi değiştir, diğer setlere dokunma
        data["itemSets"] = [x for x in data.get("itemSets", []) if not x.get("title", "").startswith(PREFIX)
                            or int(champion_id) not in x.get("associatedChampions", [])]
        data["itemSets"].append({
            "title": self._title(s, vs), "type": "custom", "map": "any", "mode": "any", "uid": str(uuid.uuid4()),
            "associatedChampions": [int(champion_id)], "associatedMaps": [11], "sortrank": 0,
            "startedFrom": "blank", "preferredItemSlots": [],
            "blocks": [*start, core, blk("Boots", src["boots"]), blk("Situational", src["situational"])],
        })
        r = self._lcu.call("PUT", path, data)
        if r is None or not r.ok:
            raise RuntimeError(f"Client refused the item set ({r.status_code if r is not None else 'closed'})")
        return self._title(s, vs)

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
        if self._collector:
            self._collector.update(self.cfg)
        # yeni ayarlar watcher yeniden başlayınca geçerli olur
        busy = self._watcher and self._watcher.state in ("recording", "processing")
        if not busy and self._thread and self._thread.is_alive():
            self._watcher.stop(close_obs=False)
            self._thread.join(timeout=10)
            self.start()
        return {"restarted": not busy}


def main() -> None:
    os.chdir(ROOT)  # config'teki göreli yollar (clips/) proje köküne göre
    api = Api()
    window = webview.create_window(
        "LoL Highlights", f"http://127.0.0.1:{api.port}/", js_api=api,
        width=1280, height=800, min_size=(960, 600), background_color="#0e1015",
    )
    api._window = window

    def on_closing():
        if api._watcher and api._watcher.state == "recording":
            return window.create_confirmation_dialog(
                "Recording in progress", "A match is being recorded. If you close, recording stops and clips "
                "will be cut on next start. Close anyway?")
        return True

    window.events.closing += on_closing
    api.start()
    api._start_collector()
    threading.Thread(target=api._live_loop, daemon=True).start()
    webview.start(gui="edgechromium", debug=os.environ.get("LOLHL_DEBUG") == "1")
    api.stop()
    # watcher thread'i daemon; OBS'i kapatabilmesi için kısa süre bekle
    if api._thread and api._thread.is_alive():
        api._thread.join(timeout=15)


if __name__ == "__main__":
    main()
