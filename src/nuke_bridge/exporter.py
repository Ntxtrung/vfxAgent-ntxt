"""Lightweight Nuke Read metadata extractor and ComfyUI prep-cache exporter."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from src.contracts.models import ColorManagementMode, ShotMetadata
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


def extract_shot_metadata(
    read_node: Any,
    cache_root: str | Path | None = None,
    nuke_module: Any | None = None,
    first_frame: int | None = None,
    last_frame: int | None = None,
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
    source_cs = normalize_colorspace_name(
        str(read_node["colorspace"].value()),
        fallback=normalize_colorspace_name(working_knob, "linear"),
    )
    working_cs = normalize_colorspace_name(working_knob, "linear")
    ai_prep_cs = resolve_ai_prep_colorspace(cm_mode, source_cs)

    shot_id = _sanitize_shot_id(node_name)
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
) -> ShotMetadata:
    """Render a 16-bit sRGB PNG prep sequence and write shot_metadata.json."""
    nuke = _get_nuke(nuke_module)
    metadata = extract_shot_metadata(
        read_node=read_node,
        cache_root=cache_root,
        nuke_module=nuke,
        first_frame=first_frame,
        last_frame=last_frame,
    )

    out_pattern = Path(metadata.cache_sequence_pattern)
    out_pattern.parent.mkdir(parents=True, exist_ok=True)

    temp_nodes: list[Any] = []
    try:
        if metadata.color_management in (
            ColorManagementMode.ACES_1_2,
            ColorManagementMode.OCIO_CUSTOM,
        ):
            cs_node = nuke.nodes.OCIOColorSpace(
                in_colorspace=metadata.working_colorspace,
                out_colorspace=metadata.ai_prep_colorspace,
            )
        else:
            cs_node = nuke.nodes.Colorspace(
                colorspace_in=metadata.working_colorspace,
                colorspace_out=metadata.ai_prep_colorspace,
            )
        cs_node.setInput(0, read_node)
        temp_nodes.append(cs_node)

        write_node = nuke.nodes.Write(
            file=metadata.cache_sequence_pattern,
            file_type="png",
            datatype="16 bit",
            create_directories=True,
        )
        write_node["colorspace"].setValue("raw")
        write_node.setInput(0, cs_node)
        temp_nodes.append(write_node)

        nuke.execute(write_node, metadata.first_frame, metadata.last_frame)
    finally:
        for node in reversed(temp_nodes):
            nuke.delete(node)

    meta_path = out_pattern.parent.parent / "shot_metadata.json"
    metadata.save(meta_path)
    return metadata
