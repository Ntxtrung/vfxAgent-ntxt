"""Run the local SAM 2 and ProPainter engines with file-based inputs and outputs."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from PIL import Image, ImageChops, ImageFilter

from src.core.config import CoreConfig

PROPAINTER_WEIGHTS = ("ProPainter.pth", "raft-things.pth", "recurrent_flow_completion.pth")


def validate_runtime(config: CoreConfig, *, use_sam2: bool, use_propainter: bool) -> None:
    if not config.model_python.is_file():
        raise ValueError("Set F2_MODEL_PYTHON_EXE to a local Python executable")
    if use_sam2 and (config.sam2_checkpoint is None or not config.sam2_checkpoint.is_file()):
        raise ValueError("Set SAM2_CHECKPOINT to a local checkpoint matching SAM2_MODEL_CONFIG")
    if use_propainter:
        root = config.propainter_dir
        if root is None or not (root / "inference_propainter.py").is_file():
            raise ValueError("Set PROPAINTER_DIR to the local upstream ProPainter checkout")
        missing = [name for name in PROPAINTER_WEIGHTS if not (root / "weights" / name).is_file()]
        if missing:
            raise ValueError(f"Missing ProPainter weights: {', '.join(missing)}")
        if config.propainter_max_edge < 64:
            raise ValueError("PROPAINTER_MAX_EDGE must be at least 64 pixels")


def run_engine(command: list[str], log: Path, *, cwd: Path) -> None:
    """Keep native logs with the job and surface the backend error through the API."""
    with log.open("wb") as output:
        try:
            result = subprocess.run(
                command,
                cwd=cwd,
                stdout=output,
                stderr=subprocess.STDOUT,
                timeout=1800,
                env={**os.environ, "PYTHONUTF8": "1"},
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"{log.stem} exceeded 30 minutes. See {log.name}") from exc
    if result.returncode:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
        detail = "\n".join(lines[-8:])
        raise RuntimeError(f"{log.stem} exited with code {result.returncode}: {detail}")


def run_sam2(
    config: CoreConfig, frames: Path, seeds: Path, destination: Path, *, points: Path | None = None
) -> None:
    worker = Path(__file__).with_name("sam2_worker.py").resolve()
    command = [
        str(config.model_python),
        str(worker),
        str(frames),
        str(seeds),
        str(destination),
        config.sam2_model_config,
        str(config.sam2_checkpoint),
    ]
    if points is not None:
        command.extend(["--points", str(points)])
    run_engine(command, destination.parent / "sam2.log", cwd=destination.parent)


def run_propainter(
    config: CoreConfig, frames: Path, masks: Path, size: tuple[int, int], fps: float
) -> tuple[Path, tuple[int, int]]:
    root = config.propainter_dir
    if root is None:
        raise ValueError("ProPainter requires PROPAINTER_DIR")
    scale = min(1.0, config.propainter_max_edge / max(size))
    width, height = (max(8, round(edge * scale / 8) * 8) for edge in size)
    output = frames.parent / "propainter_output"
    worker = Path(__file__).with_name("propainter_worker.py").resolve()
    run_engine(
        [
            str(config.model_python),
            str(worker),
            str(root / "inference_propainter.py"),
            "--video",
            str(frames),
            "--mask",
            str(masks),
            "--output",
            str(output),
            "--width",
            str(width),
            "--height",
            str(height),
            "--save_fps",
            str(max(1, round(fps))),
            "--mask_dilation",
            "2",
            "--save_frames",
            "--fp16",
        ],
        frames.parent / "propainter.log",
        cwd=root,
    )
    return output / frames.name / "frames", (width, height)


def load_luminance_mask(path: Path, size: tuple[int, int]) -> Image.Image:
    with Image.open(path) as image:
        if image.size != size:
            raise ValueError(f"Expected {size[0]}x{size[1]} mask at {path.name}")
        if image.mode.startswith("I"):
            return image.convert("I").point(lambda value: value / 257).convert("L")
        return image.convert("RGB").getchannel("R")


def prepare_mask(mask: Image.Image, expand: int, feather: int) -> Image.Image:
    """Feather outside the matte while keeping fully covered pixels opaque."""
    core = mask.filter(ImageFilter.MaxFilter(2 * expand + 1)) if expand else mask
    return ImageChops.lighter(core, core.filter(ImageFilter.GaussianBlur(feather)))


def restore_patch(plate: Image.Image, patch: Image.Image, matte: Image.Image) -> Image.Image:
    resized = patch.resize(plate.size, Image.Resampling.BICUBIC)
    return Image.composite(resized, plate, matte.point(lambda value: 255 if value else 0))


def model_mask(matte: Image.Image) -> Image.Image:
    """Request a fill wherever the published matte can contribute."""
    return matte.point(lambda value: 255 if value else 0)
