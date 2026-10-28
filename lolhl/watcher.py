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


def run(cfg: dict) -> None:
    lcu = Lcu(cfg["lol"]["install_dir"])
    api = RiotApi(cfg["riot"]["api_key"], cfg["riot"]["platform"])
    rec: ObsRecorder | None = None
    puuid = None

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

        if puuid is None and (me := lcu.me()):
            puuid = api.puuid(*me)
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

        match_id = api.match_id_from_game_id(game_id)
        sidecar = Path(video).with_suffix(".json")
        sidecar.write_text(json.dumps({"match_id": match_id, "offset": offset}))

        try:
            process(api.load(match_id, puuid), video, offset, cfg)
        except Exception as e:  # watcher ölmesin; sonra `process` ile tekrar denenebilir
            print(f"İşleme hatası: {e}\n  Tekrar dene: python -m lolhl process \"{video}\"")
