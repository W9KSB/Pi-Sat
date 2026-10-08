"""Protocol color conversion, separate from neutral-by-default display controls."""
import numpy as np
from .pixels import _structured_detail


def guided_chroma_noise_reduction(cr, cb, luminance, *, noise_ratio=None):
    """Filter one native shared-chroma pair without changing luminance.

    ``luminance`` contains one or more guides at the chroma's native width.
    All guides must agree before averaging across an edge. Both chroma channels
    share weights, and chroma differences also protect equal-luminance color
    boundaries. Call only after independent reference-tone noise evidence.
    """
    cr, cb = np.asarray(cr, dtype=float), np.asarray(cb, dtype=float)
    if noise_ratio is not None and noise_ratio <= .02:
        return cr.copy(), cb.copy()
    guides = np.atleast_2d(np.asarray(luminance, dtype=float))
    detail = _structured_detail(cr) | _structured_detail(cb)
    radius = 2
    rp, bp = (np.pad(channel, radius, mode="edge") for channel in (cr, cb))
    yp = np.pad(guides, ((0, 0), (radius, radius)), mode="edge")
    total = np.ones_like(cr)
    r, b = cr.copy(), cb.copy()
    for shift in (-2, -1, 1, 2):
        neighbor = slice(radius + shift, radius + shift + len(cr))
        # Protect every intervening edge, including a one-pixel text stroke.
        edge = np.zeros_like(cr)
        for step in range(1, abs(shift) + 1):
            distance = int(np.sign(shift)) * step
            adjacent = yp[:, radius + distance:radius + distance + len(cr)]
            edge = np.maximum(edge, np.max(abs(adjacent - guides), axis=0))
        weight = np.exp(-.5 * (shift / radius)**2 - .5 * (edge / 8)**2
                        - .5 * ((rp[neighbor] - cr) / 40)**2
                        - .5 * ((bp[neighbor] - cb) / 40)**2)
        total += weight
        r += weight * rp[neighbor]
        b += weight * bp[neighbor]
    # This follows frequency-domain treatment, so retain half of the original
    # chroma rather than repeatedly applying a full-strength spatial filter.
    return (np.where(detail, cr, .5 * (cr + r / total)),
            np.where(detail, cb, .5 * (cb + b / total)))


def frequency_to_byte(frequency):
    return np.clip((np.asarray(frequency) - 1500.0) / 800.0 * 255.0, 0, 255)


def ycrcb_to_rgb(y, cr, cb):
    # Limited-range BT.601, as documented in Dayton 2000 Appendix B.
    y = (np.asarray(y) - 16.0) * (255.0 / 219.0)
    cr = (np.asarray(cr) - 128.0) * (255.0 / 224.0)
    cb = (np.asarray(cb) - 128.0) * (255.0 / 224.0)
    return np.clip(np.rint(np.stack((y + 1.402 * cr,
                                    y - .714136 * cr - .344136 * cb,
                                    y + 1.772 * cb), axis=-1)), 0, 255).astype(np.uint8)


def display_adjust(rgb, brightness=0.0, contrast=1.0, gamma=1.0, saturation=1.0):
    if (brightness, contrast, gamma, saturation) == (0.0, 1.0, 1.0, 1.0):
        return rgb
    x = rgb.astype(float) / 255
    gray = x @ np.array([.299, .587, .114])
    x = gray[..., None] + saturation * (x - gray[..., None])
    x = np.clip((x - .5) * contrast + .5 + brightness, 0, 1)
    return np.rint(x ** (1 / gamma) * 255).astype(np.uint8)
