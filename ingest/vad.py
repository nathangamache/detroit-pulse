import threading
import numpy as np
import torch
import soundfile as sf
import structlog
from pathlib import Path

# Global lock — VAD model inference is not thread-safe
_vad_lock = threading.Lock()

log = structlog.get_logger()

_model      = None
_utils      = None


def _load_model():
    global _model, _utils
    if _model is None:
        log.info("Loading Silero VAD model (CPU)")
        _model, _utils = torch.hub.load(
            repo_or_dir = "snakers4/silero-vad",
            model       = "silero_vad",
            force_reload = False,
            trust_repo  = True,
        )
        # Always run on CPU — avoids CUDA threading segfaults
        _model = _model.cpu()
        log.info("Silero VAD model loaded (CPU)")
    return _model, _utils


def has_speech(
    audio_path:             str,
    threshold:              float = 0.5,
    min_speech_duration_ms: int   = 500,
    sample_rate:            int   = 16000,
) -> bool:
    """
    Returns True if the audio file contains speech above the threshold.
    Fast check used before sending chunks to Whisper.
    """
    model, utils = _load_model()
    get_speech_timestamps, _, read_audio, *_ = utils

    try:
        wav = read_audio(audio_path, sampling_rate=sample_rate)
        with _vad_lock:
            speech_timestamps = get_speech_timestamps(
                wav,
                model,
                threshold              = threshold,
                sampling_rate          = sample_rate,
                min_speech_duration_ms = min_speech_duration_ms,
            )
        return len(speech_timestamps) > 0
    except Exception as e:
        log.warning("VAD check failed", path=audio_path, error=str(e))
        return True


def get_speech_ratio(
    audio_path:  str,
    sample_rate: int = 16000,
) -> float:
    """
    Returns the ratio of speech to total audio duration (0.0 to 1.0).
    """
    model, utils = _load_model()
    get_speech_timestamps, _, read_audio, *_ = utils

    try:
        wav = read_audio(audio_path, sampling_rate=sample_rate)
        with _vad_lock:
            speech_timestamps = get_speech_timestamps(
                wav, model, sampling_rate=sample_rate
            )
        if len(wav) == 0:
            return 0.0
        total_speech = sum(t["end"] - t["start"] for t in speech_timestamps)
        return total_speech / len(wav)
    except Exception as e:
        log.warning("VAD ratio check failed", path=audio_path, error=str(e))
        return 0.0


def get_silence_boundaries(
    audio:       np.ndarray,
    sample_rate: int   = 16000,
    threshold:   float = 0.4,
) -> list[dict]:
    """
    Return speech timestamp dicts for a numpy audio array.
    Used by feed_worker for VAD-aware cut point detection.
    """
    model, utils = _load_model()
    get_speech_timestamps, *_ = utils

    try:
        wav = torch.FloatTensor(audio)
        with _vad_lock:
            return get_speech_timestamps(
                wav,
                model,
                sampling_rate           = sample_rate,
                threshold               = threshold,
                min_silence_duration_ms = 300,
            )
    except Exception as e:
        log.warning("VAD silence boundary detection failed", error=str(e))
        return []