"""
Streaming Faster-Whisper ASR Adapter.
Supports multiple model sizes, GPU (int8_float16) with automatic CPU fallback,
word-level timestamps, and Philippine code-switching.
Raises typed ASREngineError on failure rather than returning silent empty hypotheses.
Tracks actual serving device and compute type for immutable provenance.
"""
import os
import sys
import time
from typing import List, Optional, Dict, Any
import numpy as np

from app.config import settings

class ASREngineError(Exception):
    pass

class ASROOMError(ASREngineError):
    pass

class HypothesisWord:
    def __init__(self, start_ms: int, end_ms: int, text: str, confidence: float = 0.9, language: Optional[str] = None):
        self.start_ms = start_ms
        self.end_ms = end_ms
        self.text = text
        self.confidence = confidence
        self.language = language

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "confidence": self.confidence,
            "language": self.language
        }

ASRWordHypothesis = HypothesisWord

class FasterWhisperASREngine:
    def __init__(self, model_size_or_path: Optional[str] = None, device: Optional[str] = None):
        from faster_whisper import WhisperModel

        self.model_name = model_size_or_path or settings.DEFAULT_ASR_MODEL
        self.requested_device = device or settings.DEFAULT_DEVICE
        self.actual_device = self.requested_device
        self.actual_compute_type = settings.DEFAULT_COMPUTE_TYPE
        self.model = None

        try:
            print(f"[ASR Engine] Loading faster-whisper model '{self.model_name}' on {self.requested_device} ({self.actual_compute_type})...")
            self.model = WhisperModel(
                self.model_name,
                device=self.requested_device,
                compute_type=self.actual_compute_type,
                download_root=str(settings.MODELS_DIR)
            )
            self.actual_device = self.requested_device
            print(f"[ASR Engine] Successfully loaded '{self.model_name}' on {self.actual_device}.")
        except Exception as e:
            err_str = str(e).lower()
            if "out of memory" in err_str or "cuda" in err_str or "cublas" in err_str or "cudnn" in err_str or "invalid device" in err_str:
                print(f"[ASR Engine] GPU initialization failed ({e}). Falling back to CPU int8...")
                try:
                    self.actual_device = "cpu"
                    self.actual_compute_type = "int8"
                    self.model = WhisperModel(
                        self.model_name,
                        device="cpu",
                        compute_type="int8",
                        download_root=str(settings.MODELS_DIR)
                    )
                    print(f"[ASR Engine] Loaded '{self.model_name}' on CPU fallback.")
                except Exception as cpu_err:
                    raise ASREngineError(f"Failed to initialize Whisper model on CPU fallback: {cpu_err}") from cpu_err
            else:
                raise ASREngineError(f"Failed to initialize Whisper model '{self.model_name}': {e}") from e

    def transcribe_window(
        self,
        audio_chunk: np.ndarray,
        window_start_ms: int,
        language_mode: str = "auto-mixed",
        initial_prompt: Optional[str] = None
    ) -> List[HypothesisWord]:
        """
        Transcribe a 16kHz mono audio float32 slice.
        Transforms window-relative timestamps into absolute session milliseconds.
        Raises ASREngineError if inference fails.
        """
        if self.model is None:
            raise ASREngineError("ASR model is not initialized")

        if len(audio_chunk) < 1600:  # < 100ms
            return []

        # Configure language parameters
        if language_mode in ("auto", "auto-mixed"):
            lang_param = None
        elif language_mode in ("en", "tl", "ceb"):
            lang_param = language_mode
        else:
            lang_param = None

        if initial_prompt is None and language_mode == "auto-mixed":
            initial_prompt = "English, Tagalog, Cebuano conversation."

        try:
            segments, info = self.model.transcribe(
                audio_chunk,
                language=lang_param,
                initial_prompt=initial_prompt,
                word_timestamps=True,
                vad_filter=True,
                vad_parameters=dict(
                    min_silence_duration_ms=settings.VAD_MIN_SILENCE_DURATION_MS,
                    speech_pad_ms=settings.VAD_SPEECH_PAD_MS
                ),
                beam_size=settings.BEAM_SIZE,
                temperature=0.0
            )

            hypothesis_words: List[HypothesisWord] = []
            detected_language = getattr(info, 'language', None)

            for segment in segments:
                if not segment.words:
                    continue
                for w in segment.words:
                    word_text = w.word.strip()
                    if not word_text:
                        continue
                    w_start_ms = window_start_ms + int(w.start * 1000)
                    w_end_ms = window_start_ms + int(w.end * 1000)
                    confidence = float(w.probability) if hasattr(w, 'probability') else 0.9

                    hypothesis_words.append(HypothesisWord(
                        start_ms=w_start_ms,
                        end_ms=w_end_ms,
                        text=word_text,
                        confidence=confidence,
                        language=detected_language
                    ))

            return hypothesis_words

        except Exception as e:
            err_str = str(e).lower()
            if "out of memory" in err_str or "cuda" in err_str:
                print(f"[ASR Engine] CUDA OOM during inference: {e}. Attempting CPU fallback...")
                try:
                    from faster_whisper import WhisperModel
                    self.actual_device = "cpu"
                    self.actual_compute_type = "int8"
                    self.model = WhisperModel(self.model_name, device="cpu", compute_type="int8", download_root=str(settings.MODELS_DIR))
                    return self.transcribe_window(audio_chunk, window_start_ms, language_mode, initial_prompt)
                except Exception as inner_e:
                    raise ASROOMError(f"CUDA OOM and CPU fallback failed: {inner_e}") from inner_e
            else:
                raise ASREngineError(f"Inference error in FasterWhisper: {e}") from e
