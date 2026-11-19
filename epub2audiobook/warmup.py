"""Download the TTS models so a fresh installation works offline right away.

``python -m epub2audiobook.warmup`` fetches the Kokoro voices and the Supertonic 3 weights into
the directories pointed at by HF_HOME / SUPERTONIC_CACHE_DIR (the launcher keeps both inside the
install folder) and then reports how big they are. The installer calls this once so the first
conversion never has to wait for a download.
"""
from __future__ import annotations

import os
import sys
import threading


def _size_mb(path: str) -> float:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total / (1024 * 1024)


def _heartbeat(paths: list[str], stop: threading.Event) -> None:
    """Print the cache growth periodically: a big download looks frozen otherwise."""
    while not stop.wait(15):
        mb = sum(_size_mb(p) for p in paths if p and os.path.isdir(p))
        print(f"    … {mb:.0f} MB downloaded so far", flush=True)


def main() -> int:
    from .tts import make_engine

    caches = [os.environ.get("HF_HOME", ""), os.environ.get("SUPERTONIC_CACHE_DIR", "")]
    stop = threading.Event()
    threading.Thread(target=_heartbeat, args=(caches, stop), daemon=True).start()
    print(f"Kokoro cache: {os.environ.get('HF_HOME', '(default)')}", flush=True)
    kokoro = make_engine("kokoro", "cpu")
    print("  downloading Kokoro-82M and its voices…", flush=True)
    kokoro.pipeline("e")
    print("  Kokoro ready", flush=True)

    supertonic = make_engine("supertonic", "cpu")
    print("  downloading Supertonic 3…", flush=True)
    supertonic._engine()
    supertonic._style("F1")
    print("  Supertonic ready", flush=True)

    stop.set()
    hf = os.environ.get("HF_HOME", "")
    st = os.environ.get("SUPERTONIC_CACHE_DIR", "")
    for label, path in (("Kokoro", hf), ("Supertonic", st)):
        if path and os.path.isdir(path):
            print(f"  {label}: {_size_mb(path):.0f} MB in {path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
