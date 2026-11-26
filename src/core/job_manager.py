"""Asynchronous job queue and frame-cache processor for Core Worker."""

from __future__ import annotations

import json
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageOps

from src.contracts.models import (
    ColorManagementMode,
    JobStatus,
    MediaKind,
    RemoveJobRequest,
    RemoveJobResponse,
    RemoveMode,
    ShotMetadata,
)
from src.core.comfy_client import ComfyUIClient


class JobManager:
    """Manages shot metadata, disk frame cache, and background AI removal jobs."""

    def __init__(
        self,
        cache_dir: Path,
        comfy_client: ComfyUIClient,
        logger: logging.Logger,
        inpaint_checkpoint: str = "",
    ) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.comfy_client = comfy_client
        self.logger = logger
        self.inpaint_checkpoint = inpaint_checkpoint
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="core-worker")
        self._jobs: dict[str, RemoveJobResponse] = {}

    def list_shots(self) -> list[ShotMetadata]:
        """Return all shots that have a valid shot_metadata.json under cache_dir."""
        shots: list[ShotMetadata] = []
        for meta_file in sorted(self.cache_dir.glob("*/shot_metadata.json")):
            shots.append(ShotMetadata.from_json(meta_file.read_text(encoding="utf-8")))
        return shots

    def get_shot(self, shot_id: str) -> ShotMetadata:
        """Load ShotMetadata for shot_id or raise FileNotFoundError."""
        meta_path = self.cache_dir / shot_id / "shot_metadata.json"
        if not meta_path.is_file():
            raise FileNotFoundError(f"Shot metadata not found for '{shot_id}'")
        return ShotMetadata.from_json(meta_path.read_text(encoding="utf-8"))

    def create_demo_shot(self, shot_id: str = "demo_shot", num_frames: int = 8) -> ShotMetadata:
        """Generate a synthetic 8-frame test sequence in cache_dir for UI/E2E testing."""
        shot_dir = self.cache_dir / shot_id
        input_dir = shot_dir / "input"
        input_dir.mkdir(parents=True, exist_ok=True)

        width, height = 960, 540
        first_frame = 1001
        last_frame = first_frame + num_frames - 1

        for idx, frame in enumerate(range(first_frame, last_frame + 1)):
            img = Image.new("RGB", (width, height), (28, 34, 44))
            draw = ImageDraw.Draw(img)
            # Background grid
            for x in range(0, width, 60):
                draw.line([(x, 0), (x, height)], fill=(42, 50, 64), width=1)
            for y in range(0, height, 60):
                draw.line([(0, y), (width, y)], fill=(42, 50, 64), width=1)
            # Moving foreground object to remove
            cx = 220 + idx * 70
            cy = 270
            draw.ellipse([cx - 45, cy - 85, cx + 45, cy + 85], fill=(220, 75, 65))
            img.save(input_dir / f"{shot_id}_input.{frame:04d}.png")

        metadata = ShotMetadata(
            shot_id=shot_id,
            node_name="Read_Demo",
            file_pattern=(input_dir / f"{shot_id}_input.%04d.png").as_posix(),
            media_kind=MediaKind.EXR,
            first_frame=first_frame,
            last_frame=last_frame,
            width=width,
            height=height,
            fps=24.0,
            color_management=ColorManagementMode.ACES_1_2,
            source_colorspace="ACES - ACEScg",
            working_colorspace="ACES - ACEScg",
            ai_prep_colorspace="Output - sRGB",
            cache_sequence_pattern=(input_dir / f"{shot_id}_input.%04d.png").as_posix(),
            is_demo=True,
        )
        metadata.save(shot_dir / "shot_metadata.json")
        self.logger.info("Created demo shot '%s' (%d frames)", shot_id, num_frames)
        return metadata

    def frame_path(self, shot_id: str, kind: str, frame: int) -> Path:
        """Resolve path for an input, mask, or result frame PNG."""
        if kind == "input":
            return self.cache_dir / shot_id / "input" / f"{shot_id}_input.{frame:04d}.png"
        if kind == "mask":
            return self.cache_dir / shot_id / "masks" / f"{shot_id}_mask.{frame:04d}.png"
        if kind == "result":
            return self.cache_dir / shot_id / "result" / f"{shot_id}_result.{frame:04d}.png"
        raise ValueError(f"Unknown frame kind: {kind}")

    def submit_job(self, request: RemoveJobRequest) -> RemoveJobResponse:
        """Enqueue a removal/inpaint job on the background worker thread."""
        metadata = self.get_shot(request.shot_id)
        single_frame = request.mode == RemoveMode.SINGLE_FRAME_INPAINT
        if single_frame:
            if not self.inpaint_checkpoint:
                raise ValueError("Set COMFYUI_INPAINT_CHECKPOINT to a local checkpoint name")
            if not (
                metadata.first_frame
                == request.first_frame
                == request.last_frame
                == metadata.last_frame
            ):
                raise ValueError("Prepare and submit exactly one frame for single_frame_inpaint")
            if not request.prompt.strip():
                raise ValueError("Provide a prompt describing the background to reconstruct")
        elif not metadata.is_demo:
            raise ValueError("Nuke footage currently requires single_frame_inpaint")
        job_id = uuid.uuid4().hex[:12]
        shot_dir = self.cache_dir / metadata.shot_id
        result_dir = shot_dir / "result"
        mask_dir = shot_dir / "masks"
        if single_frame:
            result_dir = mask_dir = shot_dir / "jobs" / job_id

        job = RemoveJobResponse(
            job_id=job_id,
            shot_id=metadata.shot_id,
            status=JobStatus.PENDING,
            progress=0.0,
            result_sequence_pattern=(result_dir / f"{metadata.shot_id}_result.%04d.png").as_posix(),
            result_mask_pattern=(mask_dir / f"{metadata.shot_id}_mask.%04d.png").as_posix(),
        )
        self._jobs[job_id] = job
        self._executor.submit(self._run_job, job_id, request, metadata)
        return job

    def get_job(self, job_id: str) -> RemoveJobResponse:
        """Return current status of job_id."""
        if job_id not in self._jobs:
            raise KeyError(f"Job '{job_id}' not found")
        return self._jobs[job_id]

    def _run_job(
        self,
        job_id: str,
        request: RemoveJobRequest,
        metadata: ShotMetadata,
    ) -> None:
        """Execute removal pipeline in background thread (never blocks UI/API)."""
        job = self._jobs[job_id]
        job.status = JobStatus.RUNNING
        self.logger.info("Starting job %s for shot %s (%s)", job_id, metadata.shot_id, request.mode)

        try:
            if request.mode == RemoveMode.SINGLE_FRAME_INPAINT:
                self._run_single_frame(job, request, metadata)
                job.status = JobStatus.COMPLETED
                job.progress = 1.0
                self.logger.info("Completed ComfyUI inpaint job %s", job_id)
                return

            workflow = self.comfy_client.build_workflow(request, metadata)
            workflow_path = self.cache_dir / metadata.shot_id / f"workflow_{job_id}.json"
            workflow_path.write_text(json.dumps(workflow, indent=2), encoding="utf-8")

            total = max(1, request.last_frame - request.first_frame + 1)
            ref_img: Image.Image | None = None
            if request.reference_image_path and Path(request.reference_image_path).is_file():
                ref_img = Image.open(request.reference_image_path).convert("RGB")

            for idx, frame in enumerate(range(request.first_frame, request.last_frame + 1)):
                src_path = self.frame_path(metadata.shot_id, "input", frame)
                if not src_path.is_file():
                    raise FileNotFoundError(f"Missing input frame: {src_path}")

                out_path = self.frame_path(metadata.shot_id, "result", frame)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                mask_path = self.frame_path(metadata.shot_id, "mask", frame)
                mask_path.parent.mkdir(parents=True, exist_ok=True)

                with Image.open(src_path).convert("RGB") as src_img:
                    mask_img = self._load_or_create_mask(mask_path, src_img.size, request)
                    if request.mask_feather_px > 0:
                        mask_img = mask_img.filter(
                            ImageFilter.GaussianBlur(radius=max(1, request.mask_feather_px // 2))
                        )
                    fill_source = (
                        ref_img.resize(src_img.size)
                        if ref_img is not None
                        else src_img.filter(ImageFilter.GaussianBlur(radius=18))
                    )
                    composited = Image.composite(fill_source, src_img, mask_img)
                    composited.save(out_path)
                    mask_img.save(mask_path)

                job.progress = round((idx + 1) / total, 3)

            job.status = JobStatus.COMPLETED
            job.progress = 1.0
            self.logger.info("Completed job %s for shot %s", job_id, metadata.shot_id)
        except Exception as exc:
            job.status = JobStatus.FAILED
            job.error_message = str(exc)
            self.logger.exception("Job %s failed: %s", job_id, exc)

    def _run_single_frame(
        self, job: RemoveJobResponse, request: RemoveJobRequest, metadata: ShotMetadata
    ) -> None:
        frame = request.first_frame
        source = self.frame_path(metadata.shot_id, "input", frame)
        mask_path = self.frame_path(metadata.shot_id, "mask", frame)
        result = Path(job.result_sequence_pattern % frame)
        mask_snapshot = Path(job.result_mask_pattern % frame)
        size = (metadata.width, metadata.height)
        result.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(source) as image, Image.open(mask_path) as mask_image:
            if image.size != size or mask_image.size != size:
                raise ValueError("The source and mask must match the prepared frame dimensions")
            mask = mask_image.convert("RGB").getchannel("R")
            bounds = mask.getbbox()
            if bounds is None:
                raise ValueError("The inpaint mask is empty")
            mask.save(mask_snapshot)
            plate = image.convert("RGB")

        left, top, right, bottom = bounds
        context = 112
        crop_box = (
            max(0, left - context),
            max(0, top - context),
            min(size[0], right + context),
            min(size[1], bottom + context),
        )
        crop = plate.crop(crop_box)
        ai_input = crop.copy()
        ai_input.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
        ai_mask = mask.crop(crop_box).resize(ai_input.size, Image.Resampling.LANCZOS)
        padding = (0, 0, -ai_input.width % 8, -ai_input.height % 8)
        input_crop = result.parent / "input_crop.png"
        mask_crop = result.parent / "mask_crop.png"
        ImageOps.expand(ai_input, padding).save(input_crop)
        ImageOps.expand(ai_mask, padding).save(mask_crop)

        subfolder = f"comfyremove/{job.job_id}"
        workflow = self.comfy_client.single_frame_workflow(
            self.comfy_client.upload_image(input_crop, subfolder),
            self.comfy_client.upload_image(mask_crop, subfolder),
            self.inpaint_checkpoint,
            request.prompt,
            request.seed,
            request.negative_prompt,
        )
        workflow_path = result.parent / "workflow.json"
        workflow_path.write_text(json.dumps(workflow, indent=2), encoding="utf-8")
        prompt_id = self.comfy_client.queue_prompt(workflow)
        self.logger.info("ComfyUI prompt %s for job %s", prompt_id, job.job_id)
        job.progress = 0.5
        patch = self.comfy_client.wait_for_patch(prompt_id)
        with Image.open(BytesIO(patch)) as image:
            if image.size != (ai_input.width + padding[2], ai_input.height + padding[3]):
                raise ValueError("ComfyUI returned a patch with unexpected dimensions")
            if image.format != "PNG":
                raise ValueError("ComfyUI returned a patch in an unexpected file format")
            patch_crop = image.crop((0, 0, ai_input.width, ai_input.height)).resize(
                crop.size, Image.Resampling.LANCZOS
            )
            plate.paste(patch_crop, crop_box[:2])
        plate.save(result)
        manifest = {
            "job_id": job.job_id,
            "comfy_prompt_id": prompt_id,
            "shot_id": metadata.shot_id,
            "node_name": metadata.node_name,
            "file_pattern": metadata.file_pattern,
            "first_frame": frame,
            "last_frame": frame,
            "color_management": metadata.color_management.value,
            "ocio_config_path": metadata.ocio_config_path,
            "source_colorspace": metadata.source_colorspace,
            "working_colorspace": metadata.working_colorspace,
            "ai_prep_colorspace": metadata.ai_prep_colorspace,
            "result_sequence_pattern": job.result_sequence_pattern,
            "result_mask_pattern": job.result_mask_pattern,
        }
        manifest_path = result.parent / "apply_manifest.json"
        pending_manifest = manifest_path.with_suffix(".tmp")
        pending_manifest.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        pending_manifest.replace(manifest_path)
        job.apply_manifest_path = manifest_path.as_posix()

    @staticmethod
    def _load_or_create_mask(
        mask_path: Path,
        size: tuple[int, int],
        request: RemoveJobRequest,
    ) -> Image.Image:
        """Load user mask PNG or synthesize one from SAM2 click points."""
        if mask_path.is_file():
            return Image.open(mask_path).convert("L").resize(size)
        mask = Image.new("L", size, 0)
        if request.sam2_points:
            draw = ImageDraw.Draw(mask)
            radius = max(24, 32 + request.mask_expand_px)
            for pt in request.sam2_points:
                x = int(float(pt.get("x", 0.5)) * size[0])
                y = int(float(pt.get("y", 0.5)) * size[1])
                draw.ellipse([x - radius, y - radius, x + radius, y + radius], fill=255)
        return mask

    def write_apply_manifest(self, shot_id: str, enable_grain: bool = True) -> dict[str, Any]:
        """Write apply_manifest.json for Nuke F4 auto-composite."""
        metadata = self.get_shot(shot_id)
        if not metadata.is_demo:
            raise ValueError("Use apply_manifest_path from the completed single-frame job")
        result_pattern = (
            self.cache_dir / shot_id / "result" / f"{shot_id}_result.%04d.png"
        ).as_posix()
        mask_pattern = (self.cache_dir / shot_id / "masks" / f"{shot_id}_mask.%04d.png").as_posix()
        manifest = {
            "shot_id": shot_id,
            "node_name": metadata.node_name,
            "first_frame": metadata.first_frame,
            "last_frame": metadata.last_frame,
            "color_management": metadata.color_management.value,
            "ai_prep_colorspace": metadata.ai_prep_colorspace,
            "working_colorspace": metadata.working_colorspace,
            "source_colorspace": metadata.source_colorspace,
            "result_sequence_pattern": result_pattern,
            "result_mask_pattern": mask_pattern,
            "enable_grain": enable_grain,
        }
        manifest_path = self.cache_dir / shot_id / "apply_manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        self.logger.info("Wrote Nuke apply manifest for shot %s", shot_id)
        return manifest
