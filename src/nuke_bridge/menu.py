"""Foundry Nuke menu registration and Neat-Video-style UI launcher."""

from __future__ import annotations

import os
import shutil
import subprocess
import webbrowser
from typing import Any

from src.nuke_bridge.exporter import export_prep_sequence
from src.nuke_bridge.single_frame import apply_single_frame, prepare_single_frame


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
    url = f"http://{ui_host}:{ui_port}/?shot={shot_id}"

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

    selected = nuke.selectedNodes("Read")
    if not selected:
        nuke.message("Please select a Read node (EXR/DPX/MOV/MP4) first.")
        return

    metadata = export_prep_sequence(selected[0], nuke_module=nuke)
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
            apply_single_frame(reads[0], path, nuke_module=nuke)

    comfy_menu.addCommand("Prepare Current Frame + Mask", prepare)
    comfy_menu.addCommand("Apply Single-Frame Patch", apply)
