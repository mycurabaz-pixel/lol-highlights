"""Çift tıklayınca terminal açmadan arayüzü başlatır (pythonw)."""
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    from lolhl.ui.app import main
    main()
except Exception:
    # pythonw'da konsol yok; hatayı dosyaya yaz ve kullanıcıya göster
    (ROOT / "ui-error.log").write_text(traceback.format_exc(), encoding="utf-8")
    import ctypes
    ctypes.windll.user32.MessageBoxW(
        0, f"The app could not start. Details:\n{ROOT / 'ui-error.log'}", "LoL Highlights", 0x10)
