"""ComfyUI HTTP API client and workflow builder."""

from __future__ import annotations

from pathlib import Path
from time import monotonic, sleep
from typing import Any

import httpx

from src.contracts.models import RemoveJobRequest, RemoveMode, ShotMetadata


class ComfyUIClient:
    """Thin async/sync client for ComfyUI REST API."""

    def __init__(self, base_url: str = "http://127.0.0.1:8188", timeout: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def get_system_stats(self) -> dict[str, Any]:
        """Fetch ComfyUI /system_stats."""
        resp = httpx.get(f"{self.base_url}/system_stats", timeout=self.timeout)
        resp.raise_for_status()
        return dict(resp.json())

    def get_object_info_keys(self) -> list[str]:
        """Fetch available node class names from ComfyUI /object_info."""
        resp = httpx.get(f"{self.base_url}/object_info", timeout=max(self.timeout, 10.0))
        resp.raise_for_status()
        return list(resp.json().keys())

    @staticmethod
    def detect_capabilities(node_keys: list[str]) -> dict[str, bool]:
        """Inspect ComfyUI node keys to report supported AI capabilities."""
        lowered = {k.lower() for k in node_keys}
        return {
            "inpaint_core": "inpaintmodelconditioning" in lowered,
            "flux_kontext_ref": "fluxkontextmultireferencelatentmethod" in lowered,
            "wan_video_inpaint": "wanfuninpaintto" in "".join(lowered)
            or "wanfuninpaintto_video" in lowered
            or "wanfuninpaintto" in lowered
            or "wanfuninpainttovideo" in lowered,
            "sam2": any("sam2" in k for k in lowered),
            "propainter": any("propainter" in k for k in lowered),
        }

    def probe_capabilities(self) -> dict[str, Any]:
        """Probe ComfyUI server and return reachability + detected capabilities."""
        try:
            stats = self.get_system_stats()
            keys = self.get_object_info_keys()
            return {
                "reachable": True,
                "comfyui_version": stats.get("system", {}).get("comfyui_version", "unknown"),
                "node_count": len(keys),
                "capabilities": self.detect_capabilities(keys),
            }
        except Exception as exc:
            return {
                "reachable": False,
                "error": str(exc),
                "node_count": 0,
                "capabilities": {},
            }

    @staticmethod
    def build_workflow(
        request: RemoveJobRequest,
        metadata: ShotMetadata,
        capabilities: dict[str, bool] | None = None,
    ) -> dict[str, Any]:
        """Build a ComfyUI API prompt graph for removal/inpainting."""
        caps = capabilities or {}
        first_input = metadata.cache_sequence_pattern % request.first_frame
        mask_input = (
            request.mask_pattern % request.first_frame
            if "%04d" in request.mask_pattern
            else request.mask_pattern
        )

        workflow: dict[str, Any] = {
            "1": {
                "class_type": "LoadImage",
                "inputs": {"image": first_input},
            },
            "2": {
                "class_type": "LoadImageMask",
                "inputs": {"image": mask_input or first_input, "channel": "alpha"},
            },
        }

        if request.mode == RemoveMode.CLEAN_PLATE_REF and request.reference_image_path:
            workflow["3"] = {
                "class_type": "LoadImage",
                "inputs": {"image": request.reference_image_path},
            }
            if caps.get("flux_kontext_ref"):
                workflow["4"] = {
                    "class_type": "FluxKontextImageScale",
                    "inputs": {"image": ["3", 0]},
                }
        elif request.mode == RemoveMode.TEMPORAL_PROPAINTER and caps.get("wan_video_inpaint"):
            workflow["4"] = {
                "class_type": "WanFunInpaintToVideo",
                "inputs": {
                    "start_image": ["1", 0],
                    "length": max(1, request.last_frame - request.first_frame + 1),
                    "width": metadata.width,
                    "height": metadata.height,
                },
            }

        workflow["99"] = {
            "class_type": "SaveImage",
            "inputs": {
                "filename_prefix": f"{metadata.shot_id}_remove",
                "images": ["1", 0],
            },
        }
        return workflow

    def queue_prompt(self, workflow: dict[str, Any]) -> str:
        """Submit a workflow prompt to ComfyUI /prompt and return prompt_id."""
        resp = httpx.post(
            f"{self.base_url}/prompt",
            json={"prompt": workflow},
            timeout=self.timeout,
        )
        if not resp.is_success:
            raise RuntimeError(f"ComfyUI rejected the workflow ({resp.status_code}): {resp.text}")
        return str(resp.json()["prompt_id"])

    def get_history(self, prompt_id: str) -> dict[str, Any]:
        """Fetch execution history for a ComfyUI prompt_id."""
        resp = httpx.get(f"{self.base_url}/history/{prompt_id}", timeout=self.timeout)
        resp.raise_for_status()
        return dict(resp.json())

    def upload_image(self, path: Path, subfolder: str) -> str:
        """Upload a PNG and return its ComfyUI-relative input name."""
        with path.open("rb") as handle:
            resp = httpx.post(
                f"{self.base_url}/upload/image",
                files={"image": (path.name, handle, "image/png")},
                data={"subfolder": subfolder, "type": "input"},
                timeout=max(self.timeout, 30.0),
            )
        resp.raise_for_status()
        uploaded = resp.json()
        return f"{uploaded['subfolder']}/{uploaded['name']}".lstrip("/")

    @staticmethod
    def single_frame_workflow(
        image: str, mask: str, checkpoint: str, prompt: str, seed: int, negative_prompt: str = ""
    ) -> dict[str, Any]:
        """Create a masked latent diffusion graph for a checkpoint with CLIP and VAE."""
        return {
            "1": {"class_type": "LoadImage", "inputs": {"image": image}},
            "2": {"class_type": "LoadImageMask", "inputs": {"image": mask, "channel": "red"}},
            "3": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": checkpoint}},
            "4": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["3", 1]}},
            "5": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": negative_prompt, "clip": ["3", 1]},
            },
            "6": {
                "class_type": "VAEEncodeForInpaint",
                "inputs": {
                    "pixels": ["1", 0],
                    "vae": ["3", 2],
                    "mask": ["2", 0],
                    "grow_mask_by": 0,
                },
            },
            "7": {
                "class_type": "KSampler",
                "inputs": {
                    "model": ["3", 0],
                    "positive": ["4", 0],
                    "negative": ["5", 0],
                    "latent_image": ["6", 0],
                    "seed": seed,
                    "steps": 20,
                    "cfg": 6.0,
                    "sampler_name": "euler",
                    "scheduler": "normal",
                    "denoise": 1.0,
                },
            },
            "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["3", 2]}},
            "9": {
                "class_type": "SaveImage",
                "inputs": {
                    "filename_prefix": "comfyremove/patch",
                    "images": ["8", 0],
                },
            },
        }

    def wait_for_patch(self, prompt_id: str, timeout: float = 600.0) -> bytes:
        """Wait for the saved patch from output node 9 and download it."""
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            entry = self.get_history(prompt_id).get(prompt_id)
            if entry:
                status = entry.get("status", {})
                if status.get("status_str") == "error":
                    raise RuntimeError(f"ComfyUI job {prompt_id} failed: {status.get('messages')}")
                if status.get("completed"):
                    images = entry.get("outputs", {}).get("9", {}).get("images", [])
                    if len(images) != 1:
                        raise RuntimeError(f"Expected one ComfyUI patch for job {prompt_id}")
                    params = {key: images[0][key] for key in ("filename", "subfolder", "type")}
                    resp = httpx.get(
                        f"{self.base_url}/view", params=params, timeout=max(self.timeout, 30.0)
                    )
                    resp.raise_for_status()
                    return resp.content
            sleep(1)
        raise TimeoutError(f"Timed out waiting for ComfyUI job {prompt_id}")
