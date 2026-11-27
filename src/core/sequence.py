"""Track a clean plate in the AI prep colorspace across a masked shot range."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
from numpy.typing import NDArray
from PIL import Image


def read_plate(path: Path, size: tuple[int, int]) -> Image.Image:
    with Image.open(path) as image:
        if image.size != size:
            raise ValueError(f"Expected {size[0]}x{size[1]} image at {path.name}")
        return image.convert("RGB")


def read_mask(path: Path, size: tuple[int, int]) -> NDArray[np.uint8]:
    encoded = np.frombuffer(path.read_bytes(), dtype=np.uint8)
    mask = cv2.imdecode(encoded, cv2.IMREAD_UNCHANGED)
    if mask is None or mask.shape[:2] != size[::-1]:
        raise ValueError(f"Expected {size[0]}x{size[1]} mask at {path.name}")
    if mask.ndim == 3:
        mask = mask[:, :, 2]
    if mask.dtype == np.uint16:
        return np.rint(mask / 257).astype(np.uint8)
    return mask.astype(np.uint8)


def background_mask(mask: NDArray[np.uint8]) -> NDArray[np.uint8]:
    excluded = cv2.dilate((mask > 0).astype(np.uint8), np.ones((15, 15), np.uint8))
    return (1 - excluded) * 255


class BackgroundTracker:
    """Estimate a global similarity transform using features outside the person matte."""

    def __init__(self, plate: Image.Image, mask: NDArray[np.uint8]):
        self.detector = cv2.SIFT_create(nfeatures=3500, contrastThreshold=0.012)
        self.matcher = cv2.BFMatcher()
        self.contrast = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        self.points, self.features = self._features(plate, mask)

    def _features(self, plate: Image.Image, mask: NDArray[np.uint8]):
        gray = cv2.cvtColor(np.asarray(plate), cv2.COLOR_RGB2GRAY)
        return self.detector.detectAndCompute(self.contrast.apply(gray), background_mask(mask))

    @staticmethod
    def identity() -> NDArray[np.float64]:
        return np.eye(2, 3)

    def align(self, plate: Image.Image, mask: NDArray[np.uint8], frame: int) -> NDArray[np.float64]:
        points, features = self._features(plate, mask)
        if features is None or self.features is None or len(self.features) < 2:
            raise ValueError(f"Could not track background at frame {frame}: too few features")
        matches = [
            pair[0]
            for pair in self.matcher.knnMatch(features, self.features, k=2)
            if len(pair) == 2 and pair[0].distance < 0.70 * pair[1].distance
        ]
        if len(matches) < 8:
            raise ValueError(f"Could not track background at frame {frame}: too few matches")
        source = np.float32([points[match.queryIdx].pt for match in matches])
        target = np.float32([self.points[match.trainIdx].pt for match in matches])
        warp, inliers = cv2.estimateAffinePartial2D(
            source, target, ransacReprojThreshold=3.0, maxIters=10000
        )
        if warp is None or inliers is None or inliers.sum() < max(8, 0.30 * len(matches)):
            raise ValueError(f"Could not track background at frame {frame}: unstable transform")
        return warp


class CleanPlateBuilder:
    """Accumulate visible background in anchor-frame coordinates using bounded memory."""

    def __init__(self, size: tuple[int, int]):
        self.size = size
        self.weights = np.zeros(size[::-1], dtype=np.float32)
        self.pixels = np.zeros((*size[::-1], 3), dtype=np.float32)
        self.target = np.zeros(size[::-1], dtype=np.uint8)

    def add(self, plate: Image.Image, mask: NDArray[np.uint8], warp: NDArray, offset: int) -> None:
        valid = cv2.warpAffine(background_mask(mask), warp, self.size, flags=cv2.INTER_NEAREST)
        aligned_matte = cv2.warpAffine(mask, warp, self.size, borderMode=cv2.BORDER_REFLECT_101)
        np.maximum(self.target, aligned_matte, out=self.target)
        distance = cv2.distanceTransform(valid, cv2.DIST_L2, 3)
        weight = np.minimum(1.0, distance / 30.0) / (1 + abs(offset))
        if offset == 0:
            weight *= 5
        aligned = cv2.warpAffine(np.asarray(plate), warp, self.size)
        self.pixels += aligned * weight[:, :, None]
        self.weights += weight

    def build(self) -> tuple[Image.Image, Image.Image, NDArray[np.bool_]]:
        weight = self.weights[:, :, None]
        pixels = np.divide(self.pixels, weight, out=self.pixels, where=weight > 0)
        needed = cv2.dilate(self.target, np.ones((11, 11), np.uint8)) > 0
        known = self.weights > 0
        missing = (~known & needed).astype(np.uint8) * 255
        blended = Image.fromarray(np.rint(pixels).astype(np.uint8))
        return blended, Image.fromarray(missing), known


def merge_generated(
    plate: Image.Image, patch: Image.Image, mask: Image.Image, known: NDArray[np.bool_]
) -> Image.Image:
    """Keep generated detail while guiding its broad tone by the observed background."""
    observed = np.asarray(plate)
    scale = min(1.0, 256 / max(plate.size))
    guide_size = tuple(max(1, round(edge * scale)) for edge in plate.size)
    sample = cv2.resize(observed, guide_size, interpolation=cv2.INTER_AREA)
    coverage = cv2.resize(known.astype(np.float32), guide_size, interpolation=cv2.INTER_AREA)
    missing = (coverage < 0.98).astype(np.uint8)
    if not np.any(missing == 0):
        return Image.composite(patch, plate, mask)
    guide = cv2.inpaint(sample, missing, 5, cv2.INPAINT_NS)
    guide = cv2.resize(guide.astype(np.float32), plate.size, interpolation=cv2.INTER_LINEAR)
    guide = cv2.GaussianBlur(guide, (0, 0), 8)
    generated = np.asarray(patch, dtype=np.float32)
    smooth = cv2.GaussianBlur(generated, (0, 0), 14)
    detail = cv2.GaussianBlur(generated - smooth, (0, 0), 1.4)
    matched = guide + 0.6 * np.clip(smooth - guide, -22, 26) + 0.7 * np.clip(detail, -23, 23)
    return Image.composite(Image.fromarray(matched.clip(0, 255).astype(np.uint8)), plate, mask)


def track_patch(
    plate: Image.Image, anchor: Image.Image, mask: NDArray, warp: NDArray
) -> Image.Image:
    aligned = cv2.warpAffine(
        np.asarray(anchor),
        cv2.invertAffineTransform(warp),
        plate.size,
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REFLECT_101,
    )
    # Keep plate pixels at zero coverage. Nuke applies the textured patch using the saved matte.
    return Image.fromarray(np.where(mask[:, :, None] > 0, aligned, np.asarray(plate)))
