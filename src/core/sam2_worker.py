"""Standalone SAM 2 video inference entry point for the configured model Python."""

from __future__ import annotations

import argparse
import json
from bisect import bisect_right
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import torch
from PIL import Image
from sam2.build_sam import build_sam2_video_predictor


def track(
    frames: Path,
    seeds: Path,
    output: Path,
    model_config: str,
    checkpoint: str,
    points: Path | None = None,
) -> None:
    sources = sorted(frames.glob("*.png"))
    keyframes = {int(path.stem): path for path in seeds.glob("*.png")}
    indices = sorted(keyframes)
    output.mkdir(parents=True, exist_ok=True)
    if not indices and points is None:
        raise ValueError("SAM 2 requires at least one keyframe mask")

    device = "cuda" if torch.cuda.is_available() else "cpu"
    predictor = build_sam2_video_predictor(
        model_config, checkpoint, device=device, apply_postprocessing=False
    )
    with (
        TemporaryDirectory(dir=output.parent) as video,
        torch.inference_mode(),
        torch.autocast(device, dtype=torch.bfloat16, enabled=device == "cuda"),
    ):
        for index, source in enumerate(sources):
            with Image.open(source) as image:
                image.convert("RGB").save(Path(video) / f"{index:06d}.jpg", quality=97)
        state = predictor.init_state(video, offload_video_to_cpu=True)
        if points is not None:
            with Image.open(sources[0]) as frame:
                width, height = frame.size
            prompts = json.loads(points.read_text(encoding="utf-8"))
            coords = np.float32(
                [[point["x"] * (width - 1), point["y"] * (height - 1)] for point in prompts]
            )
            labels = np.int32([point["label"] for point in prompts])
            _, _, logits = predictor.add_new_points_or_box(
                state, frame_idx=0, obj_id=1, points=coords, labels=labels
            )
            mask = (logits[0, 0] > 0).cpu().numpy().astype(np.uint8) * 255
            Image.fromarray(mask).save(output / "000000.png")
            return
        for index in indices:
            with Image.open(keyframes[index]) as image:
                mask = np.array(image.convert("L")) >= 128
            if not mask.any():
                raise ValueError(f"SAM 2 keyframe {index} has an empty mask")
            predictor.add_new_mask(state, frame_idx=index, obj_id=1, mask=mask)
            Image.fromarray(mask.astype(np.uint8) * 255).save(output / f"{index:06d}.png")

        # Each unprompted frame uses the propagation from the nearest authored correction.
        for reverse, start in ((False, indices[0]), (True, indices[-1])):
            for index, _, logits in predictor.propagate_in_video(
                state, start_frame_idx=start, reverse=reverse
            ):
                if index in keyframes:
                    continue
                insertion = bisect_right(indices, index)
                use_reverse = insertion == 0 or (
                    insertion < len(indices)
                    and indices[insertion] - index < index - indices[insertion - 1]
                )
                if reverse == use_reverse:
                    mask = (logits[:, 0] > 0).any(dim=0).cpu().numpy().astype(np.uint8) * 255
                    Image.fromarray(mask).save(output / f"{index:06d}.png")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("frames", type=Path)
    parser.add_argument("seeds", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("model_config")
    parser.add_argument("checkpoint")
    parser.add_argument("--points", type=Path)
    args = parser.parse_args()
    track(args.frames, args.seeds, args.output, args.model_config, args.checkpoint, args.points)


if __name__ == "__main__":
    main()
