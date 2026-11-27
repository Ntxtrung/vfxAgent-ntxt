"""FastAPI server for the Core Worker process (separate from UI process)."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from shutil import copyfileobj
from tempfile import NamedTemporaryFile
from typing import Annotated, Any
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from src.contracts.models import JobStatus, RemoveJobRequest, RemoveMode
from src.core.comfy_client import ComfyUIClient
from src.core.config import CoreConfig, configure_core_logger
from src.core.job_manager import JobManager


class JobCreatePayload(BaseModel):
    """HTTP request body for creating a removal/inpaint job."""

    shot_id: str
    mode: RemoveMode = RemoveMode.TEMPORAL_PROPAINTER
    first_frame: int
    last_frame: int
    prompt: str = ""
    reference_image_path: str = ""
    mask_pattern: str = ""
    mask_expand_px: int = 4
    mask_feather_px: int = 6
    seed: int = 42
    sam2_points: list[dict[str, float | int]] = Field(default_factory=list)
    negative_prompt: str = ""
    anchor_frame: int | None = None
    mask_keyframes: list[int] = Field(default_factory=list)


class ApplyPayload(BaseModel):
    """HTTP request body for writing Nuke F4 auto-composite manifest."""

    enable_grain: bool = True


def save_upload(file: UploadFile, path: Path) -> None:
    """Publish the complete upload in one rename so readers see a whole file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as handle:
        temporary = Path(handle.name)
    try:
        with temporary.open("wb") as handle:
            copyfileobj(file.file, handle)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def create_app(config: CoreConfig | None = None) -> FastAPI:
    """Create and configure the Core Worker FastAPI application."""
    cfg = config or CoreConfig.from_env()
    logger = configure_core_logger(cfg.logs_dir)
    comfy = ComfyUIClient(base_url=cfg.comfyui_url)
    manager = JobManager(cfg, comfy, logger)

    app = FastAPI(title="VFX Remove/Inpaint Core Worker", version="0.2.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            f"http://{cfg.ui_host}:{cfg.ui_port}",
            f"http://localhost:{cfg.ui_port}",
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "cache_dir": cfg.cache_dir.as_posix()}

    @app.get("/api/comfy/capabilities")
    def comfy_capabilities() -> dict[str, Any]:
        return comfy.probe_capabilities()

    @app.get("/api/shots")
    def list_shots() -> list[dict[str, Any]]:
        return [asdict(s) for s in manager.list_shots()]

    @app.post("/api/shots/demo")
    def create_demo_shot(shot_id: str = "demo_shot", num_frames: int = 8) -> dict[str, Any]:
        meta = manager.create_demo_shot(shot_id=shot_id, num_frames=num_frames)
        return asdict(meta)

    @app.get("/api/shots/{shot_id}")
    def get_shot(shot_id: str) -> dict[str, Any]:
        try:
            meta = manager.get_shot(shot_id)
            return asdict(meta)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/shots/{shot_id}/frames/{frame}")
    def get_input_frame(shot_id: str, frame: int) -> FileResponse:
        path = manager.frame_path(shot_id, "input", frame)
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"Input frame {frame} not found")
        return FileResponse(path, media_type="image/png")

    @app.get("/api/shots/{shot_id}/result/{frame}")
    def get_result_frame(shot_id: str, frame: int) -> FileResponse:
        path = manager.frame_path(shot_id, "result", frame)
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"Result frame {frame} not found")
        return FileResponse(path, media_type="image/png")

    def get_job_file(job_id: str, frame: int, *, mask: bool = False) -> FileResponse:
        try:
            job = manager.get_job(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        pattern = job.result_mask_pattern if mask else job.result_sequence_pattern
        if not pattern:
            raise HTTPException(status_code=404, detail=f"Frame {frame} not available")
        path = Path(pattern % frame)
        if job.status != JobStatus.COMPLETED or not path.is_file():
            raise HTTPException(status_code=404, detail=f"Result frame {frame} not available")
        return FileResponse(path, media_type="image/png")

    @app.get("/api/jobs/{job_id}/result/{frame}")
    def get_job_result(job_id: str, frame: int) -> FileResponse:
        return get_job_file(job_id, frame)

    @app.get("/api/jobs/{job_id}/mask/{frame}")
    def get_job_mask(job_id: str, frame: int) -> FileResponse:
        return get_job_file(job_id, frame, mask=True)

    @app.post("/api/shots/{shot_id}/mask/{frame}")
    def upload_mask(
        shot_id: str,
        frame: int,
        file: Annotated[UploadFile, File()],
    ) -> dict[str, str]:
        path = manager.frame_path(shot_id, "mask", frame)
        save_upload(file, path)
        return {"saved": path.as_posix()}

    @app.get("/api/shots/{shot_id}/mask/{frame}")
    def get_shot_mask(shot_id: str, frame: int) -> FileResponse:
        path = manager.frame_path(shot_id, "mask", frame)
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"Mask for frame {frame} not found")
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.post("/api/shots/{shot_id}/reference")
    def upload_reference(
        shot_id: str,
        file: Annotated[UploadFile, File()],
    ) -> dict[str, str]:
        suffix = Path(file.filename or "reference.png").suffix
        ref_path = cfg.cache_dir / shot_id / "ref" / f"{uuid4().hex}{suffix}"
        save_upload(file, ref_path)
        return {"reference_image_path": ref_path.as_posix()}

    @app.post("/api/jobs")
    def submit_job(payload: JobCreatePayload) -> dict[str, Any]:
        try:
            req = RemoveJobRequest(**payload.model_dump())
            job = manager.submit_job(req)
            return asdict(job)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, Any]:
        try:
            return asdict(manager.get_job(job_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/jobs/{job_id}/manifest")
    def download_manifest(job_id: str) -> FileResponse:
        try:
            job = manager.get_job(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        path = Path(job.apply_manifest_path)
        if job.status != JobStatus.COMPLETED or not path.is_file():
            raise HTTPException(status_code=404, detail="Apply manifest is not available")
        return FileResponse(path, media_type="application/json", filename=f"apply_{job_id}.json")

    @app.post("/api/shots/{shot_id}/apply")
    def apply_to_nuke(shot_id: str, payload: ApplyPayload) -> dict[str, Any]:
        try:
            return manager.write_apply_manifest(shot_id, enable_grain=payload.enable_grain)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return app


app = create_app()
