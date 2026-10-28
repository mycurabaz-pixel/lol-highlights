"""OBS'yi obs-websocket (v5) üzerinden kontrol eder."""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

import obsws_python as obs


def _obs_running() -> bool:
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq obs64.exe", "/NH"],
                         capture_output=True, text=True).stdout
    return "obs64.exe" in out


def _try_connect(cfg: dict) -> "ObsRecorder | None":
    """Bağlanır ve OBS isteklere hazır olana kadar (açılışta 207 döner) bekler."""
    try:
        r = ObsRecorder(cfg["host"], cfg["port"], cfg["password"])
        r.cl.get_record_status()
        return r
    except Exception:
        return None


def ensure_obs(cfg: dict) -> "ObsRecorder":
    """OBS açıksa bağlanır; değilse tepsiye küçültülmüş olarak açıp bağlanır."""
    if (r := _try_connect(cfg)):
        return r
    if not _obs_running():  # açık ama hazır değilse ikinci kopya başlatma
        exe = Path(cfg.get("exe", ""))
        if not exe.is_file():
            raise RuntimeError("OBS kapalı ve config'teki obs.exe yolu bulunamadı.")
        # OBS kendi klasöründen çalıştırılmazsa locale dosyalarını bulamaz
        subprocess.Popen([str(exe), "--minimize-to-tray", "--disable-shutdown-check"],
                         cwd=exe.parent)
    for _ in range(40):
        time.sleep(1)
        if (r := _try_connect(cfg)):
            return r
    raise RuntimeError("OBS'e bağlanılamadı. WebSocket sunucusu açık mı, şifre doğru mu?")


class ObsRecorder:
    def __init__(self, host: str, port: int, password: str):
        self.cl = obs.ReqClient(host=host, port=port, password=password, timeout=5)

    def start(self) -> float:
        """Kaydı başlatır, kaydın başladığı duvar saatini döner."""
        if self.cl.get_record_status().output_active:
            self.cl.stop_record()
            time.sleep(1)
        self.cl.start_record()
        while not self.cl.get_record_status().output_active:
            time.sleep(0.05)
        return time.time()

    def stop(self) -> str:
        """Kaydı durdurur, video dosyasının yolunu döner."""
        return self.cl.stop_record().output_path

    def elapsed(self) -> float:
        """Kaydın şu anki süresi (saniye), OBS'nin kendi sayacından."""
        return self.cl.get_record_status().output_duration / 1000.0
