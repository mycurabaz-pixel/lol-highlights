"""LoL Client (LCU) ve Live Client Data API — ikisi de Riot'un resmi yerel API'leri.

Oyuna/belleğe dokunmaz; sadece localhost HTTP istekleri.
"""
from __future__ import annotations

import base64
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class Lcu:
    def __init__(self, install_dir: str):
        self.lockfile = Path(install_dir) / "lockfile"
        self._s: requests.Session | None = None
        self._base = ""

    def _connect(self) -> bool:
        if not self.lockfile.exists():
            self._s = None
            return False
        _, _, port, password, proto = self.lockfile.read_text().split(":")
        self._base = f"{proto}://127.0.0.1:{port}"
        self._s = requests.Session()
        self._s.verify = False
        tok = base64.b64encode(f"riot:{password}".encode()).decode()
        self._s.headers["Authorization"] = f"Basic {tok}"
        return True

    def get(self, path: str):
        r = self.call("GET", path)
        return r.json() if r is not None and r.ok else None

    def call(self, method: str, path: str, body=None) -> requests.Response | None:
        """Ham istek; client kapalıysa None. Durum koduna bakmak gerekince kullanılır."""
        if self._s is None and not self._connect():
            return None
        try:
            return self._s.request(method, self._base + path, json=body, timeout=5)
        except requests.RequestException:
            self._s = None  # client yeniden başlamış olabilir
            return None

    def phase(self) -> str | None:
        """None, Lobby, ChampSelect, GameStart, InProgress, EndOfGame, ..."""
        return self.get("/lol-gameflow/v1/gameflow-phase")

    def game_id(self) -> int | None:
        s = self.get("/lol-gameflow/v1/session")
        return (s or {}).get("gameData", {}).get("gameId") or None

    def me(self) -> tuple[str, str] | None:
        s = self.get("/lol-summoner/v1/current-summoner")
        if not s:
            return None
        return s["gameName"], s["tagLine"]


def live_game_time() -> float | None:
    """Oyun sırasında oyun saatini (saniye) döner, yoksa None."""
    try:
        r = requests.get("https://127.0.0.1:2999/liveclientdata/gamestats",
                         verify=False, timeout=1)
        return float(r.json()["gameTime"]) if r.ok else None
    except (requests.RequestException, KeyError, ValueError):
        return None


def live_skin(game_name: str, tag_line: str) -> int | None:
    """Oyun sırasında oynadığın skinin numarası (0 = varsayılan), yoksa None.
    Riot'un Live Client Data API'si; maç geçmişinde skin bilgisi tutulmuyor."""
    try:
        r = requests.get("https://127.0.0.1:2999/liveclientdata/playerlist", verify=False, timeout=1)
        if not r.ok:
            return None
        me = f"{game_name}#{tag_line}".lower()
        for p in r.json():
            if (p.get("riotId") or "").lower() == me or (
                    (p.get("riotIdGameName") or "").lower() == game_name.lower()
                    and (p.get("riotIdTagLine") or "").lower() == tag_line.lower()):
                return int(p.get("skinID", 0)) % 1000  # tam kimlik gelirse (84003) numaraya indir
    except (requests.RequestException, ValueError, TypeError):
        pass
    return None
