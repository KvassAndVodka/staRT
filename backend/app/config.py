"""
Configuration & Environment Settings for staRT (Local Transcript Service)
"""
import os
import sys
import glob
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

# Automatically inject nvidia pip-installed CUDA libraries into LD_LIBRARY_PATH/ctypes if present
def _setup_cuda_libs():
    site_packages = Path(__file__).resolve().parent.parent / ".venv" / "lib"
    if site_packages.exists():
        py_dirs = list(site_packages.glob("python3.*"))
        if py_dirs:
            nvidia_dir = py_dirs[0] / "site-packages" / "nvidia"
            if nvidia_dir.exists():
                lib_paths = []
                for sub in ["cublas", "cudnn", "cuda_nvrtc"]:
                    p = nvidia_dir / sub / "lib"
                    if p.exists():
                        lib_paths.append(str(p))
                if lib_paths:
                    cur = os.environ.get("LD_LIBRARY_PATH", "")
                    os.environ["LD_LIBRARY_PATH"] = ":".join(lib_paths) + (f":{cur}" if cur else "")
                    # Preload with ctypes
                    import ctypes
                    for p in lib_paths:
                        for so_file in glob.glob(f"{p}/*.so*"):
                            try:
                                ctypes.CDLL(so_file)
                            except Exception:
                                pass

_setup_cuda_libs()

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="START_")

    APP_NAME: str = "staRT - Local Transcript Service"
    APP_VERSION: str = "0.1.0"
    DEBUG: bool = False
    
    # Root directory
    BASE_DIR: Path = Path(__file__).resolve().parent.parent.parent
    DATA_DIR: Path = BASE_DIR / "data"
    SESSIONS_DIR: Path = DATA_DIR / "sessions"
    MODELS_DIR: Path = DATA_DIR / "models"
    EXPORTS_DIR: Path = DATA_DIR / "exports"
    
    # Database
    DATABASE_URL: str = f"sqlite+aiosqlite:///{DATA_DIR}/app.db"
    
    # Default Hardware Profile & Model Selection
    DEFAULT_ASR_MODEL: str = "small"  # 'small' for 4GB RTX 3050 Ti, 'turbo' for 6GB RTX 3050
    DEFAULT_DEVICE: str = "cuda"
    DEFAULT_COMPUTE_TYPE: str = "int8_float16"  # 'int8_float16' for CUDA, 'int8' for CPU
    FALLBACK_DEVICE: str = "cpu"
    
    # Continuity & Streaming Presets
    WINDOW_DURATION_SEC: float = 20.0
    OVERLAP_DURATION_SEC: float = 5.0
    INFERENCE_INTERVAL_SEC: float = 1.5
    STABILITY_MARGIN_SEC: float = 3.5
    TURN_SILENCE_THRESHOLD_SEC: float = 2.0
    PARAGRAPH_PAUSE_THRESHOLD_SEC: float = 1.2
    
    # VAD and ASR parameters
    VAD_MIN_SILENCE_DURATION_MS: int = 500
    VAD_SPEECH_PAD_MS: int = 400
    BEAM_SIZE: int = 5
    
    # Audio Ingestion
    MAX_FRAGMENT_DURATION_SEC: float = 2.0
    INFERENCE_SAMPLE_RATE: int = 16000
    SOURCE_STALL_THRESHOLD_SEC: float = 3.0
    
    # Server host & port
    HOST: str = "127.0.0.1"
    PORT: int = 8000
    CORS_ORIGINS: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]

settings = Settings()

# Ensure directories exist
settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
settings.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
settings.MODELS_DIR.mkdir(parents=True, exist_ok=True)
settings.EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
