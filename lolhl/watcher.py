"""Arka planda çalışır: maç başlayınca kaydı açar, bitince klipleri çıkarır.
Oyun içinde hiçbir arayüz göstermez."""
from __future__ import annotations

import json
import statistics
import time
from pathlib import Path

from .lcu import Lcu, live_game_time
from .pipeline import process
from .recorder import ObsRecorder, ensure_obs
from .riot_api import RiotApi

MAX_TRIES = 3  # pratik modu gibi API'ye hiç düşmeyen maçlar sonsuza dek denenmesin


def handle(sidecar: Path, api: RiotApi, puuid: str, cfg: dict, timeout: float) -> None:
    """Bir kaydı işler ve sonucu yan .json dosyasına yazar."""
    meta = json.loads(sidecar.read_text())
    video = meta.get("video") or str(sidecar.with_suffix(".mp4"))
    try:
        process(api.load(meta["match_id"], puuid, timeout), video, meta["offset"], cfg)
        meta["processed"] = True
    except Exception as e:  # watcher ölmesin
        meta["tries"] = meta.get("tries", 0) + 1
        print(f"İşleme hatası ({meta['tries']}/{MAX_TRIES}): {e}")
    sidecar.write_text(json.dumps(meta))


def process_pending(rec_dir: Path, api: RiotApi, puuid: str, cfg: dict) -> None:
    """Önceden işlenememiş kayıtları (watcher erken kapandıysa vb.) tamamlar."""
    for sidecar in sorted(rec_dir.glob("*.json")):
        meta = json.loads(sidecar.read_text())
        if meta.get("processed") or meta.get("tries", 0) >= MAX_TRIES:
            continue
        if not Path(meta.get("video") or sidecar.with_suffix(".mp4")).exists():
            continue
        print(f"Bekleyen kayıt işleniyor: {sidecar.stem}")
        handle(sidecar, api, puuid, cfg, timeout=0)


def run(cfg: dict) -> None:
    lcu = Lcu(cfg["lol"]["install_dir"])
    api = RiotApi(cfg["riot"]["api_key"], cfg["riot"]["platform"])
    puuid = api.puuid(cfg["riot"]["game_name"], cfg["riot"]["tag_line"])
    rec_dir = Path(cfg["obs"]["recordings_dir"])
    rec: ObsRecorder | None = None

    process_pending(rec_dir, api, puuid, cfg)
    print("Bekleniyor... (Ctrl+C ile çık)")
    while True:
        phase = lcu.phase()
        if phase in ("ChampSelect", "GameStart", "InProgress"):
            # şampiyon seçiminde açılırsa maç başlayana kadar hazır olur
            try:
                rec and rec.elapsed()
            except Exception:
                rec = None  # OBS kapatılmış
            if rec is None:
                rec = ensure_obs(cfg["obs"])
                print("OBS hazır.")
        if phase != "InProgress":
            time.sleep(2)
            continue

        game_id = lcu.game_id()
        rec.start()
        print(f"Maç başladı (gameId={game_id}), kayıt açık.")

        # video_zamanı - oyun_zamanı farkını birkaç örnekle ölç
        offsets: list[float] = []
        while lcu.phase() == "InProgress":
            gt = live_game_time()
            if gt is not None and gt > 0 and len(offsets) < 10:
                offsets.append(rec.elapsed() - gt)
            time.sleep(3)

        video = rec.stop()
        if not offsets:
            print("Oyun saati okunamadı (remake/erken çıkış?), atlanıyor.")
            continue
        offset = statistics.median(offsets)
        print(f"Maç bitti. Video: {video}, offset={offset:.2f}s")
        print("Klipler hazırlanıyor, pencereyi kapatma...")

        sidecar = Path(video).with_suffix(".json")
        sidecar.write_text(json.dumps({"match_id": api.match_id_from_game_id(game_id),
                                       "offset": offset, "video": video}))
        handle(sidecar, api, puuid, cfg, timeout=300)
        process_pending(rec_dir, api, puuid, cfg)
        print("Bekleniyor...")
