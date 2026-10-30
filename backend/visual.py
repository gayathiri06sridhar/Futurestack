import io
import math
import re
import warnings
from collections import Counter
from typing import Any

import numpy as np
from PIL import Image, ImageOps


DEFAULT_THRESHOLDS = {
    "logo_similarity": 0.72,
    "colour_delta_e": 38.0,
    "unapproved_colour_fraction": 0.55,
    "minimum_wcag_contrast": 4.5,
}


def decode_image(image_bytes: bytes) -> Image.Image:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            image = Image.open(io.BytesIO(image_bytes))
            image.verify()
            image = Image.open(io.BytesIO(image_bytes))
            image.load()
            return ImageOps.exif_transpose(image).convert("RGB")
    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError("The uploaded image is invalid or cannot be decoded.") from exc


def _rgb_from_hex(value: str) -> tuple[int, int, int]:
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise ValueError(f"Invalid RGB hex colour: {value}")
    return tuple(int(value[index : index + 2], 16) for index in (1, 3, 5))


def _linear_channel(value: int) -> float:
    channel = value / 255
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def relative_luminance(color: str | tuple[int, int, int]) -> float:
    rgb = _rgb_from_hex(color) if isinstance(color, str) else color
    red, green, blue = (_linear_channel(value) for value in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def wcag_contrast_ratio(
    foreground: str | tuple[int, int, int],
    background: str | tuple[int, int, int],
) -> float:
    first = relative_luminance(foreground)
    second = relative_luminance(background)
    lighter, darker = max(first, second), min(first, second)
    return round((lighter + 0.05) / (darker + 0.05), 2)


def _dominant_colours(image: Image.Image, count: int = 8) -> list[dict[str, Any]]:
    thumbnail = image.copy()
    thumbnail.thumbnail((160, 160))
    quantized = thumbnail.quantize(colors=count, method=Image.Quantize.FASTOCTREE)
    palette = quantized.getpalette() or []
    counts = quantized.getcolors(maxcolors=160 * 160) or []
    total = sum(size for size, _ in counts) or 1
    result = []
    for size, index in sorted(counts, reverse=True):
        offset = index * 3
        rgb = tuple(palette[offset : offset + 3])
        result.append(
            {
                "hex": "#{:02x}{:02x}{:02x}".format(*rgb),
                "fraction": round(size / total, 4),
            }
        )
    return result[:count]


def _delta_e_76(left: str, right: str) -> float:
    def to_lab(value: str) -> np.ndarray:
        red, green, blue = (
            _linear_channel(channel) for channel in _rgb_from_hex(value)
        )
        xyz = np.asarray(
            [
                0.4124564 * red + 0.3575761 * green + 0.1804375 * blue,
                0.2126729 * red + 0.7151522 * green + 0.0721750 * blue,
                0.0193339 * red + 0.1191920 * green + 0.9503041 * blue,
            ]
        )
        xyz /= np.asarray([0.95047, 1.0, 1.08883])
        delta = 6 / 29
        transformed = np.where(
            xyz > delta**3,
            np.cbrt(xyz),
            xyz / (3 * delta**2) + 4 / 29,
        )
        x, y, z = transformed
        return np.asarray([116 * y - 16, 500 * (x - y), 200 * (y - z)])

    return round(float(np.linalg.norm(to_lab(left) - to_lab(right))), 2)


def _phash(image: Image.Image) -> int:
    pixels = np.asarray(
        ImageOps.grayscale(image).resize((32, 32), Image.Resampling.LANCZOS),
        dtype=np.float64,
    )
    coordinates = np.arange(32)
    transform = np.cos(np.pi * (2 * coordinates + 1)[:, None] * coordinates / 64)
    transform[:, 0] *= 1 / math.sqrt(2)
    transform *= math.sqrt(2 / 32)
    coefficients = transform.T @ pixels @ transform
    low_frequency = coefficients[:8, :8].flatten()
    median = float(np.median(low_frequency[1:]))
    bits = low_frequency > median
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return value


def _logo_match(
    creative: Image.Image, approved_logo: Image.Image
) -> tuple[float, tuple[float, float]]:
    target_hash = _phash(approved_logo)
    width, height = creative.size
    best = 0
    best_center = (0.5, 0.5)
    target_ratio = approved_logo.width / approved_logo.height
    position_fractions = (
        0, 0.04, 0.05, 0.0625, 0.07, 0.1, 0.15, 0.2, 0.3,
        0.5, 0.7, 0.8, 0.9, 0.90625, 0.9167, 0.95, 1,
    )
    scales = (
        0.12, 0.16, 0.2, 0.24, 0.25, 0.28, 0.32, 0.36,
        0.4, 0.45, 0.52, 0.62, 0.75, 0.9, 1.0,
    )
    for scale in scales:
        crop_width = min(width, max(32, int(width * scale)))
        crop_height = min(height, max(32, int(crop_width / target_ratio)))
        if crop_height / crop_width > 1 / target_ratio:
            crop_width = min(width, max(32, int(crop_height * target_ratio)))
        if crop_width < 1 or crop_height < 1:
            continue
        max_x = width - crop_width
        max_y = height - crop_height
        for horizontal in position_fractions:
            x = round(max_x * horizontal)
            for vertical in position_fractions:
                y = round(max_y * vertical)
                candidate = creative.crop((x, y, x + crop_width, y + crop_height))
                candidate_hash = _phash(candidate)
                distance = (target_hash ^ candidate_hash).bit_count()
                similarity = 1 - distance / 64
                if similarity > best:
                    best = similarity
                    best_center = (
                        (x + crop_width / 2) / width,
                        (y + crop_height / 2) / height,
                    )
    return round(best, 4), (round(best_center[0], 4), round(best_center[1], 4))


def logo_similarity(creative: Image.Image, approved_logo: Image.Image) -> float:
    """Return max 64-bit pHash similarity across likely logo crop scales."""
    return _logo_match(creative, approved_logo)[0]


def _placement_zone(center: tuple[float, float]) -> str:
    x, y = center
    if 0.25 <= x <= 0.75 and 0.25 <= y <= 0.75:
        return "center"
    return ("top" if y < 0.5 else "bottom") + (
        "_left" if x < 0.5 else "_right"
    )


def analyze_visual_content(
    image_bytes: bytes,
    approved_colours: list[str] | None = None,
    approved_logos: list[bytes] | None = None,
    contrast_pairs: list[dict[str, str]] | None = None,
    thresholds: dict[str, float] | None = None,
    expected_logo_zone: str = "any",
) -> dict[str, Any]:
    settings = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    creative = decode_image(image_bytes)
    colours = _dominant_colours(creative)
    palette = [colour for colour in approved_colours or []]

    color_check: dict[str, Any] = {
        "status": "not_configured" if not palette else "measured",
        "dominant_colours": colours,
        "approved_palette": palette,
        "out_of_palette_fraction": None,
        "findings": [],
    }
    if palette:
        unapproved_fraction = 0.0
        for dominant in colours:
            nearest = min(
                _delta_e_76(dominant["hex"], approved)
                for approved in palette
            )
            dominant["nearest_approved_delta_e_76"] = nearest
            dominant["palette_match"] = (
                nearest <= settings["colour_delta_e"]
            )
            if not dominant["palette_match"]:
                unapproved_fraction += dominant["fraction"]
        color_check["out_of_palette_fraction"] = round(unapproved_fraction, 4)
        if unapproved_fraction > settings["unapproved_colour_fraction"]:
            color_check["findings"].append(
                {
                    "type": "visual_inconsistency",
                    "status": "objective_measurement",
                    "reason": (
                        f"{unapproved_fraction:.1%} of dominant image pixels are "
                        "outside the configured brand palette threshold."
                    ),
                }
            )

    logos = [decode_image(logo) for logo in (approved_logos or [])]
    logo_check: dict[str, Any] = {
        "status": "not_configured" if not logos else "measured",
        "method": "maximum 64-bit perceptual-hash similarity across a fixed crop grid",
        "threshold": settings["logo_similarity"],
        "similarity": None,
        "detected_center": None,
        "detected_zone": None,
        "expected_zone": expected_logo_zone,
        "findings": [],
    }
    if logos:
        matches = [_logo_match(creative, logo) for logo in logos]
        similarity, detected_center = max(matches, key=lambda match: match[0])
        logo_check["similarity"] = similarity
        logo_check["detected_center"] = detected_center
        logo_check["detected_zone"] = _placement_zone(detected_center)
        if similarity < settings["logo_similarity"]:
            logo_check["findings"].append(
                {
                    "type": "logo_violation",
                    "status": "objective_measurement",
                    "reason": (
                        f"No image crop reached the configured logo similarity "
                        f"threshold ({similarity:.1%} measured)."
                    ),
                }
            )
        elif expected_logo_zone != "any" and logo_check["detected_zone"] != expected_logo_zone:
            logo_check["findings"].append(
                {
                    "type": "logo_placement_violation",
                    "status": "objective_measurement",
                    "reason": (
                        f"The strongest logo match is in the {logo_check['detected_zone']} "
                        f"area; the configured area is {expected_logo_zone}."
                    ),
                }
            )

    contrast_results = []
    for pair in contrast_pairs or []:
        foreground = pair["foreground"]
        background = pair["background"]
        ratio = wcag_contrast_ratio(foreground, background)
        threshold = float(pair.get("minimum_ratio", settings["minimum_wcag_contrast"]))
        contrast_results.append(
            {
                "label": pair.get("label", "Configured text/background colour pair"),
                "foreground": foreground,
                "background": background,
                "ratio": ratio,
                "minimum_ratio": threshold,
                "passes": ratio >= threshold,
                "status": "objective_colour_pair_calculation",
                "note": "This checks configured colours, not text pixels detected in this creative.",
            }
        )
    return {
        "assessment_kind": "objective_measurements",
        "image_dimensions": {"width": creative.width, "height": creative.height},
        "colour_check": color_check,
        "logo_check": logo_check,
        "contrast_checks": contrast_results,
        "visual_layout": {
            "status": "not_measured",
            "note": "Logo placement and text bounding boxes are not reliably available from the current OCR adapter.",
        },
        "limitations": [
            "Perceptual-hash similarity is a screening metric, not proof of logo authenticity.",
            "Dominant colours do not identify which regions are logos or text.",
            "Contrast values apply to configured colour pairs, not automatically detected image text.",
        ],
    }
