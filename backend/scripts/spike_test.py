"""
Technical Spike: Device, Faster-Whisper, and Ingestion pipeline test
"""
import sys
import time
import shutil
import numpy as np
import soundfile as sf
import ctranslate2
from faster_whisper import WhisperModel

def test_ctranslate_device():
    print("=== Checking CTranslate2 GPU / CUDA Support ===")
    cuda_types = ctranslate2.get_supported_compute_types("cuda") if ctranslate2.get_cuda_device_count() > 0 else []
    cpu_types = ctranslate2.get_supported_compute_types("cpu")
    print(f"CUDA devices: {ctranslate2.get_cuda_device_count()}")
    print(f"CUDA supported compute types: {cuda_types}")
    print(f"CPU supported compute types: {cpu_types}")
    return ctranslate2.get_cuda_device_count() > 0

def test_synthetic_transcription():
    print("\n=== Testing Faster-Whisper Inference on Synthetic 16kHz PCM ===")
    sample_rate = 16000
    duration = 3.0  # 3 seconds
    # Generate 16kHz sine wave as placeholder PCM
    t = np.linspace(0, duration, int(sample_rate * duration), endpoint=False, dtype=np.float32)
    audio = 0.1 * np.sin(2 * np.pi * 440 * t)
    
    device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
    compute_type = "int8_float16" if device == "cuda" else "int8"
    
    print(f"Initializing WhisperModel (tiny, device={device}, compute_type={compute_type})...")
    start = time.time()
    try:
        model = WhisperModel("tiny", device=device, compute_type=compute_type, download_root="data/models")
        print(f"Model loaded in {time.time() - start:.2f}s")
        
        segments, info = model.transcribe(audio, word_timestamps=True, language="en")
        print(f"Detected language: {info.language} with prob: {info.language_probability:.2f}")
        for segment in segments:
            print(f"Segment: [{segment.start:.2f}s -> {segment.end:.2f}s] {segment.text}")
            for word in segment.words or []:
                print(f"   Word: [{word.start:.2f}s -> {word.end:.2f}s] {word.word} (prob: {word.probability:.2f})")
        print("Transcription test completed successfully.")
    except Exception as e:
        print(f"Error during transcription: {e}")
        if device == "cuda":
            print("Falling back to CPU int8...")
            model = WhisperModel("tiny", device="cpu", compute_type="int8", download_root="data/models")
            segments, info = model.transcribe(audio, word_timestamps=True, language="en")
            print(f"CPU Fallback succeeded. Language: {info.language}")

def test_ffmpeg_ytdlp():
    print("\n=== Testing FFmpeg & yt-dlp binary presence ===")
    ffmpeg_path = shutil.which("ffmpeg")
    print(f"FFmpeg path: {ffmpeg_path}")
    import yt_dlp
    print(f"yt-dlp version: {yt_dlp.version.__version__}")

if __name__ == "__main__":
    test_ctranslate_device()
    test_ffmpeg_ytdlp()
    test_synthetic_transcription()
