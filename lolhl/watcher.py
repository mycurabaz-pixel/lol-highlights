"""Arka planda çalışır: maç başlayınca kaydı açar, bitince klipleri çıkarır.
Oyun içinde hiçbir arayüz göstermez."""
from __future__ import annotations

import json
import statistics
import threading
import time
from pathlib import Path
from typing import Callable

from .lcu import Lcu, live_game_time
from .pipeline import process
from .recorder import ObsRecorder, ensure_obs
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

    def stop(self) -> None:
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
                    meta["offset"], self.cfg, self.log)
            meta["processed"] = True
        except Exception as e:  # watcher ölmesin
            meta["tries"] = meta.get("tries", 0) + 1
            self.log(f"İşleme hatası ({meta['tries']}/{MAX_TRIES}): {e}")
        sidecar.write_text(json.dumps(meta))

    def process_pending(self) -> None:
        """Önceden işlenememiş kayıtları (watcher erken kapandıysa vb.) tamamlar."""
        for sidecar in sorted(self.rec_dir.glob("*.json")):
            meta = json.loads(sidecar.read_text())
            if meta.get("processed") or meta.get("tries", 0) >= MAX_TRIES:
                continue
            if not Path(meta.get("video") or sidecar.with_suffix(".mp4")).exists():
                continue
            self._set("processing", f"Bekleyen kayıt işleniyor: {sidecar.stem}")
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
            sidecar.unlink()
            self.log(f"Eski kayıt silindi: {video.name}")

    def run(self) -> None:
        try:
            self._run()
            self._set("stopped", "Durduruldu.")
        except Exception as e:
            self.error = str(e)
            self._set("error", f"Hata: {e}")

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
        self._set("waiting", "Maç bekleniyor...")
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
                    self.log("OBS hazır.")
            if phase != "InProgress":
                self._stop.wait(2)
                continue

            game_id = lcu.game_id()
            rec.start()
            self._set("recording", f"Maç başladı (gameId={game_id}), kayıt açık.")

            # video_zamanı - oyun_zamanı farkını birkaç örnekle ölç
            offsets: list[float] = []
            while lcu.phase() == "InProgress" and not self._stop.is_set():
                gt = live_game_time()
                if gt is not None and gt > 0 and len(offsets) < 10:
                    offsets.append(rec.elapsed() - gt)
                self._stop.wait(3)

            video = rec.stop()
            if not offsets:
                self._set("waiting", "Oyun saati okunamadı (remake/erken çıkış?), atlanıyor.")
                continue
            offset = statistics.median(offsets)
            sidecar = Path(video).with_suffix(".json")
            sidecar.write_text(json.dumps({"match_id": self.api.match_id_from_game_id(game_id),
                                           "offset": offset, "video": video}))
            if self._stop.is_set():
                # maç ortasında durdurulduysa sonraki açılışta işlenir
                self.log("Kayıt kaydedildi; klipler bir sonraki başlatmada çıkarılacak.")
                break

            self._set("processing", "Maç bitti, klipler hazırlanıyor...")
            self.handle(sidecar, timeout=300)
            self.process_pending()
            self.cleanup()
            self._set("waiting", "Maç bekleniyor...")


def run(cfg: dict) -> None:
    print("Ctrl+C ile çık")
    Watcher(cfg).run()
