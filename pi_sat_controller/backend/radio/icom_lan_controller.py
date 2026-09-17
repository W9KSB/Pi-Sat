from __future__ import annotations

"""Compose the native IC-9700 controller with LAN connectivity."""

from threading import Event

from pi_sat_controller.backend.radio.icom_lan_connectivity import (
    IcomLanConfig,
    IcomLanConnectivity,
)
from pi_sat_controller.backend.radio.icom_radio_controller import (
    IcomRadioError,
    IcomRadioConfig,
    IcomRadioController,
    IcomRadioState,
)
from pi_sat_controller.backend.radio.icom_connectivity import IcomError

IcomLanError = IcomError


class IcomLanController(IcomRadioController):
    def __init__(self, config: IcomLanConfig) -> None:
        stop_event = Event()
        radio_config = IcomRadioConfig(
            civ_address=config.civ_address,
            controller_address=config.controller_address,
            sample_rate=config.sample_rate,
            rx_codec=config.rx_codec,
            tx_codec=config.tx_codec,
            full_duplex=config.full_duplex,
            scope_enabled=config.scope_enabled,
            debug_logging=config.debug_logging,
        )
        super().__init__(
            radio_config,
            IcomLanConnectivity(config, stop_event),
            stop_event,
        )
