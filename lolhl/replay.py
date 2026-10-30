"""Tekrarlar (.rofl): maç geçmişinden indirme/açma, tekrar sırasında kamera/zaman/kayıt.

Hepsi Riot'un resmi yerel API'leri (localhost HTTP), Porofessor/Blitz'in yolu:
  - LCU: maç geçmişi, tekrar indirme ve açma
  - Replay API (127.0.0.1:2999, game.cfg'de EnableReplayApi=1): kamera, zaman, kayıt
Oyuna/belleğe dokunmaz, oyunun üstüne bir şey çizmez. .rofl dosyasından yalnızca açık
metin metadata (oyuncular, KDA) okunur; şifreli oyun verisi okunmaz (Riot şartlarına aykırı).
"""
from __future__ import annotations

import json
import os
import re
import time
import winreg
from pathlib import Path

import requests

from .lcu import Lcu

REPLAY_API = "https://127.0.0.1:2999"
COMPONENT = {"componentType": "replay-button_match-history"}

# Riot saatte 10 indirmeye izin veriyor (26.21); client'tan elle indirmelere yer kalsın.
DOWNLOAD_LIMIT = 6
HOUR = 3600

QUEUES = {
    400: "Normal Draft", 420: "Ranked Solo/Duo", 430: "Normal Blind", 440: "Ranked Flex",
    450: "ARAM", 480: "Swiftplay", 490: "Quickplay", 700: "Clash", 900: "URF", 1700: "Arena",
}

# Sihirdar Vadisi koordinatları (x/z zemin, y yükseklik). Kaba kadraj noktaları;
# oyundaki serbest kamera her zaman kullanılabilir.
CAMERA_PRESETS = {
    "top": (2200, 12600), "mid": (7400, 7400), "bot": (12600, 2200),
    "dragon": (9850, 4400), "baron": (5000, 10450),
    "blue_base": (1600, 1600), "red_base": (13200, 13200),
}
CAMERA_HEIGHT = 2400


def documents_dir() -> Path:
    """Belgeler klasörü (OneDrive'a taşınmış olabilir)."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
            return Path(os.path.expandvars(winreg.QueryValueEx(k, "Personal")[0]))
    except OSError:
        return Path.home() / "Documents"


# ---------- LCU: maç geçmişi, indirme, açma ----------

class ReplayClient:
    def __init__(self, lcu: Lcu, state_file: Path):
        self.lcu = lcu
        self.state_file = state_file
        self._champs: dict[int, str] = {}

    def status(self) -> dict:
        me = self.lcu.get("/lol-summoner/v1/current-summoner")
        if not me:
            return {"connected": False}
        return {"connected": True, "name": f'{me["gameName"]}#{me["tagLine"]}'}

    def folders(self) -> dict:
        docs = documents_dir() / "League of Legends"
        return {
            "replays": self.lcu.get("/lol-replays/v1/rofls/path") or str(docs / "Replays"),
            "highlights": self.lcu.get("/lol-highlights/v1/highlights-folder-path") or str(docs / "Highlights"),
        }

    def _champion_names(self) -> dict[int, str]:
        if not self._champs:
            data = self.lcu.get("/lol-game-data/assets/v1/champion-summary.json") or []
            self._champs = {c["id"]: c["name"] for c in data}
        return self._champs

    def replay_state(self, game: dict) -> dict:
        """Client bir maçın tekrar durumunu ancak metadata oluşunca bilir; maç geçmişi
        ekranı da böyle oluşturur."""
        path = f"/lol-replays/v1/metadata/{game['gameId']}"
        r = self.lcu.call("GET", path)
        if r is not None and r.status_code == 404 and game.get("queueId") is not None:
            self.lcu.call("POST", f"/lol-replays/v2/metadata/{game['gameId']}/create", {
                "gameId": game["gameId"], "gameType": game.get("gameType", ""),
                "queueId": game["queueId"], "gameEnd": game.get("gameEnd", 0)})
            r = self.lcu.call("GET", path)
        if r is None:
            raise RuntimeError("LoL client is not open")
        return r.json() if r.ok else {"state": "error"}

    def matches(self, count: int = 20) -> list[dict]:
        names = self._champion_names()
        res = self.lcu.get(f"/lol-match-history/v1/products/lol/current-summoner/matches"
                           f"?begIndex=0&endIndex={count - 1}")
        if res is None:
            raise RuntimeError("LoL client is not open")
        out = []
        for g in res.get("games", {}).get("games", []):
            me = (g.get("participants") or [{}])[0]
            st = me.get("stats", {})
            item = {
                "gameId": g["gameId"], "gameType": g.get("gameType", ""), "queueId": g.get("queueId"),
                "queue": QUEUES.get(g.get("queueId"), g.get("gameMode", "")),
                "created": g["gameCreation"] / 1000, "duration": g.get("gameDuration", 0),
                "gameEnd": g["gameCreation"] + g.get("gameDuration", 0) * 1000,
                "champion": names.get(me.get("championId"), f'#{me.get("championId")}'),
                "win": bool(st.get("win")),
                "kda": [st.get("kills", 0), st.get("deaths", 0), st.get("assists", 0)],
            }
            try:
                item["replay"] = self.replay_state(item)
            except Exception:
                item["replay"] = {"state": "error"}
            out.append(item)
        return out

    # --- saatlik indirme sınırı ---
    def _recent_downloads(self) -> list[float]:
        try:
            log = json.loads(self.state_file.read_text())
        except (OSError, json.JSONDecodeError):
            log = []
        return [t for t in log if t > time.time() - HOUR]

    def quota(self) -> dict:
        recent = self._recent_downloads()
        free_at = min(recent) + HOUR if len(recent) >= DOWNLOAD_LIMIT else None
        return {"used": len(recent), "limit": DOWNLOAD_LIMIT, "free_at": free_at}

    def download(self, game: dict) -> dict:
        q = self.quota()
        if q["used"] >= q["limit"]:
            mins = int((q["free_at"] - time.time()) // 60) + 1
            raise RuntimeError(f"Hourly download limit reached ({q['limit']}). Try again in {mins} min.")
        self.replay_state(game)
        r = self.lcu.call("POST", f"/lol-replays/v1/rofls/{game['gameId']}/download/graceful", COMPONENT)
        if r is None or not r.ok:
            raise RuntimeError("Could not start the download" + (f" ({r.status_code})" if r is not None else ""))
        self.state_file.write_text(json.dumps([*self._recent_downloads(), time.time()]))
        return self.quota()

    def watch(self, game_id: int) -> None:
        self.lcu.call("POST", "/lol-replays/v1/rofls/scan")
        r = self.lcu.call("POST", f"/lol-replays/v1/rofls/{game_id}/watch", COMPONENT)
        if r is None:
            raise RuntimeError("The LoL client must be open to watch a replay")
        if not r.ok:
            msg = (r.json() or {}).get("message", "") if r.content else ""
            raise RuntimeError(f"Could not open the replay ({r.status_code}) {msg}".strip())


# ---------- .rofl metadata ----------

def _rofl_metadata(path: Path) -> tuple[dict, str] | None:
    """ROFL2 (güncel): dosya, metadata JSON'u ve ardından uzunluğu (uint32 LE) ile biter;
    oyun sürümü başta açık metin durur. ROFL1 (eski): ofsetler 262. bayttaki başlıkta."""
    with open(path, "rb") as f:
        head = f.read(64)
        if head[:4] != b"RIOT":
            return None
        size = f.seek(0, 2)
        if head[4] == 2:
            f.seek(size - 4)
            n = int.from_bytes(f.read(4), "little")
            if not 0 < n < min(size, 4 << 20):
                return None
            f.seek(size - 4 - n)
            m = re.search(rb"\d+\.\d+\.\d+\.\d+", head)
            return json.loads(f.read(n)), m.group().decode() if m else ""
        f.seek(262 + 6)
        offset = int.from_bytes(f.read(4), "little")
        n = int.from_bytes(f.read(4), "little")
        if not 0 < n < 4 << 20 or offset + n > size:
            return None
        f.seek(offset)
        return json.loads(f.read(n)), ""


def parse_rofl(path: Path) -> dict | None:
    try:
        res = _rofl_metadata(path)
    except (OSError, ValueError):
        return None
    if not res:
        return None
    meta, version = res
    stats = meta.get("statsJson") or []
    if isinstance(stats, str):
        stats = json.loads(stats)
    return {
        "length": meta.get("gameLength", 0) / 1000,
        "version": meta.get("gameVersion") or version,
        "players": [{
            "name": p.get("RIOT_ID_GAME_NAME") or p.get("NAME", ""),
            "champion": p.get("SKIN", ""),
            "team": "red" if p.get("TEAM") == "200" else "blue",
            "win": p.get("WIN") == "Win",
            "kda": [int(p.get("CHAMPIONS_KILLED") or 0), int(p.get("NUM_DEATHS") or 0),
                    int(p.get("ASSISTS") or 0)],
        } for p in stats],
    }


def list_replays(folder: Path) -> list[dict]:
    out = []
    for p in folder.glob("*.rofl"):
        m = re.fullmatch(r"([A-Z0-9]+)-(\d+)\.rofl", p.name, re.I)
        st = p.stat()
        out.append({"name": p.name, "gameId": int(m.group(2)) if m else None,
                    "size": st.st_size, "mtime": st.st_mtime, "meta": parse_rofl(p)})
    return sorted(out, key=lambda r: r["mtime"], reverse=True)


# ---------- Replay API: tekrar açıkken kamera, zaman, kayıt ----------

def _replay(method: str, path: str, body=None):
    r = requests.request(method, REPLAY_API + path, json=body, verify=False, timeout=3)
    r.raise_for_status()
    return r.json() if r.content else None


def replay_status() -> dict:
    try:
        return {"running": True, "playback": _replay("GET", "/replay/playback"),
                "render": _replay("GET", "/replay/render")}
    except requests.RequestException:
        return {"running": False}


def playback(patch: dict):
    return _replay("POST", "/replay/playback", patch)


def render(patch: dict):
    return _replay("POST", "/replay/render", patch)


def recording(patch: dict | None = None):
    return _replay("POST", "/replay/recording", patch) if patch else _replay("GET", "/replay/recording")


def players() -> list[dict]:
    return [{
        "name": p.get("riotIdGameName") or p.get("summonerName", "").split("#")[0],
        "full": p.get("riotId") or p.get("summonerName", ""),
        "champion": p.get("championName", ""),
        "team": "red" if p.get("team") == "CHAOS" else "blue",
    } for p in _replay("GET", "/liveclientdata/playerlist")]


def follow(names: list[str]) -> bool:
    """Replay API'nin beklediği birim adı yamadan yamaya değişti (sihirdar adı, Riot ID,
    Riot ID#tag); sırayla dener, kabul edileni bırakır."""
    for name in filter(None, names):
        render({"selectionName": name, "cameraAttached": True})
        if (_replay("GET", "/replay/render").get("selectionName") or "").lower() == name.lower():
            return True
    return False


def camera(preset: str):
    x, z = CAMERA_PRESETS[preset]
    return render({"cameraAttached": False, "cameraMode": "top",
                   "cameraPosition": {"x": x, "y": CAMERA_HEIGHT, "z": z}})


def _game_cfg(install_dir: str) -> Path:
    return Path(install_dir) / "Config" / "game.cfg"


def replay_api_enabled(install_dir: str) -> bool:
    try:
        text = _game_cfg(install_dir).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return re.search(r"^EnableReplayApi\s*=\s*1\s*$", text, re.M) is not None


def enable_replay_api(install_dir: str) -> bool:
    """[General] altına EnableReplayApi=1 ekler (Riot'un belgelediği yol). İlk seferde
    game.cfg.bak yedeği alır. Değişiklik yaptıysa True."""
    cfg = _game_cfg(install_dir)
    raw = cfg.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    if re.search(r"^EnableReplayApi\s*=\s*1\s*$", text, re.M):
        return False
    backup = cfg.with_name("game.cfg.bak")
    if not backup.exists():
        backup.write_bytes(raw)
    eol = "\r\n" if "\r\n" in text else "\n"
    if re.search(r"^EnableReplayApi[ \t]*=", text, re.M):
        text = re.sub(r"^EnableReplayApi[ \t]*=[^\r\n]*", "EnableReplayApi=1", text, count=1, flags=re.M)
    else:
        text, n = re.subn(r"^\[General\](?=[ \t\r]*$)", lambda m: m.group() + eol + "EnableReplayApi=1",
                          text, count=1, flags=re.M)
        if not n:
            text = f"[General]{eol}EnableReplayApi=1{eol}" + text
    cfg.write_bytes(text.encode("utf-8"))
    return True
