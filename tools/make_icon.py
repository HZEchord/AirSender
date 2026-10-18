"""產生 assets/airsender.ico（供 exe 與安裝程式使用）。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from airsender.tray import make_icon_image  # noqa: E402

out = ROOT / "assets" / "airsender.ico"
out.parent.mkdir(exist_ok=True)
base = make_icon_image(True, 256)
base.save(out, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print(f"wrote {out}")
