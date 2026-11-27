"""Asynchronous job queue and frame-cache processor for Core Worker."""

from __future__ import annotations

import json
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any

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
from src.core.config import CoreConfig
from src.core.model_sequence import (
    load_luminance_mask,
    model_mask,
    prepare_mask,
    restore_patch,
    run_propainter,
    run_sam2,
    validate_runtime,
)

if TYPE_CHECKING:
    from numpy.typing import NDArray


class JobManager:
    """Manages shot metadata, disk frame cache, and background AI removal jobs."""

    def __init__(
        self,
        config: CoreConfig,
        comfy_client: ComfyUIClient,
        logger: logging.Logger,
    ) -> None:
        self.config = config
        self.cache_dir = config.cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.comfy_client = comfy_client
        self.logger = logger
        self.inpaint_checkpoint = config.inpaint_checkpoint
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
        sequence = request.mode == RemoveMode.TRACKED_CLEAN_PLATE
        model_sequence = self._uses_model_sequence(request, metadata)
        if request.mask_keyframes and not model_sequence:
            raise ValueError("Mask keyframes require sam2_track or temporal_propainter")
        if single_frame or (sequence and not request.reference_image_path):
            if not self.inpaint_checkpoint:
                raise ValueError("Set COMFYUI_INPAINT_CHECKPOINT to a local checkpoint name")
            if not request.prompt.strip():
                raise ValueError("Provide a prompt describing the background to reconstruct")
        if single_frame:
            if not (
                metadata.first_frame
                == request.first_frame
                == request.last_frame
                == metadata.last_frame
            ):
                raise ValueError("Prepare and submit exactly one frame for single_frame_inpaint")
        elif sequence:
            if (
                not metadata.first_frame
                <= request.first_frame
                < request.last_frame
                <= (metadata.last_frame)
            ):
                raise ValueError("Submit at least two frames within the prepared shot range")
            if request.anchor_frame is not None and not (
                request.first_frame <= request.anchor_frame <= request.last_frame
            ):
                raise ValueError("The anchor frame must be inside the requested job range")
            if request.reference_image_path:
                location = Path(request.reference_image_path).resolve()
                ref_dir = (self.cache_dir / metadata.shot_id / "ref").resolve()
                if not location.is_relative_to(ref_dir) or not location.is_file():
                    raise ValueError(
                        "Upload the clean plate to the reference endpoint for this shot"
                    )
        elif model_sequence:
            self._validate_model_job(request, metadata)
        elif not metadata.is_demo:
            raise ValueError("Unsupported removal mode for prepared footage")
        job_id = uuid.uuid4().hex[:12]
        shot_dir = self.cache_dir / metadata.shot_id
        result_dir = shot_dir / "result"
        mask_dir = shot_dir / "masks"
        if single_frame or sequence or model_sequence:
            result_dir = mask_dir = shot_dir / "jobs" / job_id

        job = RemoveJobResponse(
            job_id=job_id,
            shot_id=metadata.shot_id,
            status=JobStatus.PENDING,
            progress=0.0,
            result_sequence_pattern=(
                ""
                if request.mode == RemoveMode.SAM2_TRACK
                else (result_dir / f"{metadata.shot_id}_result.%04d.png").as_posix()
            ),
            result_mask_pattern=(mask_dir / f"{metadata.shot_id}_mask.%04d.png").as_posix(),
        )
        self._jobs[job_id] = job
        self._executor.submit(self._run_job, job_id, request, metadata)
        return job

    def _uses_model_sequence(self, request: RemoveJobRequest, metadata: ShotMetadata) -> bool:
        return request.mode == RemoveMode.SAM2_TRACK or (
            request.mode == RemoveMode.TEMPORAL_PROPAINTER
            and (
                not metadata.is_demo
                or self.config.propainter_dir is not None
                or bool(request.mask_keyframes)
            )
        )

    def _validate_model_job(self, request: RemoveJobRequest, metadata: ShotMetadata) -> None:
        point_job = request.mode == RemoveMode.SAM2_TRACK and bool(request.sam2_points)
        if (
            not metadata.first_frame
            <= request.first_frame
            <= request.last_frame
            <= metadata.last_frame
        ):
            raise ValueError("Submit frames within the prepared shot range")
        if request.first_frame == request.last_frame and not point_job:
            raise ValueError("Submit at least two frames within the prepared shot range")
        if request.sam2_points and not point_job:
            raise ValueError("Point prompts require a sam2_track job")
        if point_job:
            self._validate_point_prompts(request)
        elif request.mode == RemoveMode.SAM2_TRACK and not request.mask_keyframes:
            raise ValueError("Provide a mask keyframe or point prompt for sam2_track")
        if any(
            not request.first_frame <= frame <= request.last_frame
            for frame in request.mask_keyframes
        ):
            raise ValueError("SAM 2 mask keyframes must be inside the requested job range")
        if not 0 <= request.mask_expand_px <= 64 or not 0 <= request.mask_feather_px <= 64:
            raise ValueError("Mask expand and feather must be between 0 and 64 pixels")
        validate_runtime(
            self.config,
            use_sam2=bool(request.mask_keyframes) or point_job,
            use_propainter=request.mode == RemoveMode.TEMPORAL_PROPAINTER,
        )

    @staticmethod
    def _validate_point_prompts(request: RemoveJobRequest) -> None:
        if request.first_frame != request.last_frame or request.mask_keyframes:
            raise ValueError("Run point prompts on one frame, then save the mask as a keyframe")
        points = request.sam2_points
        valid = all(
            0 <= p.get("x", -1) <= 1 and 0 <= p.get("y", -1) <= 1 and p.get("label") in (0, 1)
            for p in points
        )
        if len(points) > 64 or not valid or not any(p["label"] == 1 for p in points):
            raise ValueError("Provide 1–64 normalized SAM 2 points, including a positive point")

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
        job.stage = "preparing"
        job.status = JobStatus.RUNNING
        self.logger.info("Starting job %s for shot %s (%s)", job_id, metadata.shot_id, request.mode)

        try:
            model_sequence = self._uses_model_sequence(request, metadata)
            if model_sequence or request.mode in (
                RemoveMode.SINGLE_FRAME_INPAINT,
                RemoveMode.TRACKED_CLEAN_PLATE,
            ):
                if request.mode == RemoveMode.SINGLE_FRAME_INPAINT:
                    self._run_single_frame(job, request, metadata)
                elif model_sequence:
                    self._run_model_sequence(job, request, metadata)
                else:
                    self._run_sequence(job, request, metadata)
                job.stage = "completed"
                job.progress = 1.0
                job.status = JobStatus.COMPLETED
                self.logger.info("Completed removal job %s", job_id)
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

            job.stage = "completed"
            job.progress = 1.0
            job.status = JobStatus.COMPLETED
            self.logger.info("Completed job %s for shot %s", job_id, metadata.shot_id)
        except Exception as exc:
            job.error_message = str(exc)
            job.status = JobStatus.FAILED
            self.logger.exception("Job %s failed: %s", job_id, exc)

    def _run_model_sequence(
        self, job: RemoveJobResponse, request: RemoveJobRequest, metadata: ShotMetadata
    ) -> None:
        frames = range(request.first_frame, request.last_frame + 1)
        size = (metadata.width, metadata.height)
        directory = Path(job.result_mask_pattern % request.first_frame).resolve().parent
        staged_frames = directory / "frames"
        staged_masks = directory / "engine_masks"
        seeds = directory / "sam2_seeds"
        for folder in (staged_frames, staged_masks, seeds):
            folder.mkdir(parents=True, exist_ok=True)
        keyframes = set(request.mask_keyframes)
        tracking_only = request.mode == RemoveMode.SAM2_TRACK
        for index, frame in enumerate(frames):
            job.current_frame = frame
            source = self.frame_path(metadata.shot_id, "input", frame)
            with Image.open(source) as plate:
                if plate.size != size:
                    raise ValueError(f"Invalid dimensions for input frame {frame}")
                plate.convert("RGB").save(staged_frames / f"{index:06d}.png")
            if not request.sam2_points and (not keyframes or frame in keyframes):
                pathname = self.frame_path(metadata.shot_id, "mask", frame)
                if not pathname.is_file():
                    raise FileNotFoundError(f"Missing mask for frame {frame}: {pathname.name}")
                target = seeds if keyframes else staged_masks
                load_luminance_mask(pathname, size).save(target / f"{index:06d}.png")
            job.progress = 0.20 * (index + 1) / len(frames)

        raw_masks = staged_masks
        if keyframes or request.sam2_points:
            job.stage = "tracking_sam2"
            job.current_frame = None
            raw_masks = directory / "sam2_masks"
            if request.sam2_points:
                points = directory / "sam2_points.json"
                points.write_text(json.dumps(request.sam2_points), encoding="utf-8")
                run_sam2(self.config, staged_frames, seeds, raw_masks, points=points)
            else:
                run_sam2(self.config, staged_frames, seeds, raw_masks)

        job.stage = "preparing_masks"
        for index, frame in enumerate(frames):
            job.current_frame = frame
            raw = raw_masks / f"{index:06d}.png"
            if not raw.is_file():
                raise FileNotFoundError(f"SAM 2 did not produce a mask for frame {frame}")
            # Keep authored corrections, including soft edges, as the source for those frames.
            source = seeds / raw.name if frame in keyframes else raw
            matte = load_luminance_mask(source, size)
            if not tracking_only:
                matte = prepare_mask(matte, request.mask_expand_px, request.mask_feather_px)
                model_mask(matte).save(staged_masks / raw.name)
            matte.save(job.result_mask_pattern % frame)
        if tracking_only:
            return

        job.stage = "inpainting_propainter"
        job.progress = 0.50
        job.current_frame = None
        generated, expected_size = run_propainter(
            self.config, staged_frames, staged_masks, size, metadata.fps
        )
        job.stage = "writing_frames"
        for index, frame in enumerate(frames):
            job.current_frame = frame
            with (
                Image.open(staged_frames / f"{index:06d}.png") as plate,
                Image.open(generated / f"{index:04d}.png") as out,
            ):
                if out.size != expected_size:
                    raise ValueError(f"Unexpected ProPainter patch dimensions at frame {frame}")
                matte = load_luminance_mask(Path(job.result_mask_pattern % frame), size)
                restore_patch(plate.convert("RGB"), out.convert("RGB"), matte).save(
                    job.result_sequence_pattern % frame
                )
            job.progress = 0.85 + 0.14 * (index + 1) / len(frames)
        self._write_job_manifest(job, request, metadata, "", request.first_frame)

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

        job.current_frame = frame
        result_image, prompt_id = self._inpaint_patch(plate, mask, job, request, result.parent)
        result_image.save(result)
        self._write_job_manifest(job, request, metadata, prompt_id, frame)

    def _run_sequence(
        self, job: RemoveJobResponse, request: RemoveJobRequest, metadata: ShotMetadata
    ) -> None:
        try:
            from src.core.sequence import (
                BackgroundTracker,
                CleanPlateBuilder,
                merge_generated,
                read_mask,
                read_plate,
                track_patch,
            )
        except ModuleNotFoundError as exc:
            if exc.name not in {"cv2", "numpy"}:
                raise
            raise RuntimeError("Install opencv-python-headless in the Core environment") from exc

        size = (metadata.width, metadata.height)
        frames = range(request.first_frame, request.last_frame + 1)
        anchor_frame = request.anchor_frame
        if anchor_frame is None:
            anchor_frame = (request.first_frame + request.last_frame) // 2
        directory = Path(job.result_sequence_pattern % anchor_frame).parent
        directory.mkdir(parents=True, exist_ok=True)

        def load_mask(frame: int) -> NDArray:
            mask = self.frame_path(metadata.shot_id, "mask", frame)
            if not mask.is_file():
                raise FileNotFoundError(f"Missing mask for frame {frame}: {mask.name}")
            return read_mask(mask, size)

        job.current_frame = anchor_frame
        anchor = read_plate(self.frame_path(metadata.shot_id, "input", anchor_frame), size)
        tracker = BackgroundTracker(anchor, load_mask(anchor_frame))
        builder = None if request.reference_image_path else CleanPlateBuilder(size)
        transforms = {}
        job.stage = "tracking_background"
        for index, frame in enumerate(frames):
            job.current_frame = frame
            plate = read_plate(self.frame_path(metadata.shot_id, "input", frame), size)
            mask = load_mask(frame)
            Image.fromarray(mask).save(job.result_mask_pattern % frame)
            warp = (
                tracker.identity() if frame == anchor_frame else tracker.align(plate, mask, frame)
            )
            transforms[frame] = warp
            if builder is not None:
                builder.add(plate, mask, warp, frame - anchor_frame)
            job.progress = 0.05 + 0.35 * (index + 1) / len(frames)

        prompt_id = ""
        if builder is None:
            clean = read_plate(Path(request.reference_image_path), size)
        else:
            clean, missing, known = builder.build()
            if missing.getbbox():
                job.current_frame = anchor_frame
                generated, prompt_id = self._inpaint_patch(clean, missing, job, request, directory)
                clean = merge_generated(clean, generated, missing, known)
        clean.save(directory / "clean_anchor.png")
        tracking = {frame: warp.tolist() for frame, warp in transforms.items()}
        (directory / "tracking.json").write_text(json.dumps(tracking, indent=2), encoding="utf-8")

        job.stage = "writing_frames"
        for index, frame in enumerate(frames):
            job.current_frame = frame
            plate = read_plate(self.frame_path(metadata.shot_id, "input", frame), size)
            snapshot = read_mask(Path(job.result_mask_pattern % frame), size)
            track_patch(plate, clean, snapshot, transforms[frame]).save(
                job.result_sequence_pattern % frame
            )
            job.progress = 0.65 + 0.34 * (index + 1) / len(frames)
        self._write_job_manifest(job, request, metadata, prompt_id, anchor_frame)

    def _inpaint_patch(
        self,
        plate: Image.Image,
        mask: Image.Image,
        job: RemoveJobResponse,
        request: RemoveJobRequest,
        directory: Path,
    ) -> tuple[Image.Image, str]:
        bounds = mask.getbbox()
        if bounds is None:
            raise ValueError("The inpaint mask is empty")

        left, top, right, bottom = bounds
        size = plate.size
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
        input_crop = directory / "input_crop.png"
        mask_crop = directory / "mask_crop.png"
        ImageOps.expand(ai_input, padding).save(input_crop)
        ImageOps.expand(ai_mask, padding).save(mask_crop)

        subfolder = f"comfyremove/{job.job_id}"
        job.stage = "submitting_to_comfyui"
        workflow = self.comfy_client.single_frame_workflow(
            self.comfy_client.upload_image(input_crop, subfolder),
            self.comfy_client.upload_image(mask_crop, subfolder),
            self.inpaint_checkpoint,
            request.prompt,
            request.seed,
            request.negative_prompt,
        )
        workflow_path = directory / "workflow.json"
        workflow_path.write_text(json.dumps(workflow, indent=2), encoding="utf-8")
        prompt_id = self.comfy_client.queue_prompt(workflow)
        self.logger.info("ComfyUI prompt %s for job %s", prompt_id, job.job_id)
        job.stage = "waiting_for_comfyui"
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
        result_image = plate.copy()
        result_image.paste(patch_crop, crop_box[:2])
        return result_image, prompt_id

    def _write_job_manifest(
        self,
        job: RemoveJobResponse,
        request: RemoveJobRequest,
        metadata: ShotMetadata,
        prompt_id: str,
        anchor_frame: int,
    ) -> None:
        manifest = {
            "job_id": job.job_id,
            "comfy_prompt_id": prompt_id,
            "processing_mode": request.mode.value,
            "anchor_frame": anchor_frame,
            "mask_keyframes": sorted(set(request.mask_keyframes)),
            "shot_id": metadata.shot_id,
            "node_name": metadata.node_name,
            "file_pattern": metadata.file_pattern,
            "first_frame": request.first_frame,
            "last_frame": request.last_frame,
            "width": metadata.width,
            "height": metadata.height,
            "color_management": metadata.color_management.value,
            "ocio_config_path": metadata.ocio_config_path,
            "source_colorspace": metadata.source_colorspace,
            "working_colorspace": metadata.working_colorspace,
            "ai_prep_colorspace": metadata.ai_prep_colorspace,
            "result_sequence_pattern": job.result_sequence_pattern,
            "result_mask_pattern": job.result_mask_pattern,
        }
        manifest_path = (
            Path(job.result_sequence_pattern % anchor_frame).parent / "apply_manifest.json"
        )
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
            raise ValueError("Use apply_manifest_path from the completed job")
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
