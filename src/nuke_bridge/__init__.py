"""Foundry Nuke bridge package (Read metadata, colorspace cache, node builder)."""

from src.nuke_bridge.colorspace import (
    detect_color_management,
    detect_media_kind,
    normalize_colorspace_name,
    resolve_ai_prep_colorspace,
)
from src.nuke_bridge.exporter import export_prep_sequence, extract_shot_metadata

__all__ = [
    "detect_color_management",
    "detect_media_kind",
    "export_prep_sequence",
    "extract_shot_metadata",
    "normalize_colorspace_name",
    "resolve_ai_prep_colorspace",
]
