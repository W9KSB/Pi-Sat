"""Validate SSTVFlow events and retain authoritative row replacements."""
from __future__ import annotations

import base64
import io
import math
from typing import Any

SSTV_MODE_DIMENSIONS = {
    "Robot36": (320, 240),
    "PD120": (640, 496),
    "Robot72": (320, 240),
    "Robot24": (160, 120),
    "MartinM1": (320, 256),
    "MartinM2": (320, 256),
    "PD180": (640, 496),
}


class SstvEventAdapter:
    def __init__(self) -> None:
        self.current: dict[str, Any] | None = None
        self.finished: set[str] = set()
        self._frequency_offset_hz = 0.0

    def adapt(self, event: dict[str, Any]) -> dict[str, Any] | None:
        kind = event.get("type")
        offset = event.get("frequency_offset_hz")
        if isinstance(offset, (int, float)) and math.isfinite(offset):
            self._frequency_offset_hz = float(offset)
        if kind not in {"image_start", "row", "row_revision", "image_complete", "image_partial"}:
            return event
        image_id = str(event.get("image_id", ""))
        if not image_id or len(image_id) > 128:
            raise ValueError("Invalid SSTV image identity")
        if image_id in self.finished:
            return None
        if kind == "image_start":
            if self.current is not None and image_id == self.current["image_id"]:
                return None
            mode = str(event["mode"])
            width, height = int(event["width"]), int(event["nominal_height"])
            if SSTV_MODE_DIMENSIONS.get(mode) != (width, height):
                raise ValueError("Invalid SSTV mode dimensions")
            confidence = float(event.get("acquisition_confidence", 0.0))
            confidence = max(0.0, min(1.0, confidence)) if math.isfinite(confidence) else 0.0
            beginning_known = bool(event.get("beginning_known", False))
            acquisition = {
                "method": "VIS" if event.get("acquisition_source") == "VIS" else "blind",
                "source": event.get("acquisition_source"),
                "confidence": confidence,
                "expected_lines": height,
                "row_origin_known": bool(event.get("transmitter_row_numbering_known", False)),
                "beginning_known": beginning_known,
            }
            self.current = dict(image_id=image_id, mode=mode, width=width, height=height,
                                acquisition=acquisition, revisions={}, flags={})
            return dict(type="vis_detected", image_id=image_id, mode=mode, width=width,
                        height=height, frequency_offset_hz=self._frequency_offset_hz,
                        partial=not beginning_known, acquisition=acquisition)
        current = self.current
        if current is None or image_id != current["image_id"]:
            return None
        width = int(event["width"])
        if width != current["width"]:
            raise ValueError("SSTV row/image width does not match acquisition")
        if kind in {"row", "row_revision"}:
            row, revision = int(event["row_index"]), int(event["revision"])
            if not 0 <= row < current["height"] or revision < 0:
                raise ValueError("Invalid SSTV row index or revision")
            if revision <= current["revisions"].get(row, -1):
                return None
            rgb = base64.b64decode(event["rgb"], validate=True)
            if len(rgb) != width * 3:
                raise ValueError("Invalid SSTV row buffer")
            flags = event.get("quality_flags", [])
            if not isinstance(flags, list) or any(not isinstance(flag, str) for flag in flags):
                raise ValueError("Invalid SSTV row quality flags")
            current["revisions"][row] = revision
            current["flags"][row] = flags
            return {**event, "type": "line_decoded", "line_index": row,
                    "rgb_base64": event["rgb"], "row_revision": revision > 0}
        height = int(event["height"])
        if height == 0:
            self._finish(image_id)
            return None
        if not 0 < height <= current["height"] or event.get("mode") != current["mode"]:
            raise ValueError("Invalid final SSTV image dimensions or mode")
        png = base64.b64decode(event["png"], validate=True)
        if len(png) > 16 * 1024 * 1024:
            raise ValueError("Final SSTV PNG exceeds its size limit")
        # Image support is only needed when the optional decoder returns a PNG.
        from PIL import Image

        with Image.open(io.BytesIO(png)) as image:
            if image.format != "PNG" or image.size != (width, height):
                raise ValueError("Final SSTV PNG dimensions do not match its event")
            rgb = image.convert("RGB").tobytes()
        counts = {}
        for name in ("decoded_row_count", "output_row_count", "missing_row_count", "recovered_row_count"):
            value = int(event.get(name, 0))
            if not 0 <= value <= height:
                raise ValueError("Invalid SSTV reception row count")
            counts[name] = value
        if (counts["decoded_row_count"] + counts["missing_row_count"] != counts["output_row_count"]
                or counts["recovered_row_count"] > counts["output_row_count"]):
            raise ValueError("Inconsistent SSTV reception row counts")
        partial = kind == "image_partial" or not current["acquisition"]["beginning_known"]
        signal_flags = {"missing", "low_signal_quality", "timing_recovered",
                        "chroma_phase_unknown", "chroma_phase_inferred"}
        degraded = partial or counts["missing_row_count"] > 0 or counts["recovered_row_count"] > 0 or any(
            signal_flags.intersection(flags) for flags in current["flags"].values()
        ) or counts["output_row_count"] < height
        acquisition = {**current["acquisition"], "recovered_lines": height}
        reception = {**counts, "completion_reason": event.get("completion_reason"),
                     "unknown_tail_rows": event.get("unknown_tail_rows"),
                     "quality_summary": event.get("quality_summary", {}),
                     "image_id": image_id}
        self._finish(image_id)
        return {**event, "type": "image_complete", "rgb_base64": base64.b64encode(rgb).decode("ascii"),
                "png_bytes": png, "partial": partial, "quality": "degraded" if degraded else "clean",
                "acquisition": acquisition, "reception": reception}

    def _finish(self, image_id: str) -> None:
        # Only one image is active; retain a bounded set for duplicate terminal events.
        if len(self.finished) >= 256:
            self.finished.clear()
        self.finished.add(image_id)
        self.current = None
