"""Foundry Nuke menu registration and Neat-Video-style UI launcher."""

from __future__ import annotations

import os
import shutil
import subprocess
import webbrowser
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

from src.nuke_bridge.exporter import export_prep_sequence
from src.nuke_bridge.single_frame import (
    apply_result,
    apply_single_frame,
    prepare_masked_range,
    prepare_single_frame,
)


def _find_app_browser() -> str | None:
    """Locate Edge or Chrome executable for frameless App Window mode."""
    for candidate in ("msedge", "chrome", "google-chrome"):
        found = shutil.which(candidate)
        if found:
            return found
    return None


def open_ui_window(shot_id: str) -> None:
    """Open the separate UI process in frameless App Window mode without blocking Nuke."""
    ui_host = os.environ.get("UI_HOST", "127.0.0.1")
    ui_port = os.environ.get("UI_PORT", "5173")
    url = f"http://{ui_host}:{ui_port}/?{urlencode({'shot': shot_id})}"

    browser_exe = _find_app_browser()
    if browser_exe:
        subprocess.Popen([browser_exe, f"--app={url}"])
        return
    webbrowser.open(url)


def launch_comfy_remove(nuke_module: Any | None = None) -> None:
    """Export selected Read node prep sequence and launch external Remove/Inpaint UI."""
    if nuke_module is None:
        import nuke
    else:
        nuke = nuke_module

    selected = nuke.selectedNodes()
    reads = [node for node in selected if node.Class() == "Read"]
    masks = [node for node in selected if node.Class() != "Read"]
    if len(reads) != 1 or len(masks) > 1:
        nuke.message("Select one source Read and, optionally, one node with a mask in alpha.")
        return
    read = reads[0]
    panel = nuke.Panel("Prepare shot for ComfyRemove")
    panel.addSingleLineInput("First frame", str(read.firstFrame()))
    panel.addSingleLineInput("Last frame", str(read.lastFrame()))
    if not panel.show():
        return
    try:
        first, last = int(panel.value("First frame")), int(panel.value("Last frame"))
        if not read.firstFrame() <= first <= last <= read.lastFrame():
            raise ValueError("Select a frame range within the original Read")
        if masks:
            metadata = prepare_masked_range(read, masks[0], first, last, nuke_module=nuke)
        else:
            metadata = export_prep_sequence(
                read,
                nuke_module=nuke,
                first_frame=first,
                last_frame=last,
                shot_id=f"inpaint_{uuid4().hex[:12]}",
            )
    except (ValueError, RuntimeError, OSError) as exc:
        nuke.message(str(exc))
        return
    open_ui_window(metadata.shot_id)


def register_menu(nuke_module: Any | None = None) -> None:
    """Register ComfyRemove command in Nuke Nodes toolbar."""
    if nuke_module is None:
        import nuke
    else:
        nuke = nuke_module

    toolbar = nuke.menu("Nodes")
    comfy_menu = toolbar.addMenu("ComfyRemove")
    comfy_menu.addCommand("Open Remove/Inpaint UI", launch_comfy_remove)

    def prepare() -> None:
        selected = nuke.selectedNodes()
        reads = [node for node in selected if node.Class() == "Read"]
        masks = [node for node in selected if node.Class() != "Read"]
        if len(reads) != 1 or len(masks) != 1:
            nuke.message("Select the source Read and one mask node with an alpha channel.")
            return
        metadata = prepare_single_frame(reads[0], masks[0], int(nuke.frame()), nuke_module=nuke)
        nuke.message(f"Prepared {metadata.shot_id}, frame {metadata.first_frame}. Submit via API.")

    def apply() -> None:
        reads = nuke.selectedNodes("Read")
        if len(reads) != 1:
            nuke.message("Select the original Read before applying a patch.")
            return
        path = nuke.getFilename("Choose a completed job's apply manifest", "*.json")
        if path:
            try:
                apply_single_frame(reads[0], path, nuke_module=nuke)
            except (ValueError, RuntimeError, OSError, KeyError) as exc:
                nuke.message(str(exc))

    def apply_shot() -> None:
        reads = nuke.selectedNodes("Read")
        if len(reads) != 1:
            nuke.message("Select the original Read before applying a result.")
            return
        path = nuke.getFilename("Choose a completed job's apply manifest", "*.json")
        if not path:
            return
        controls = nuke.Panel("Apply ComfyRemove result")
        controls.addSingleLineInput("Extra mask feather (px)", "0")
        controls.addBooleanCheckBox("Add grain to patch", False)
        if not controls.show():
            return
        try:
            feather = float(controls.value("Extra mask feather (px)"))
            if not 0 <= feather <= 128:
                raise ValueError("Mask feather must be between 0 and 128 pixels")
            apply_result(
                reads[0],
                path,
                nuke_module=nuke,
                feather=feather,
                grain=bool(controls.value("Add grain to patch")),
            )
        except (ValueError, RuntimeError, OSError, KeyError) as exc:
            nuke.message(str(exc))

    comfy_menu.addCommand("Prepare Current Frame + Mask", prepare)
    comfy_menu.addCommand("Apply Single-Frame Patch", apply)
    comfy_menu.addCommand("Apply Core Result", apply_shot)
