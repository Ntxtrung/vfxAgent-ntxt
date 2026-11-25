"""Foundry Nuke menu registration and Neat-Video-style UI launcher."""

from __future__ import annotations

import os
import shutil
import subprocess
import webbrowser
from typing import Any

from src.nuke_bridge.exporter import export_prep_sequence


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
