"""Environment configuration and logging setup for Core Worker process."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(env_path: Path) -> None:
    """Load key=value pairs from .env into os.environ if not already set."""
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


@dataclass(slots=True)
class CoreConfig:
    """Runtime settings for the Core Worker FastAPI process."""

    core_host: str = "127.0.0.1"
    core_port: int = 8765
    comfyui_url: str = "http://127.0.0.1:8188"
    cache_dir: Path = Path("./cache")
    logs_dir: Path = Path("./logs")
    ui_host: str = "127.0.0.1"
    ui_port: int = 5173
    inpaint_checkpoint: str = ""

    @classmethod
    def from_env(cls, root_dir: Path | None = None) -> CoreConfig:
        """Load configuration from .env and environment variables."""
        base = root_dir or Path.cwd()
        _load_dotenv(base / ".env")
        return cls(
            core_host=os.environ.get("CORE_HOST", "127.0.0.1"),
            core_port=int(os.environ.get("CORE_PORT", "8765")),
            comfyui_url=os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188").rstrip("/"),
            cache_dir=Path(os.environ.get("CACHE_DIR", "./cache")).expanduser().resolve(),
            logs_dir=Path(os.environ.get("LOGS_DIR", "./logs")).expanduser().resolve(),
            ui_host=os.environ.get("UI_HOST", "127.0.0.1"),
            ui_port=int(os.environ.get("UI_PORT", "5173")),
            inpaint_checkpoint=os.environ.get("COMFYUI_INPAINT_CHECKPOINT", ""),
        )


def configure_core_logger(logs_dir: Path) -> logging.Logger:
    """Configure file + stream logger writing to logs/core.log."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("vfx_agent.core")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    file_handler = logging.FileHandler(logs_dir / "core.log", encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    logger.addHandler(stream_handler)
    return logger
