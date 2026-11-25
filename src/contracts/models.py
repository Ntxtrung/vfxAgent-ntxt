"""Shared data contracts for Nuke Bridge, Core Worker, and UI Process."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path


class ColorManagementMode(StrEnum):
    """Supported Foundry Nuke color management modes."""

    ACES_1_2 = "aces_1_2"
    NUKE_DEFAULT = "nuke_default"
    OCIO_CUSTOM = "ocio_custom"


class MediaKind(StrEnum):
    """Supported footage container/sequence types from Nuke Read nodes."""

    EXR = "exr"
    DPX = "dpx"
    MOV = "mov"
    MP4 = "mp4"
    PNG_SEQ = "png_seq"


class JobStatus(StrEnum):
    """Lifecycle states for an asynchronous Core removal/inpaint job."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class RemoveMode(StrEnum):
    """Inpainting/removal strategies supported by the Core worker."""

    TEMPORAL_PROPAINTER = "temporal_propainter"
    CLEAN_PLATE_REF = "clean_plate_ref"
    PROMPT_REF_INPAINT = "prompt_ref_inpaint"


@dataclass(slots=True)
class ShotMetadata:
    """Normalized shot metadata exported from Nuke Read for Core and UI."""

    shot_id: str
    node_name: str
    file_pattern: str
    media_kind: MediaKind
    first_frame: int
    last_frame: int
    width: int
    height: int
    fps: float
    color_management: ColorManagementMode
    source_colorspace: str
    working_colorspace: str
    ai_prep_colorspace: str
    cache_sequence_pattern: str
    ocio_config_path: str = ""
    mask_input_pattern: str = ""

    def to_json(self) -> str:
        """Serialize metadata to formatted JSON."""
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, raw: str) -> ShotMetadata:
        """Deserialize metadata from a JSON string."""
        data = json.loads(raw)
        return cls(
            shot_id=data["shot_id"],
            node_name=data["node_name"],
            file_pattern=data["file_pattern"],
            media_kind=MediaKind(data["media_kind"]),
            first_frame=int(data["first_frame"]),
            last_frame=int(data["last_frame"]),
            width=int(data["width"]),
            height=int(data["height"]),
            fps=float(data["fps"]),
            color_management=ColorManagementMode(data["color_management"]),
            source_colorspace=data["source_colorspace"],
            working_colorspace=data["working_colorspace"],
            ai_prep_colorspace=data["ai_prep_colorspace"],
            cache_sequence_pattern=data["cache_sequence_pattern"],
            ocio_config_path=data.get("ocio_config_path", ""),
            mask_input_pattern=data.get("mask_input_pattern", ""),
        )

    def save(self, path: Path) -> Path:
        """Write metadata JSON to disk."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json(), encoding="utf-8")
        return path


@dataclass(slots=True)
class RemoveJobRequest:
    """Payload sent from UI Process to Core Process to run AI removal."""

    shot_id: str
    mode: RemoveMode
    first_frame: int
    last_frame: int
    prompt: str = ""
    reference_image_path: str = ""
    mask_pattern: str = ""
    mask_expand_px: int = 4
    mask_feather_px: int = 6
    seed: int = 42
    sam2_points: list[dict[str, float | int]] = field(default_factory=list)


@dataclass(slots=True)
class RemoveJobResponse:
    """Status and output paths returned by Core Process for a job."""

    job_id: str
    shot_id: str
    status: JobStatus
    progress: float = 0.0
    result_sequence_pattern: str = ""
    result_mask_pattern: str = ""
    error_message: str = ""
