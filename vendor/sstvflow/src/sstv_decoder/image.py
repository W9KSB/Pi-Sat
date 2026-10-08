"""Authoritative row state and monotonic replacement events."""
import base64
import io
import uuid
import numpy as np
from PIL import Image


class ImageState:
    def __init__(self, emit, mode, width, height, source, confidence, beginning_known):
        self.emit = emit
        self.id = uuid.uuid4().hex
        self.mode = mode
        self.width = width
        self.height = height
        self.beginning_known = beginning_known
        self.rows = {}
        self.revisions = {}
        self.flags = {}
        emit(dict(type="image_start", image_id=self.id, mode=mode, width=width,
                  nominal_height=height, acquisition_source=source,
                  acquisition_confidence=confidence, beginning_known=beginning_known,
                  transmitter_row_numbering_known=beginning_known))

    def row(self, index, rgb, confidence, flags, reason=None, **metadata):
        if not 0 <= index < self.height or rgb.shape != (self.width, 3):
            raise ValueError("invalid output row")
        revision = self.revisions.get(index, -1) + 1
        self.rows[index] = rgb.copy()
        self.revisions[index] = revision
        self.flags[index] = list(flags)
        event = dict(type="row" if revision == 0 else "row_revision", image_id=self.id,
                     row_index=index, width=self.width, revision=revision,
                     rgb=base64.b64encode(rgb.tobytes()).decode("ascii"),
                     row_confidence=float(confidence), quality_flags=list(flags), **metadata)
        if revision:
            event["reason"] = reason or "adjacent_chroma_received"
        self.emit(event)

    def finish(self, reason, status):
        if not self.rows:
            kind = "image_partial"
            actual_height = 0
        else:
            actual_height = max(self.rows) + 1
            kind = "image_complete" if self.beginning_known and len(self.rows) == self.height else "image_partial"
        canvas = np.zeros((max(1, actual_height), self.width, 3), np.uint8)
        for y, pixels in self.rows.items():
            canvas[y] = pixels
        data = io.BytesIO()
        Image.fromarray(canvas).save(data, format="PNG")
        missing = sum("missing" in self.flags[i] for i in self.rows)
        recovered = sum("timing_recovered" in self.flags[i] for i in self.rows)
        self.emit(dict(status, type=kind, image_id=self.id, mode=self.mode, completion_reason=reason,
                       decoded_row_count=len(self.rows)-missing, output_row_count=len(self.rows),
                       missing_row_count=missing, recovered_row_count=recovered,
                       unknown_tail_rows=self.height-actual_height if self.beginning_known else None,
                       width=self.width, height=actual_height,
                       png=base64.b64encode(data.getvalue()).decode("ascii"),
                       quality_summary={"flagged_rows":sum(bool(v) for v in self.flags.values())}))
