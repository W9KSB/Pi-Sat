"""Configuration-backed per-module decoder input trims."""

from __future__ import annotations

import logging

from pi_sat_controller.backend.audio_gain import RX_GAIN_DEFAULT_DB
from pi_sat_controller.backend.config import load_config, save_settings

LOGGER = logging.getLogger(__name__)


def configured_rx_gain_db(section: str) -> float:
    """Decoder input trim for one module; never fatal at startup."""
    try:
        return float(getattr(load_config(), section).rx_gain_db)
    except Exception:
        LOGGER.warning("Could not read [%s] rx_gain_db; using the default", section, exc_info=True)
        return RX_GAIN_DEFAULT_DB


def save_rx_gain_db(section: str, value: float) -> None:
    """Persist a module's decoder input trim so it survives a restart."""
    save_settings({section: {"rx_gain_db": f"{float(value):g}"}})
