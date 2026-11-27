"""Unit tests for F1: Contracts and Nuke Bridge Read -> Prep Cache export."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from src.contracts.models import ColorManagementMode, MediaKind, ShotMetadata
from src.nuke_bridge.colorspace import (
    detect_color_management,
    detect_media_kind,
    normalize_colorspace_name,
    resolve_ai_prep_colorspace,
)
from src.nuke_bridge.exporter import export_prep_sequence, extract_shot_metadata


class _FakeKnob:
    def __init__(self, value: Any) -> None:
        self._value = value

    def value(self) -> Any:
        return self._value

    def setValue(self, value: Any) -> None:
        self._value = value


class _FakeFormat:
    def __init__(self, width: int, height: int) -> None:
        self._w = width
        self._h = height

    def width(self) -> int:
        return self._w

    def height(self) -> int:
        return self._h


class _FakeNode:
    def __init__(self, name: str, knobs: dict[str, Any], fmt: _FakeFormat | None = None) -> None:
        self._name = name
        self._knobs = {k: _FakeKnob(v) for k, v in knobs.items()}
        self._fmt = fmt or _FakeFormat(1920, 1080)
        self.inputs: dict[int, Any] = {}

    def name(self) -> str:
        return self._name

    def knobs(self) -> dict[str, _FakeKnob]:
        return self._knobs

    def __getitem__(self, key: str) -> _FakeKnob:
        return self._knobs[key]

    def format(self) -> _FakeFormat:
        return self._fmt

    def setInput(self, index: int, node: Any) -> None:
        self.inputs[index] = node


class _FakeNodesFactory:
    def __init__(self, created: list[tuple[str, dict[str, Any], _FakeNode]]) -> None:
        self._created = created

    def OCIOColorSpace(self, **kwargs: Any) -> _FakeNode:
        node = _FakeNode("OCIOColorSpace1", kwargs)
        self._created.append(("OCIOColorSpace", kwargs, node))
        return node

    def Colorspace(self, **kwargs: Any) -> _FakeNode:
        node = _FakeNode("Colorspace1", kwargs)
        self._created.append(("Colorspace", kwargs, node))
        return node

    def Write(self, **kwargs: Any) -> _FakeNode:
        knobs = dict(kwargs)
        knobs.setdefault("colorspace", "default")
        node = _FakeNode("Write1", knobs)
        self._created.append(("Write", kwargs, node))
        return node


class _FakeNuke:
    def __init__(self, root_knobs: dict[str, Any]) -> None:
        self._root = _FakeNode("root", root_knobs)
        self.created_nodes: list[tuple[str, dict[str, Any], _FakeNode]] = []
        self.executed: list[tuple[str, int, int]] = []
        self.rendered_source_colorspaces: list[str] = []
        self.deleted: list[str] = []
        self.nodes = _FakeNodesFactory(self.created_nodes)

    def root(self) -> _FakeNode:
        return self._root

    def execute(self, node: _FakeNode, first: int, last: int) -> None:
        self.executed.append((node.name(), first, last))
        read_node = node.inputs[0].inputs[0]
        self.rendered_source_colorspaces.append(read_node["colorspace"].value())

    def delete(self, node: _FakeNode) -> None:
        self.deleted.append(node.name())


class TestColorspaceAndContracts(unittest.TestCase):
    """Test media detection, colorspace mapping, and ShotMetadata serialization."""

    def test_detect_media_kind(self) -> None:
        self.assertEqual(detect_media_kind("D:/plates/shot_01.%04d.exr"), MediaKind.EXR)
        self.assertEqual(detect_media_kind("D:/plates/shot_01.%04d.DPX"), MediaKind.DPX)
        self.assertEqual(detect_media_kind("D:/plates/clip.mov"), MediaKind.MOV)
        self.assertEqual(detect_media_kind("D:/plates/clip.mp4"), MediaKind.MP4)
        with self.assertRaises(ValueError):
            detect_media_kind("D:/plates/clip.avi")

    def test_aces_1_2_and_default_detection(self) -> None:
        aces = detect_color_management("OCIO", "aces_1.2")
        self.assertEqual(aces, ColorManagementMode.ACES_1_2)
        self.assertEqual(resolve_ai_prep_colorspace(aces, "ACES - ACEScg"), "Output - sRGB")

        nuke_def = detect_color_management("Nuke", "")
        self.assertEqual(nuke_def, ColorManagementMode.NUKE_DEFAULT)
        self.assertEqual(resolve_ai_prep_colorspace(nuke_def, "linear"), "sRGB")

    def test_normalize_colorspace_name(self) -> None:
        self.assertEqual(normalize_colorspace_name("default (ACES - ACEScg)"), "ACES - ACEScg")
        self.assertEqual(normalize_colorspace_name("linear"), "linear")


class TestNukeExporter(unittest.TestCase):
    """Test Read metadata extraction and temporary prep export graph."""

    def test_extract_and_export_aces_exr(self) -> None:
        fake_nuke = _FakeNuke(
            {
                "fps": 24.0,
                "colorManagement": "OCIO",
                "OCIO_config": "aces_1.2",
                "workingSpaceLUT": "ACES - ACEScg",
                "floatLut": "scene_linear",
            }
        )
        read_node = _FakeNode(
            "Read_Shot01",
            {
                "file": "G:/shots/sh010/plate.%04d.exr",
                "first": 1001,
                "last": 1024,
                "colorspace": "default (linear)",
            },
            fmt=_FakeFormat(3840, 2160),
        )

        with tempfile.TemporaryDirectory() as tmp:
            meta = export_prep_sequence(read_node, cache_root=tmp, nuke_module=fake_nuke)
            self.assertEqual(meta.media_kind, MediaKind.EXR)
            self.assertEqual(meta.color_management, ColorManagementMode.ACES_1_2)
            self.assertEqual(meta.width, 3840)
            self.assertEqual(meta.height, 2160)
            self.assertEqual(meta.ai_prep_colorspace, "Output - sRGB")

            self.assertEqual(fake_nuke.created_nodes[0][0], "OCIOColorSpace")
            self.assertEqual(fake_nuke.created_nodes[1][0], "Write")
            self.assertEqual(fake_nuke.executed, [("Write1", 1001, 1024)])
            self.assertEqual(fake_nuke.deleted, ["Write1", "OCIOColorSpace1"])
            self.assertEqual(fake_nuke.rendered_source_colorspaces, ["ACES - ACEScg"])
            self.assertEqual(read_node["colorspace"].value(), "default")

            saved_json = Path(tmp) / "Read_Shot01" / "shot_metadata.json"
            self.assertTrue(saved_json.is_file())
            reloaded = ShotMetadata.from_json(saved_json.read_text(encoding="utf-8"))
            self.assertEqual(reloaded.shot_id, "Read_Shot01")
            self.assertEqual(reloaded.source_colorspace, "ACES - ACEScg")

    def test_extract_nuke_default_mov(self) -> None:
        fake_nuke = _FakeNuke(
            {
                "fps": 25.0,
                "colorManagement": "Nuke",
                "workingSpaceLUT": "linear",
            }
        )
        read_node = _FakeNode(
            "Read_Mov01",
            {
                "file": "G:/shots/sh020/clip.mov",
                "first": 1,
                "last": 50,
                "colorspace": "rec709",
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            meta = extract_shot_metadata(read_node, cache_root=tmp, nuke_module=fake_nuke)
            self.assertEqual(meta.media_kind, MediaKind.MOV)
            self.assertEqual(meta.color_management, ColorManagementMode.NUKE_DEFAULT)
            self.assertEqual(meta.ai_prep_colorspace, "sRGB")


if __name__ == "__main__":
    unittest.main()
