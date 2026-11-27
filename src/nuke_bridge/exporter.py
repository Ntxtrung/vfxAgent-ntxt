"""Lightweight Nuke Read metadata extractor and ComfyUI prep-cache exporter."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from src.contracts.models import ColorManagementMode, MediaKind, ShotMetadata
from src.nuke_bridge.colorspace import (
    detect_color_management,
    detect_media_kind,
    normalize_colorspace_name,
    resolve_ai_prep_colorspace,
)


def _get_nuke(nuke_module: Any | None = None) -> Any:
    """Lazily import Foundry Nuke only inside Nuke Bridge runtime."""
    if nuke_module is not None:
        return nuke_module
    import nuke

    return nuke


def _sanitize_shot_id(node_name: str) -> str:
    """Create a filesystem-safe shot identifier from a Nuke node name."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", node_name).strip("_")
    return cleaned or "shot"


def resolve_cache_dir(cache_root: str | Path | None = None) -> Path:
    """Resolve cache root from argument or CACHE_DIR environment variable."""
    raw = str(cache_root) if cache_root is not None else os.environ.get("CACHE_DIR", "./cache")
    return Path(raw).expanduser().resolve()


def _uses_aces_exr_default(mode: ColorManagementMode, kind: MediaKind, colorspace: str) -> bool:
    name = colorspace.strip().lower()
    return (
        mode == ColorManagementMode.ACES_1_2
        and kind == MediaKind.EXR
        and (name == "default" or name.startswith("default ("))
    )


def extract_shot_metadata(
    read_node: Any,
    cache_root: str | Path | None = None,
    nuke_module: Any | None = None,
    first_frame: int | None = None,
    last_frame: int | None = None,
    shot_id: str | None = None,
) -> ShotMetadata:
    """Read footage, format, and colorspace metadata from a Nuke Read node."""
    nuke = _get_nuke(nuke_module)
    root = nuke.root()

    node_name = read_node.name()
    file_pattern = str(read_node["file"].value())
    media_kind = detect_media_kind(file_pattern)

    f_start = int(first_frame if first_frame is not None else read_node["first"].value())
    f_end = int(last_frame if last_frame is not None else read_node["last"].value())
    if f_end < f_start:
        raise ValueError(f"Invalid frame range: {f_start}..{f_end}")

    fmt = read_node.format()
    width = int(fmt.width())
    height = int(fmt.height())
    fps = float(root["fps"].value())

    cm_knob = str(root["colorManagement"].value())
    ocio_knob = str(root["OCIO_config"].value()) if "OCIO_config" in root.knobs() else ""
    working_knob = str(root["workingSpaceLUT"].value()) if "workingSpaceLUT" in root.knobs() else ""

    cm_mode = detect_color_management(cm_knob, ocio_knob)
    read_colorspace = str(read_node["colorspace"].value())
    working_cs = normalize_colorspace_name(working_knob, "linear")
    source_cs = normalize_colorspace_name(
        read_colorspace,
        fallback=working_cs,
    )
    if _uses_aces_exr_default(cm_mode, media_kind, read_colorspace):
        float_lut = str(root["floatLut"].value()) if "floatLut" in root.knobs() else ""
        source_cs = normalize_colorspace_name(float_lut, source_cs)
        if source_cs in {"default", "linear", "scene_linear"}:
            source_cs = "ACES - ACEScg"
    ai_prep_cs = resolve_ai_prep_colorspace(cm_mode, source_cs)

    shot_id = _sanitize_shot_id(shot_id or node_name)
    cache_dir = resolve_cache_dir(cache_root)
    cache_seq = (cache_dir / shot_id / "input" / f"{shot_id}_input.%04d.png").as_posix()

    return ShotMetadata(
        shot_id=shot_id,
        node_name=node_name,
        file_pattern=file_pattern,
        media_kind=media_kind,
        first_frame=f_start,
        last_frame=f_end,
        width=width,
        height=height,
        fps=fps,
        color_management=cm_mode,
        source_colorspace=source_cs,
        working_colorspace=working_cs,
        ai_prep_colorspace=ai_prep_cs,
        cache_sequence_pattern=cache_seq,
        ocio_config_path=ocio_knob,
    )


def export_prep_sequence(
    read_node: Any,
    cache_root: str | Path | None = None,
    nuke_module: Any | None = None,
    first_frame: int | None = None,
    last_frame: int | None = None,
    shot_id: str | None = None,
) -> ShotMetadata:
    """Render a 16-bit sRGB PNG prep sequence and write shot_metadata.json."""
    nuke = _get_nuke(nuke_module)
    metadata = extract_shot_metadata(
        read_node=read_node,
        cache_root=cache_root,
        nuke_module=nuke,
        first_frame=first_frame,
        last_frame=last_frame,
        shot_id=shot_id,
    )

    out_pattern = Path(metadata.cache_sequence_pattern)
    out_pattern.parent.mkdir(parents=True, exist_ok=True)

    cs_node = create_color_transform(
        nuke, metadata.color_management, metadata.working_colorspace, metadata.ai_prep_colorspace
    )
    colorspace = read_node["colorspace"]
    original_colorspace = str(colorspace.value())
    use_explicit_source = _uses_aces_exr_default(
        metadata.color_management, metadata.media_kind, original_colorspace
    ) and not ("raw" in read_node.knobs() and read_node["raw"].value())
    try:
        # Nuke 15 can decode an untagged ACES EXR through the display transform with Read's default.
        if use_explicit_source:
            colorspace.setValue(metadata.source_colorspace)
        cs_node.setInput(0, read_node)
        render_cache_png(
            nuke,
            cs_node,
            metadata.cache_sequence_pattern,
            metadata.first_frame,
            metadata.last_frame,
        )
    finally:
        try:
            if use_explicit_source:
                colorspace.setValue("default")
        finally:
            nuke.delete(cs_node)

    meta_path = out_pattern.parent.parent / "shot_metadata.json"
    metadata.save(meta_path)
    return metadata


def create_color_transform(nuke: Any, mode: ColorManagementMode, source: str, target: str) -> Any:
    """Create the project color transform between two explicit color spaces."""
    if mode == ColorManagementMode.NUKE_DEFAULT:
        return nuke.nodes.Colorspace(colorspace_in=source, colorspace_out=target)
    return nuke.nodes.OCIOColorSpace(in_colorspace=source, out_colorspace=target)


def render_cache_png(nuke: Any, node: Any, path: str, first: int, last: int) -> None:
    """Render already-transformed pixels to disk without a second output transform."""
    write_node = nuke.nodes.Write(
        file=path, file_type="png", datatype="16 bit", create_directories=True
    )
    try:
        write_node["colorspace"].setValue("raw")
        write_node.setInput(0, node)
        nuke.execute(write_node, first, last)
    finally:
        nuke.delete(write_node)
