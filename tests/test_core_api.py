"""Unit and API tests for F2: Core Worker FastAPI, ComfyUI workflow, and JobManager."""

from __future__ import annotations

import io
import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

from src.contracts.models import JobStatus, RemoveJobRequest, RemoveMode, ShotMetadata
from src.core.comfy_client import ComfyUIClient
from src.core.config import CoreConfig
from src.core.server import create_app


class TestCoreAPI(unittest.TestCase):
    """Test Core Worker API endpoints and asynchronous job execution."""

    def test_demo_shot_job_and_apply_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg = CoreConfig(
                cache_dir=tmp_path / "cache",
                logs_dir=tmp_path / "logs",
                comfyui_url="http://127.0.0.1:9",
            )
            client = TestClient(create_app(cfg))

            health = client.get("/health")
            self.assertEqual(health.status_code, 200)
            self.assertEqual(health.json()["status"], "ok")

            demo = client.post("/api/shots/demo?shot_id=sh010&num_frames=4")
            self.assertEqual(demo.status_code, 200)
            self.assertEqual(demo.json()["shot_id"], "sh010")

            frame_resp = client.get("/api/shots/sh010/frames/1001")
            self.assertEqual(frame_resp.status_code, 200)
            self.assertEqual(frame_resp.headers["content-type"], "image/png")

            # Upload a mask for frame 1001
            mask_buf = io.BytesIO()
            Image.new("L", (960, 540), 255).save(mask_buf, format="PNG")
            mask_buf.seek(0)
            up = client.post(
                "/api/shots/sh010/mask/1001",
                files={"file": ("mask.png", mask_buf, "image/png")},
            )
            self.assertEqual(up.status_code, 200)

            # Submit job
            job_resp = client.post(
                "/api/jobs",
                json={
                    "shot_id": "sh010",
                    "mode": "temporal_propainter",
                    "first_frame": 1001,
                    "last_frame": 1004,
                    "sam2_points": [{"x": 0.3, "y": 0.5, "label": 1}],
                },
            )
            self.assertEqual(job_resp.status_code, 200)
            job_id = job_resp.json()["job_id"]

            # Poll until completed
            final_status = ""
            for _ in range(30):
                status_resp = client.get(f"/api/jobs/{job_id}")
                final_status = status_resp.json()["status"]
                if final_status in (JobStatus.COMPLETED.value, JobStatus.FAILED.value):
                    break
                time.sleep(0.05)

            self.assertEqual(final_status, JobStatus.COMPLETED.value)
            res_frame = client.get("/api/shots/sh010/result/1001")
            self.assertEqual(res_frame.status_code, 200)

            apply_resp = client.post("/api/shots/sh010/apply", json={"enable_grain": True})
            self.assertEqual(apply_resp.status_code, 200)
            self.assertEqual(apply_resp.json()["source_colorspace"], "ACES - ACEScg")

    def test_comfy_capability_detection_and_workflow(self) -> None:
        caps = ComfyUIClient.detect_capabilities(
            ["LoadImage", "InpaintModelConditioning", "FluxKontextMultiReferenceLatentMethod"]
        )
        self.assertTrue(caps["inpaint_core"])
        self.assertTrue(caps["flux_kontext_ref"])
        self.assertFalse(caps["sam2"])

        with tempfile.TemporaryDirectory() as tmp:
            cfg = CoreConfig(cache_dir=Path(tmp) / "cache", logs_dir=Path(tmp) / "logs")
            client = TestClient(create_app(cfg))
            meta_dict = client.post("/api/shots/demo?shot_id=wf01&num_frames=2").json()
            meta = ShotMetadata.from_json(json.dumps(meta_dict))
            wf = ComfyUIClient.build_workflow(
                RemoveJobRequest(
                    shot_id="wf01",
                    mode=RemoveMode.CLEAN_PLATE_REF,
                    first_frame=1001,
                    last_frame=1002,
                    reference_image_path="ref.png",
                ),
                meta,
                caps,
            )
            self.assertEqual(wf["3"]["class_type"], "LoadImage")
            self.assertEqual(wf["4"]["class_type"], "FluxKontextImageScale")

    def test_single_frame_uses_comfy_output_and_snapshots_the_mask(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = CoreConfig(
                cache_dir=root, logs_dir=root / "logs", inpaint_checkpoint="local.ckpt"
            )
            client = TestClient(create_app(cfg))
            demo = client.post("/api/shots/demo?shot_id=plate&num_frames=1").json()
            metadata = replace(
                ShotMetadata.from_json(json.dumps(demo)), width=45, height=35, is_demo=False
            )
            metadata.save(root / "plate" / "shot_metadata.json")
            Image.new("RGB", (45, 35), (31, 49, 77)).save(metadata.cache_sequence_pattern % 1001)
            mask_dir = root / "plate" / "masks"
            mask_dir.mkdir()
            mask = Image.new("L", (45, 35), 0)
            mask.paste(255, (15, 11, 25, 20))
            mask.save(mask_dir / "plate_mask.1001.png")
            patch_image = io.BytesIO()
            Image.new("RGB", (48, 40), (10, 177, 80)).save(patch_image, format="PNG")

            with (
                patch.object(
                    ComfyUIClient, "upload_image", side_effect=["src.png", "mask.png"]
                ) as up,
                patch.object(ComfyUIClient, "queue_prompt", return_value="test-prompt") as submit,
                patch.object(ComfyUIClient, "wait_for_patch", return_value=patch_image.getvalue()),
            ):
                response = client.post(
                    "/api/jobs",
                    json={
                        "shot_id": "plate",
                        "mode": "single_frame_inpaint",
                        "first_frame": 1001,
                        "last_frame": 1001,
                        "prompt": "Empty room",
                    },
                )
                self.assertEqual(response.status_code, 200)
                job_id = response.json()["job_id"]
                for _ in range(100):
                    job = client.get(f"/api/jobs/{job_id}").json()
                    if job["status"] in ("completed", "failed"):
                        break
                    time.sleep(0.01)
                self.assertEqual(job["status"], "completed", job["error_message"])

            manifest = json.loads(Path(job["apply_manifest_path"]).read_text(encoding="utf-8"))
            self.assertEqual(manifest["comfy_prompt_id"], "test-prompt")
            self.assertEqual(up.call_count, 2)
            self.assertEqual(submit.call_args.args[0]["9"]["inputs"]["images"], ["8", 0])
            self.assertEqual(client.get(f"/api/jobs/{job_id}/result/1002").status_code, 404)
            response = client.get(f"/api/jobs/{job_id}/result/1001")
            self.assertEqual(response.status_code, 200)
            with Image.open(io.BytesIO(response.content)) as result:
                self.assertEqual(result.size, (45, 35))
                self.assertEqual(result.getpixel((20, 15)), (10, 177, 80))
            with Image.open(manifest["result_mask_pattern"] % 1001) as snapshot:
                self.assertEqual(snapshot.tobytes(), mask.tobytes())


if __name__ == "__main__":
    unittest.main()
