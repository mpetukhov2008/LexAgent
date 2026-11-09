"""Bounded CI diagnostics for synthetic browser-test screenshots only."""

import base64
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "reports"

if __name__ == "__main__":
    for name in ("welcome", "home", "light", "processing"):
        target = ROOT / f"ui-{name}.jpg"
        if not target.exists() or target.stat().st_size > 400_000:
            continue
        encoded = base64.b64encode(target.read_bytes()).decode()
        for index in range(0, len(encoded), 4000):
            print(f"UI_FRAME_{name}_{index:06d}:{encoded[index:index + 4000]}")
