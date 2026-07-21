from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import logging
from threading import Lock

from pi_sat_controller.backend.models import SatellitePass


class AutotrackCoordinator:
    """Selects each upcoming configured pass once from the shared backend state."""

    def __init__(
        self,
        *,
        load_options: Callable[[], tuple[set[int], bool]],
        get_passes: Callable[[], list[SatellitePass]],
        start_pass: Callable[[SatellitePass], bool | None],
        run_pre_aos: Callable[[SatellitePass], None],
        logger: logging.Logger,
        retry_interval_s: float = 30.0,
        pre_aos_lead_s: float = 15.0,
    ) -> None:
        self._load_options = load_options
        self._get_passes = get_passes
        self._start_pass = start_pass
        self._run_pre_aos = run_pre_aos
        self._logger = logger
        self._retry_interval = timedelta(seconds=max(1.0, retry_interval_s))
        self._pre_aos_lead = timedelta(seconds=max(0.0, pre_aos_lead_s))
        self._handled_pass_key: tuple[int, str] | None = None
        self._pre_aos_pass_key: tuple[int, str] | None = None
        self._last_attempt_key: tuple[int, str] | None = None
        self._last_attempt_at: datetime | None = None
        self._lock = Lock()

    def tick(self, now_utc: datetime | None = None) -> bool:
        """Evaluates current passes and returns True only when a pass is selected."""

        now_utc = now_utc or datetime.now(timezone.utc)
        configured_norads, enabled = self._load_options()
        if not enabled:
            self._clear_state()
            return False

        next_pass = _select_next_pass(
            self._get_passes(),
            configured_norads,
            now_utc,
        )
        if next_pass is None:
            self._clear_state()
            return False

        pass_key = (next_pass.norad_id, next_pass.aos_utc.isoformat())
        with self._lock:
            if self._handled_pass_key == pass_key:
                pass_already_started = True
            else:
                pass_already_started = False
            if (
                not pass_already_started
                and self._last_attempt_key == pass_key
                and self._last_attempt_at is not None
                and now_utc - self._last_attempt_at < self._retry_interval
            ):
                return False
            if not pass_already_started:
                self._last_attempt_key = pass_key
                self._last_attempt_at = now_utc

        if pass_already_started:
            self._trigger_pre_aos_if_due(next_pass, pass_key, now_utc)
            return False

        try:
            started = self._start_pass(next_pass)
        except Exception:
            self._logger.exception(
                "Autotrack failed satellite=%s norad=%s aos=%s",
                next_pass.satellite_name,
                next_pass.norad_id,
                next_pass.aos_utc.isoformat(),
            )
            return False
        if started is False:
            return False

        with self._lock:
            self._handled_pass_key = pass_key
        self._trigger_pre_aos_if_due(next_pass, pass_key, now_utc)
        self._logger.info(
            "Autotrack selected satellite=%s norad=%s aos=%s los=%s",
            next_pass.satellite_name,
            next_pass.norad_id,
            next_pass.aos_utc.isoformat(),
            next_pass.los_utc.isoformat(),
        )
        return True

    def _trigger_pre_aos_if_due(
        self,
        satellite_pass: SatellitePass,
        pass_key: tuple[int, str],
        now_utc: datetime,
    ) -> None:
        if now_utc < satellite_pass.aos_utc - self._pre_aos_lead:
            return
        with self._lock:
            if self._pre_aos_pass_key == pass_key:
                return
            self._pre_aos_pass_key = pass_key
        try:
            self._run_pre_aos(satellite_pass)
        except Exception:
            with self._lock:
                self._pre_aos_pass_key = None
            self._logger.exception(
                "Pre-AOS automation failed satellite=%s norad=%s aos=%s",
                satellite_pass.satellite_name,
                satellite_pass.norad_id,
                satellite_pass.aos_utc.isoformat(),
            )

    def reset(self) -> None:
        """Forgets the handled pass after an explicit autotrack setting change."""

        self._clear_state()

    def _clear_state(self) -> None:
        with self._lock:
            self._handled_pass_key = None
            self._pre_aos_pass_key = None
            self._last_attempt_key = None
            self._last_attempt_at = None


def _select_next_pass(
    passes: list[SatellitePass],
    configured_norads: set[int],
    now_utc: datetime,
) -> SatellitePass | None:
    upcoming = [
        satellite_pass
        for satellite_pass in passes
        if satellite_pass.norad_id in configured_norads
        and now_utc <= satellite_pass.los_utc
    ]
    return min(upcoming, key=lambda satellite_pass: satellite_pass.aos_utc, default=None)
