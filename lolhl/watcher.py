"""Arka planda çalışır: maç başlayınca kaydı açar, bitince klipleri çıkarır.
Oyun içinde hiçbir arayüz göstermez."""
from __future__ import annotations

import json
import statistics
import threading
import time
from pathlib import Path
from typing import Callable

from .lcu import Lcu, live_game_time, live_skin
from .pipeline import process
from .recorder import ObsRecorder, close_obs, ensure_obs
from .riot_api import RiotApi

MAX_TRIES = 3  # pratik modu gibi API'ye hiç düşmeyen maçlar sonsuza dek denenmesin

Log = Callable[[str], None]


class Watcher:
    """`run()` bloklar; UI bunu bir thread'de çalıştırıp `state`'i okur."""

    def __init__(self, cfg: dict, log: Log = print):
        self.cfg = cfg
        self.log = log
        self.state = "stopped"  # stopped, starting, waiting, recording, processing, error
        self.error = ""
        self._stop = threading.Event()
        self._close_obs = False

    def stop(self, close_obs: bool = True) -> None:
        """close_obs=False: ayar değişikliği gibi hemen yeniden başlatılacak durumlar."""
        self._close_obs = close_obs
        self._stop.set()

    def _set(self, state: str, msg: str | None = None) -> None:
        self.state = state
        if msg:
            self.log(msg)

    def handle(self, sidecar: Path, timeout: float) -> None:
        """Bir kaydı işler ve sonucu yan .json dosyasına yazar."""
        meta = json.loads(sidecar.read_text())
        video = meta.get("video") or str(sidecar.with_suffix(".mp4"))
        try:
            process(self.api.load(meta["match_id"], self.puuid, timeout), video,
                    meta["offset"], self.cfg, self.log, skin=meta.get("skin"))
            meta["processed"] = True
        except Exception as e:  # watcher ölmesin
            meta["tries"] = meta.get("tries", 0) + 1
            self.log(f"Processing error ({meta['tries']}/{MAX_TRIES}): {e}")
        sidecar.write_text(json.dumps(meta))

    def process_pending(self) -> None:
        """Önceden işlenememiş kayıtları (watcher erken kapandıysa vb.) tamamlar."""
        for sidecar in sorted(self.rec_dir.glob("*.json")):
            meta = json.loads(sidecar.read_text())
            if meta.get("processed") or meta.get("tries", 0) >= MAX_TRIES:
                continue
            if not Path(meta.get("video") or sidecar.with_suffix(".mp4")).exists():
                continue
            self._set("processing", f"Processing pending recording: {sidecar.stem}")
            self.handle(sidecar, timeout=0)

    def cleanup(self) -> None:
        """En yeni `keep` maç kaydı dışındaki, işi bitmiş kayıtları siler.
        Yalnızca bu programın yazdığı (.json'u olan) kayıtlara dokunur."""
        keep = self.cfg["obs"].get("keep_recordings", 1)
        sidecars = sorted(self.rec_dir.glob("*.json"),
                          key=lambda p: p.stat().st_mtime, reverse=True)
        for sidecar in sidecars[keep:]:
            meta = json.loads(sidecar.read_text())
            if not (meta.get("processed") or meta.get("tries", 0) >= MAX_TRIES):
                continue  # klipleri henüz çıkmadı, silme
            video = Path(meta.get("video") or sidecar.with_suffix(".mp4"))
            video.unlink(missing_ok=True)
            video.with_name(video.name + ".notes.json").unlink(missing_ok=True)  # çizimleri
            sidecar.unlink()
            self.log(f"Old recording deleted: {video.name}")

    def run(self) -> None:
        try:
            self._run()
            self._set("stopped", "Stopped.")
        except Exception as e:
            self.error = str(e)
            self._set("error", f"Error: {e}")
        if self._stop.is_set() and self._close_obs:  # kullanıcı durdurduysa OBS'i de kapat
            try:
                if close_obs(self.cfg["obs"]):
                    self.log("OBS closed.")
            except Exception as e:
                self.log(f"Could not close OBS: {e}")

    def _run(self) -> None:
        cfg = self.cfg
        self._set("starting")
        lcu = Lcu(cfg["lol"]["install_dir"])
        self.api = RiotApi(cfg["riot"]["api_key"], cfg["riot"]["platform"])
        self.puuid = self.api.puuid(cfg["riot"]["game_name"], cfg["riot"]["tag_line"])
        self.rec_dir = Path(cfg["obs"]["recordings_dir"])
        rec: ObsRecorder | None = None

        self.process_pending()
        self.cleanup()
        self._set("waiting", "Waiting for a match...")
        while not self._stop.is_set():
            phase = lcu.phase()
            if phase in ("ChampSelect", "GameStart", "InProgress"):
                # şampiyon seçiminde açılırsa maç başlayana kadar hazır olur
                try:
                    rec and rec.elapsed()
                except Exception:
                    rec = None  # OBS kapatılmış
                if rec is None:
                    rec = ensure_obs(cfg["obs"])
                    self.log("OBS ready.")
            if phase != "InProgress":
                self._stop.wait(2)
                continue

            game_id = lcu.game_id()
            rec.start()
            self._set("recording", f"Match started (gameId={game_id}), recording.")

            # video_zamanı - oyun_zamanı farkını maç boyunca ölç. Yükleme ekranı
            # sırasında Live Client'ın saati oyun saatiyle uyuşmayabiliyor (bir maçta
            # 63 sn sapma görüldü), bu yüzden oyun saati 90 sn'yi geçtikten sonraki
            # örnekler esas alınır.
            samples: list[tuple[float, float]] = []
            skin = None  # kart görseli için oynadığın skin (maç geçmişinde yok)
            while lcu.phase() == "InProgress" and not self._stop.is_set():
                gt = live_game_time()
                if gt is not None and gt > 0:
                    samples.append((gt, rec.elapsed() - gt))
                    if skin is None:
                        skin = live_skin(cfg["riot"]["game_name"], cfg["riot"]["tag_line"])
                self._stop.wait(5)

            video = rec.stop()
            if not samples:
                self._set("waiting", "Could not read game time (remake/early exit?), skipping.")
                continue
            settled = [o for gt, o in samples if gt >= 90] or [o for _, o in samples]
            offset = statistics.median(settled)
            spread = max(settled) - min(settled)
            if spread > 3:
                self.log(f"Warning: clock offset samples varied by {spread:.1f} s; clips may be shifted.")
            sidecar = Path(video).with_suffix(".json")
            sidecar.write_text(json.dumps({"match_id": self.api.match_id_from_game_id(game_id),
                                           "offset": offset, "video": video, "skin": skin,
                                           "samples": samples[:: max(1, len(samples) // 60)]}))
            if self._stop.is_set():
                # maç ortasında durdurulduysa sonraki açılışta işlenir
                self.log("Recording saved; clips will be cut on next start.")
                break

            self._set("processing", "Match ended, preparing clips...")
            self.handle(sidecar, timeout=300)
            self.process_pending()
            self.cleanup()
            self._set("waiting", "Waiting for a match...")


def run(cfg: dict) -> None:
    print("Press Ctrl+C to quit")
    Watcher(cfg).run()
