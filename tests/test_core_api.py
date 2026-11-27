"""Unit and API tests for F2: Core Worker FastAPI, ComfyUI workflow, and JobManager."""

from __future__ import annotations

import io
import json
import random
import tempfile
import time
import unittest
from dataclasses import replace
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

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

    @unittest.skipUnless(find_spec("cv2"), "Install OpenCV to exercise sequence processing")
    def test_sequence_inpaints_once_and_tracks_one_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = CoreConfig(cache_dir=root, logs_dir=root / "logs", inpaint_checkpoint="local")
            client = TestClient(create_app(cfg))
            demo = client.post("/api/shots/demo?shot_id=seq&num_frames=3").json()
            metadata = replace(
                ShotMetadata.from_json(json.dumps(demo)), width=256, height=192, is_demo=False
            )
            metadata.save(root / "seq" / "shot_metadata.json")
            background = Image.new("RGB", (256, 192), (20, 30, 45))
            draw = ImageDraw.Draw(background)
            rng = random.Random(16)
            for _ in range(260):
                x, y = rng.randrange(240), rng.randrange(176)
                value = rng.randrange(50, 220)
                draw.ellipse((x, y, x + 6, y + 6), fill=(value, value, value))
            mask_dir = root / "seq" / "masks"
            mask_dir.mkdir()
            for index, frame in enumerate(range(1001, 1004)):
                plate = Image.new("RGB", background.size)
                plate.paste(background, (index * 4, 0))
                mask = Image.new("L", background.size)
                box = (90 + index * 4, 70, 160 + index * 4, 140)
                mask.paste(255, box)
                plate.paste((240, 40, 25), box)
                mask.save(mask_dir / f"seq_mask.{frame}.png")
                plate.save(metadata.cache_sequence_pattern % frame)

            def fake_patch(_prompt_id: str) -> bytes:
                input_file = upload.call_args_list[0].args[0]
                buffer = io.BytesIO()
                with Image.open(input_file) as image:
                    Image.new("RGB", image.size, (20, 170, 80)).save(buffer, format="PNG")
                return buffer.getvalue()

            payload = {
                "shot_id": "seq",
                "mode": "tracked_clean_plate",
                "first_frame": 1001,
                "last_frame": 1003,
                "prompt": "Rebuild the wall",
            }
            with (
                patch.object(
                    ComfyUIClient, "upload_image", side_effect=["src.png", "mask.png"]
                ) as upload,
                patch.object(ComfyUIClient, "queue_prompt", return_value="one-prompt") as submit,
                patch.object(ComfyUIClient, "wait_for_patch", side_effect=fake_patch),
            ):
                response = client.post("/api/jobs", json=payload)
                self.assertEqual(response.status_code, 200)
                job = self._wait_for_job(client, response.json()["job_id"])
            self.assertEqual(job["status"], "completed", job["error_message"])
            self.assertEqual(job["current_frame"], 1003)
            self.assertEqual(submit.call_count, 1)
            path = Path(job["apply_manifest_path"])
            manifest = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["anchor_frame"], 1002)
            self.assertEqual(manifest["comfy_prompt_id"], "one-prompt")
            transforms = json.loads((path.parent / "tracking.json").read_text(encoding="utf-8"))
            self.assertAlmostEqual(transforms["1001"][0][2], 4, delta=1)
            self.assertAlmostEqual(transforms["1003"][0][2], -4, delta=1)
            patch_pixels = []
            for index, frame in enumerate(range(1001, 1004)):
                response = client.get(f"/api/jobs/{job['job_id']}/result/{frame}")
                self.assertEqual(response.status_code, 200)
                with Image.open(io.BytesIO(response.content)) as result:
                    patch_pixels.append(result.getpixel((123 + index * 4, 100)))
                    with Image.open(metadata.cache_sequence_pattern % frame) as plate:
                        self.assertEqual(result.getpixel((15, 15)), plate.getpixel((15, 15)))
            self.assertLess(max(pixel[0] for pixel in patch_pixels), 120)
            for value, anchor in zip(patch_pixels[0], patch_pixels[2], strict=True):
                self.assertAlmostEqual(value, anchor, delta=4)

            (mask_dir / "seq_mask.1003.png").unlink()
            response = client.post("/api/jobs", json=payload)
            job = self._wait_for_job(client, response.json()["job_id"])
            self.assertEqual(job["status"], "failed")
            self.assertIn("Missing mask for frame 1003", job["error_message"])
            self.assertEqual(job["current_frame"], 1003)
            self.assertEqual(job["apply_manifest_path"], "")
            self.assertEqual(client.get(f"/api/jobs/{job['job_id']}/result/1001").status_code, 404)

    @staticmethod
    def _wait_for_job(client: TestClient, job_id: str) -> dict:
        for _ in range(180):
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] in ("completed", "failed"):
                return job
            time.sleep(0.02)
        raise AssertionError(f"Job {job_id} did not finish")

    def test_temporal_job_uses_sam2_keyframes_and_writes_one_patch_per_frame(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkpoint = root / "sam2.pt"
            checkpoint.touch()
            painter = root / "propainter"
            (painter / "weights").mkdir(parents=True)
            (painter / "inference_propainter.py").touch()
            for name in ("ProPainter.pth", "raft-things.pth", "recurrent_flow_completion.pth"):
                (painter / "weights" / name).touch()
            cfg = CoreConfig(
                cache_dir=root / "cache",
                logs_dir=root / "logs",
                sam2_checkpoint=checkpoint,
                propainter_dir=painter,
            )
            client = TestClient(create_app(cfg))
            demo = client.post("/api/shots/demo?shot_id=clip&num_frames=3").json()
            metadata = replace(
                ShotMetadata.from_json(json.dumps(demo)), width=32, height=24, is_demo=False
            )
            shot_dir = cfg.cache_dir / "clip"
            metadata.save(shot_dir / "shot_metadata.json")
            for frame in range(1001, 1004):
                Image.new("RGB", (32, 24), (50, 60, 70)).save(
                    metadata.cache_sequence_pattern % frame
                )
            authored = Image.new("L", (32, 24), 0)
            authored.paste(128, (8, 6, 24, 18))
            mask_data = io.BytesIO()
            authored.save(mask_data, format="PNG")
            response = client.post(
                "/api/shots/clip/mask/1002",
                files={"file": ("mask.png", mask_data.getvalue(), "image/png")},
            )
            self.assertEqual(response.status_code, 200)

            def track(_cfg, frames, seeds, output):
                self.assertEqual([path.name for path in seeds.glob("*.png")], ["000001.png"])
                output.mkdir()
                for index, _ in enumerate(sorted(frames.glob("*.png"))):
                    authored.point(lambda value: 255 if value else 0).save(
                        output / f"{index:06d}.png"
                    )

            def inpaint(_cfg, frames, masks, size, _fps):
                self.assertEqual(len(list(masks.glob("*.png"))), 3)
                output = frames.parent / "fake_propainter"
                output.mkdir()
                for index in range(3):
                    Image.new("RGB", size, (100, 110, 120)).save(output / f"{index:04d}.png")
                return output, size

            payload = {
                "shot_id": "clip",
                "mode": "temporal_propainter",
                "first_frame": 1001,
                "last_frame": 1003,
                "mask_keyframes": [1002],
                "mask_expand_px": 0,
                "mask_feather_px": 0,
            }
            with (
                patch("src.core.job_manager.run_sam2", side_effect=track) as sampler,
                patch("src.core.job_manager.run_propainter", side_effect=inpaint) as painter_run,
            ):
                response = client.post("/api/jobs", json=payload)
                self.assertEqual(response.status_code, 200, response.text)
                job = self._wait_for_job(client, response.json()["job_id"])
                self.assertEqual(job["status"], "completed", job["error_message"])
                sampler.assert_called_once()
                painter_run.assert_called_once()
            jid = job["job_id"]
            for frame in range(1001, 1004):
                response = client.get(f"/api/jobs/{jid}/result/{frame}")
                self.assertEqual(response.status_code, 200)
                with Image.open(io.BytesIO(response.content)) as patch_image:
                    self.assertEqual(patch_image.getpixel((0, 0)), (50, 60, 70))
                    self.assertEqual(patch_image.getpixel((10, 10)), (100, 110, 120))
            mask = client.get(f"/api/jobs/{jid}/mask/1002")
            self.assertEqual(mask.status_code, 200)
            with Image.open(io.BytesIO(mask.content)) as authored_snapshot:
                self.assertEqual(authored_snapshot.getpixel((10, 10)), 128)
            manifest = json.loads(Path(job["apply_manifest_path"]).read_text(encoding="utf-8"))
            self.assertEqual(manifest["mask_keyframes"], [1002])
            self.assertEqual(manifest["processing_mode"], "temporal_propainter")


if __name__ == "__main__":
    unittest.main()
