"""Prepare Nuke mattes and composite completed Core jobs on the original plate."""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

from src.contracts.models import ShotMetadata
from src.nuke_bridge.exporter import (
    _get_nuke,
    _uses_aces_exr_default,
    create_color_transform,
    export_prep_sequence,
    extract_shot_metadata,
    render_cache_png,
    resolve_cache_dir,
)


def prepare_single_frame(
    read_node: Any,
    mask_node: Any,
    frame: int,
    cache_root: str | Path | None = None,
    nuke_module: Any | None = None,
) -> ShotMetadata:
    """Render one frame plus the alpha of a Nuke mask node into the shared cache."""
    return prepare_masked_range(read_node, mask_node, frame, frame, cache_root, nuke_module)


def prepare_masked_range(
    read_node: Any,
    mask_node: Any,
    first: int,
    last: int,
    cache_root: str | Path | None = None,
    nuke_module: Any | None = None,
) -> ShotMetadata:
    """Export a plate and its authored alpha matte over the requested frame range."""
    nuke = _get_nuke(nuke_module)
    if not read_node.firstFrame() <= first <= last <= read_node.lastFrame():
        raise ValueError(f"Range {first}–{last} is outside the selected Read range")
    if "rgba.alpha" not in mask_node.channels():
        raise ValueError("The mask node must provide an rgba.alpha channel")
    if (mask_node.width(), mask_node.height()) != (read_node.width(), read_node.height()):
        raise ValueError("The mask must use the source format. Reformat the mask if needed.")

    shot_id = f"inpaint_{uuid.uuid4().hex[:12]}"
    mask_dir = resolve_cache_dir(cache_root) / shot_id / "masks"
    mask_dir.mkdir(parents=True)
    mask_path = (mask_dir / f"{shot_id}_mask.%04d.png").as_posix()
    shuffle = nuke.nodes.Shuffle(
        **{"in": "rgba", "red": "alpha", "green": "alpha", "blue": "alpha"}
    )
    try:
        shuffle.setInput(0, mask_node)
        render_cache_png(nuke, shuffle, mask_path, first, last)
    finally:
        nuke.delete(shuffle)
    return export_prep_sequence(read_node, cache_root, nuke, first, last, shot_id=shot_id)


def apply_single_frame(
    read_node: Any, manifest_path: str | Path, nuke_module: Any | None = None
) -> Any:
    """Build a reversible graph that replaces masked RGB only on the processed frame."""
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if not manifest.get("comfy_prompt_id") or manifest["first_frame"] != manifest["last_frame"]:
        raise ValueError("Select the apply manifest of a completed ComfyUI single-frame job")
    return _apply_manifest(read_node, manifest, _get_nuke(nuke_module))


def apply_result(
    read_node: Any,
    manifest_path: str | Path,
    nuke_module: Any | None = None,
    *,
    feather: float = 0.0,
    grain: bool = False,
) -> Any:
    """Apply a published Core patch or sequence to an explicitly selected source Read."""
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    return _apply_manifest(read_node, manifest, _get_nuke(nuke_module), feather, grain)


def _sequence_key(path: str) -> str:
    path = re.sub(r"#+", lambda match: f"%0{len(match.group())}d", path)
    return os.path.normcase(os.path.normpath(path))


def _apply_manifest(
    read_node: Any,
    manifest: dict[str, Any],
    nuke: Any,
    feather: float = 0.0,
    grain: bool = False,
) -> Any:
    supported = {"single_frame_inpaint", "tracked_clean_plate", "temporal_propainter"}
    mode = manifest.get("processing_mode")
    if not manifest.get("job_id") or (
        mode not in supported and (mode is not None or not manifest.get("comfy_prompt_id"))
    ):
        raise ValueError("Select an apply manifest from a completed removal job")
    first = int(manifest["first_frame"])
    last = int(manifest["last_frame"])
    metadata = extract_shot_metadata(read_node, nuke_module=nuke)
    comparable = (
        "node_name",
        "source_colorspace",
        "working_colorspace",
        "ai_prep_colorspace",
        "ocio_config_path",
        "color_management",
    )
    if _sequence_key(metadata.file_pattern) != _sequence_key(manifest["file_pattern"]) or any(
        getattr(metadata, field) != manifest[field] for field in comparable
    ):
        raise ValueError("The selected Read or color settings differ from the prepared shot")
    if not metadata.first_frame <= first <= last <= metadata.last_frame:
        raise ValueError(f"Range {first}–{last} is outside the selected Read range")
    if (manifest.get("width", metadata.width), manifest.get("height", metadata.height)) != (
        metadata.width,
        metadata.height,
    ):
        raise ValueError("The selected Read format differs from the prepared shot")

    patch_path = manifest["result_sequence_pattern"]
    if _uses_aces_exr_default(
        metadata.color_management, metadata.media_kind, str(read_node["colorspace"].value())
    ) and not ("raw" in read_node.knobs() and read_node["raw"].value()):
        raise ValueError(
            f"Set Read colorspace explicitly to {metadata.source_colorspace} before Apply "
            "so the source and prepared patch use the same ACES transform"
        )
    mask_path = manifest["result_mask_pattern"]
    for frame in range(first, last + 1):
        if not all(Path(path % frame).is_file() for path in (patch_path, mask_path)):
            raise FileNotFoundError(f"Result or mask is missing for frame {frame}")

    created: list[Any] = []
    try:
        for path in (patch_path, mask_path):
            node = nuke.nodes.Read(
                file=path, first=first, last=last, origfirst=first, origlast=last
            )
            created.append(node)
            node["raw"].setValue(True)
        patch, mask = created
        transform = create_color_transform(
            nuke,
            metadata.color_management,
            metadata.ai_prep_colorspace,
            metadata.working_colorspace,
        )
        created.append(transform)
        transform.setInput(0, patch)
        mask_alpha = nuke.nodes.Shuffle(**{"in": "rgba", "alpha": "red"})
        created.append(mask_alpha)
        mask_alpha.setInput(0, mask)
        patch_input, mask_input = transform, mask_alpha
        x, y = read_node.xpos(), read_node.ypos()
        if feather > 0.0:
            blurred = nuke.nodes.Blur(channels="alpha", size=feather, label="Mask feather")
            created.append(blurred)
            blurred.setInput(0, mask_alpha)
            blurred.setXYpos(x + 440, y + 135)
            mask_input = nuke.nodes.Merge2(
                operation="multiply",
                Achannels="alpha",
                Bchannels="alpha",
                output="alpha",
                label="Keep feather inside the mask",
            )
            created.append(mask_input)
            mask_input.setInput(0, mask_alpha)
            mask_input.setInput(1, blurred)
            mask_input.setXYpos(x + 320, y + 175)
        if grain:
            alpha_copy = nuke.nodes.Copy(from0="rgba.alpha", to0="rgba.alpha")
            created.append(alpha_copy)
            alpha_copy.setInput(0, transform)
            alpha_copy.setInput(1, mask_alpha)
            alpha_copy.setXYpos(x + 160, y + 115)
            patch_input = nuke.nodes.Grain2(label="Patch grain")
            created.append(patch_input)
            patch_input.setInput(0, alpha_copy)
            patch_input.setXYpos(x + 160, y + 165)
        keymix = nuke.nodes.Keymix(channels="rgb")
        created.append(keymix)
        keymix.setInput(0, read_node)
        keymix.setInput(1, patch_input)
        keymix.setInput(2, mask_input)
        composite = keymix
        if "rgba.alpha" not in read_node.channels() and "rgba.alpha" in keymix.channels():
            composite = nuke.nodes.Remove(operation="remove", channels="alpha")
            created.append(composite)
            composite.setInput(0, keymix)
            composite.setXYpos(x + 160, y + 270)
        switch = nuke.nodes.Switch(label=f"ComfyRemove · {first}–{last}")
        created.append(switch)
        switch.setInput(0, read_node)
        switch.setInput(1, composite)
        switch["which"].setExpression(f"frame >= {first} && frame <= {last}")
        patch.setXYpos(x + 160, y)
        mask.setXYpos(x + 320, y)
        transform.setXYpos(x + 160, y + 75)
        mask_alpha.setXYpos(x + 320, y + 75)
        keymix.setXYpos(x + 160, y + 205)
        switch.setXYpos(x, y + 340)
    except Exception:
        for node in reversed(created):
            nuke.delete(node)
        raise
    return switch
