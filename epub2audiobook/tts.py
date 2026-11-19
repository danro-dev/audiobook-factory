"""Kokoro-82M wrapper: lazy model loading, per-language pipelines, voice caching."""
from __future__ import annotations

import logging
import threading
import warnings
from typing import Optional

import numpy as np

from .voices import LANGUAGES, supertonic_lang

SAMPLE_RATE = 24000
REPO_ID = "hexgrad/Kokoro-82M"
SUPERTONIC_CACHE_HINT = "~/.cache/supertonic3"

log = logging.getLogger(__name__)


def resolve_device(device: Optional[str]) -> str:
    if device and device.lower() not in {"auto", ""}:
        return device.lower()
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"


def device_description() -> str:
    try:
        import torch
        if torch.cuda.is_available():
            return f"CUDA ({torch.cuda.get_device_name(0)})"
    except Exception:
        pass
    return "CPU"


def _resample(a: np.ndarray, src_rate: int, dst_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Resample mono float32 audio to the pipeline's 24 kHz (torchaudio if present, else linear)."""
    if src_rate == dst_rate or a.size == 0:
        return a
    try:
        import torch
        import torchaudio.functional as AF  # ships with the torchaudio installed by setup.ps1
        return AF.resample(torch.from_numpy(a), int(src_rate), int(dst_rate)).numpy().astype(np.float32)
    except Exception:
        n = max(1, int(round(a.size * dst_rate / src_rate)))
        return np.interp(np.linspace(0, a.size - 1, n, dtype=np.float64),
                         np.arange(a.size, dtype=np.float64), a).astype(np.float32)


class KokoroEngine:
    """Thread-safe (serialised) text-to-speech using Kokoro-82M.

    The model (~330 MB) and the voices are downloaded from Hugging Face on first use and cached
    in the standard HF cache directory. espeak-ng (needed for non-English languages and for
    out-of-dictionary English words) is provided by the `espeakng-loader` wheel.
    """

    label = "Kokoro"

    def __init__(self, device: Optional[str] = "auto", repo_id: str = REPO_ID):
        self.device = resolve_device(device)
        self.repo_id = repo_id
        self._model = None
        self._pipes: dict[str, object] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def pipeline(self, lang: str):
        if lang not in LANGUAGES:
            raise ValueError(f"Unsupported Kokoro language code: {lang!r}")
        with self._lock:
            if lang in self._pipes:
                return self._pipes[lang]
            warnings.filterwarnings("ignore", category=FutureWarning)
            warnings.filterwarnings("ignore", category=UserWarning, module="torch")
            # huggingface_hub nags about anonymous downloads; they work fine (set HF_TOKEN to silence/speed up)
            logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
            for name in ("huggingface_hub.utils._http", "huggingface_hub.file_download"):
                logging.getLogger(name).setLevel(logging.ERROR)
            from kokoro import KPipeline  # heavy import, deferred
            if self._model is None:
                pipe = KPipeline(lang_code=lang, repo_id=self.repo_id, device=self.device)
                self._model = pipe.model
            else:
                pipe = KPipeline(lang_code=lang, repo_id=self.repo_id, model=self._model, device=self.device)
            self._pipes[lang] = pipe
            return pipe

    def warm_up(self, lang: str, voice: str) -> None:
        pipe = self.pipeline(lang)
        try:
            pipe.load_voice(voice)
        except Exception as e:  # pragma: no cover
            log.warning("could not preload voice %s: %s", voice, e)

    # ------------------------------------------------------------------
    def synthesize(self, text: str, voice: str, lang: str, speed: float = 1.0) -> np.ndarray:
        """Return float32 mono audio at 24 kHz for `text` (empty array if nothing was produced)."""
        text = text.strip()
        if not text:
            return np.zeros(0, dtype=np.float32)
        pipe = self.pipeline(lang)
        parts: list[np.ndarray] = []
        with self._lock:
            for result in pipe(text, voice=voice, speed=speed, split_pattern=None):
                audio = getattr(result, "audio", None)
                if audio is None:
                    continue
                try:
                    arr = audio.detach().cpu().numpy()
                except AttributeError:
                    arr = np.asarray(audio)
                arr = arr.astype(np.float32).reshape(-1)
                if arr.size:
                    parts.append(arr)
        if not parts:
            return np.zeros(0, dtype=np.float32)
        if len(parts) == 1:
            return parts[0]
        gap = np.zeros(int(0.08 * SAMPLE_RATE), dtype=np.float32)
        joined: list[np.ndarray] = []
        for i, p in enumerate(parts):
            if i:
                joined.append(gap)
            joined.append(p)
        return np.concatenate(joined)

    def release(self) -> None:
        with self._lock:
            self._pipes.clear()
            self._model = None
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass


class SupertonicEngine:
    """Thread-safe (serialised) text-to-speech using Supertonic 3 (ONNX Runtime, CPU).

    The model (~400 MB) plus its ten built-in voice styles (F1-F5, M1-M5) are downloaded from
    Hugging Face on first use and cached by the package (see SUPERTONIC_CACHE_HINT). Synthesis
    runs on the CPU; audio arrives at the model's 44.1 kHz and is resampled to SAMPLE_RATE so the
    rest of the pipeline (pauses, AAC encoding, M4B muxing) is unchanged.
    """

    label = "Supertonic 3"

    def __init__(self, device: Optional[str] = "auto", model_name: str = "supertonic-3"):
        self.device = "cpu"        # ONNX Runtime CPU inference; the Device choice only affects Kokoro
        self.model_name = model_name
        self._tts = None
        self._styles: dict[str, object] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    def _engine(self):
        with self._lock:
            if self._tts is None:
                try:
                    from supertonic import TTS  # heavy import (onnxruntime), deferred
                except ImportError as e:  # pragma: no cover
                    raise RuntimeError("Supertonic is not installed. Install it with: "
                                       "uv pip install supertonic") from e
                self._tts = TTS(model=self.model_name)
            return self._tts

    def _style(self, voice: str):
        with self._lock:
            if voice not in self._styles:
                self._styles[voice] = self._engine().get_voice_style(voice)
            return self._styles[voice]

    def warm_up(self, lang: str, voice: str) -> None:
        self._style(voice)

    # ------------------------------------------------------------------
    def synthesize(self, text: str, voice: str, lang: str, speed: float = 1.0) -> np.ndarray:
        """Return float32 mono audio at 24 kHz for `text` (empty array if nothing was produced)."""
        text = text.strip()
        if not text:
            return np.zeros(0, dtype=np.float32)
        tts = self._engine()
        style = self._style(voice)
        with self._lock:
            wav, _duration = tts.synthesize(text, voice_style=style, speed=speed,
                                            lang=supertonic_lang(lang), verbose=False)
        a = np.asarray(wav, dtype=np.float32).reshape(-1)
        return _resample(a, int(getattr(tts, "sample_rate", SAMPLE_RATE)))

    def release(self) -> None:
        with self._lock:
            self._tts = None
            self._styles.clear()


def make_engine(name: str = "kokoro", device: Optional[str] = "auto"):
    """Build the engine named by the `engine` option: 'kokoro' or 'supertonic'."""
    if (name or "").lower() == "supertonic":
        return SupertonicEngine(device)
    return KokoroEngine(device)
