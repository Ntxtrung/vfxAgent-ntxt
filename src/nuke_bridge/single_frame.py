"""Prepare a masked Nuke frame and composite its completed ComfyUI patch."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from src.contracts.models import ShotMetadata
from src.nuke_bridge.exporter import (
    _get_nuke,
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
    nuke = _get_nuke(nuke_module)
    if not read_node.firstFrame() <= frame <= read_node.lastFrame():
        raise ValueError(f"Frame {frame} is outside the selected Read range")
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
        render_cache_png(nuke, shuffle, mask_path, frame, frame)
    finally:
        nuke.delete(shuffle)
    return export_prep_sequence(read_node, cache_root, nuke, frame, frame, shot_id=shot_id)


def apply_single_frame(
    read_node: Any, manifest_path: str | Path, nuke_module: Any | None = None
) -> Any:
    """Build a reversible graph that replaces masked RGB only on the processed frame."""
    nuke = _get_nuke(nuke_module)
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if not manifest.get("comfy_prompt_id") or manifest["first_frame"] != manifest["last_frame"]:
        raise ValueError("Select the apply manifest of a completed ComfyUI single-frame job")
    frame = int(manifest["first_frame"])
    metadata = extract_shot_metadata(read_node, nuke_module=nuke)
    comparable = (
        "node_name",
        "file_pattern",
        "source_colorspace",
        "working_colorspace",
        "ai_prep_colorspace",
        "ocio_config_path",
        "color_management",
    )
    if any(getattr(metadata, field) != manifest[field] for field in comparable):
        raise ValueError("The selected Read or color settings differ from the prepared shot")
    if not metadata.first_frame <= frame <= metadata.last_frame:
        raise ValueError(f"Frame {frame} is outside the selected Read range")

    patch_path = manifest["result_sequence_pattern"]
    mask_path = manifest["result_mask_pattern"]
    if not all(Path(path % frame).is_file() for path in (patch_path, mask_path)):
        raise FileNotFoundError("ComfyUI patch or matching mask is missing")

    created: list[Any] = []
    try:
        for path in (patch_path, mask_path):
            node = nuke.nodes.Read(
                file=path, first=frame, last=frame, origfirst=frame, origlast=frame
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
        keymix = nuke.nodes.Keymix(channels="rgb")
        created.append(keymix)
        keymix.setInput(0, read_node)
        keymix.setInput(1, transform)
        keymix.setInput(2, mask_alpha)
        x, y = read_node.xpos(), read_node.ypos()
        composite = keymix
        if "rgba.alpha" not in read_node.channels() and "rgba.alpha" in keymix.channels():
            composite = nuke.nodes.Remove(operation="remove", channels="alpha")
            created.append(composite)
            composite.setInput(0, keymix)
            composite.setXYpos(x + 160, y + 230)
        switch = nuke.nodes.Switch(label=f"ComfyRemove · frame {frame}")
        created.append(switch)
        switch.setInput(0, read_node)
        switch.setInput(1, composite)
        switch["which"].setExpression(f"frame == {frame}")
        patch.setXYpos(x + 160, y)
        mask.setXYpos(x + 320, y)
        transform.setXYpos(x + 160, y + 75)
        mask_alpha.setXYpos(x + 320, y + 75)
        keymix.setXYpos(x + 160, y + 155)
        switch.setXYpos(x, y + 305)
    except Exception:
        for node in reversed(created):
            nuke.delete(node)
        raise
    return switch
