"""Colorspace and media format helpers for Nuke Read -> ComfyUI prep."""

from __future__ import annotations

import re
from pathlib import Path

from src.contracts.models import ColorManagementMode, MediaKind

_EXT_TO_MEDIA: dict[str, MediaKind] = {
    ".exr": MediaKind.EXR,
    ".dpx": MediaKind.DPX,
    ".mov": MediaKind.MOV,
    ".mp4": MediaKind.MP4,
    ".png": MediaKind.PNG_SEQ,
}


def detect_media_kind(file_pattern: str) -> MediaKind:
    """Infer MediaKind from a Nuke Read file path or frame pattern."""
    path = re.sub(r"\s+-?\d+\s*-\s*-?\d+\s*$", "", file_pattern.strip())
    suffix = Path(path).suffix.lower()
    if suffix not in _EXT_TO_MEDIA:
        supported = ", ".join(sorted(_EXT_TO_MEDIA))
        raise ValueError(f"Unsupported footage extension '{suffix}'. Expected one of: {supported}")
    return _EXT_TO_MEDIA[suffix]


def detect_color_management(
    color_management_knob: str,
    ocio_config_knob: str = "",
) -> ColorManagementMode:
    """Classify Nuke root color management into ACES 1.2, OCIO custom, or Nuke default."""
    cm_lower = color_management_knob.strip().lower()
    ocio_lower = ocio_config_knob.strip().lower()

    if cm_lower == "ocio":
        if "aces" in ocio_lower:
            return ColorManagementMode.ACES_1_2
        return ColorManagementMode.OCIO_CUSTOM
    return ColorManagementMode.NUKE_DEFAULT


def resolve_ai_prep_colorspace(
    mode: ColorManagementMode,
    source_colorspace: str,
) -> str:
    """Return the target sRGB-like colorspace used when exporting frames for ComfyUI."""
    src_clean = source_colorspace.strip()
    if mode == ColorManagementMode.ACES_1_2:
        if "utility" in src_clean.lower() or "aces" in src_clean.lower():
            return "Output - sRGB"
        return "Output - sRGB"
    if mode == ColorManagementMode.OCIO_CUSTOM:
        return "sRGB"
    return "sRGB"


def normalize_colorspace_name(raw_value: str, fallback: str = "default") -> str:
    """Normalize Nuke Read colorspace knob text, stripping 'default (...)' wrapper."""
    cleaned = raw_value.strip()
    if not cleaned:
        return fallback
    if cleaned.lower().startswith("default (") and cleaned.endswith(")"):
        return cleaned[9:-1].strip()
    return cleaned
