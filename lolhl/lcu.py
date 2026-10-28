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
        if self._s is None and not self._connect():
            return None
        try:
            r = self._s.get(self._base + path, timeout=3)
        except requests.RequestException:
            self._s = None  # client yeniden başlamış olabilir
            return None
        return r.json() if r.ok else None

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
