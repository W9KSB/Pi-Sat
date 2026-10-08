from __future__ import annotations

"""Orbital element download and merge management.

Pi-Sat can ingest TLE and GP/OMM JSON sources. When duplicate NORAD IDs are present,
the cache keeps the entry with the newest epoch and discards older duplicates.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock
from typing import NamedTuple

import requests
from skyfield.api import load

from pi_sat_controller.backend.satellites.orbital_elements import (
    OrbitalElements,
    parse_orbital_elements,
    render_orbital_cache,
)

LOGGER = logging.getLogger(__name__)
_TLE_WRITE_LOCK = Lock()


@dataclass(frozen=True)
class TleCacheStatus:
    cache_file: Path
    downloaded_at_utc: datetime | None
    exists: bool


class ParsedTle(NamedTuple):
    satnum: int
    name: str
    line1: str
    line2: str
    epoch_utc: datetime


class TleManager:
    """Downloads and merges TLE or GP/OMM JSON data for the orbital engine."""

    def __init__(self, source_url: str, cache_dir: Path) -> None:
        self.source_url = source_url
        self.cache_dir = cache_dir
        self.cache_file = cache_dir / "tle_cache.tle"
        self.timescale = load.timescale()

    def _source_urls(self) -> list[str]:
        return [
            line.strip()
            for line in self.source_url.splitlines()
            if line.strip()
        ]

    def download(self) -> TleCacheStatus:
        """Detects each source's format and writes one merged orbital cache."""

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        errors: list[str] = []
        merged_blocks_by_norad: dict[int, OrbitalElements] = {}
        source_urls = self._source_urls()
        LOGGER.info("Orbital data refresh started with %s source(s)", len(source_urls))
        for url in source_urls:
            LOGGER.info("Orbital data download attempt: %s", url)
            try:
                response = requests.get(url, timeout=15)
                response.raise_for_status()
                added_from_source = 0
                replaced_from_source = 0
                duplicates_from_source = 0
                parsed_records = parse_orbital_elements(response.text, self.timescale)
                if not parsed_records:
                    raise ValueError("Source contains no usable TLE or GP/OMM JSON records")
                for parsed_tle in parsed_records:
                    existing = merged_blocks_by_norad.get(parsed_tle.satnum)
                    if existing is None:
                        merged_blocks_by_norad[parsed_tle.satnum] = parsed_tle
                        added_from_source += 1
                        continue
                    if parsed_tle.epoch_utc > existing.epoch_utc:
                        merged_blocks_by_norad[parsed_tle.satnum] = parsed_tle
                        replaced_from_source += 1
                    else:
                        duplicates_from_source += 1
                LOGGER.info(
                    "Orbital data download succeeded: %s (added=%s, replaced_with_newer=%s, duplicates_skipped=%s)",
                    url,
                    added_from_source,
                    replaced_from_source,
                    duplicates_from_source,
                )
            except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
                LOGGER.warning("Orbital data download failed: %s (%s)", url, exc)
                errors.append(f"{url}: {exc}")
        if merged_blocks_by_norad:
            merged_blocks = sorted(
                merged_blocks_by_norad.values(),
                key=lambda block: block.satnum,
            )
            rendered = render_orbital_cache(merged_blocks)
            temporary_path: Path | None = None
            with _TLE_WRITE_LOCK:
                try:
                    with NamedTemporaryFile(
                        "w",
                        encoding="utf-8",
                        dir=self.cache_dir,
                        prefix=f".{self.cache_file.name}.",
                        suffix=".tmp",
                        delete=False,
                    ) as temporary:
                        temporary.write(rendered)
                        temporary.flush()
                        os.fsync(temporary.fileno())
                        temporary_path = Path(temporary.name)
                    os.replace(temporary_path, self.cache_file)
                    temporary_path = None
                finally:
                    if temporary_path is not None:
                        temporary_path.unlink(missing_ok=True)
            LOGGER.info(
                "Orbital data refresh complete: cached %s unique satellites at %s",
                len(merged_blocks),
                self.cache_file,
            )
            return TleCacheStatus(
                cache_file=self.cache_file,
                downloaded_at_utc=datetime.now(timezone.utc),
                exists=True,
            )
        error_text = "; ".join(errors) if errors else "No orbital data source URLs configured"
        LOGGER.error("Orbital data refresh failed: %s", error_text)
        raise requests.RequestException(error_text)

    def status(self) -> TleCacheStatus:
        exists = self.cache_file.exists()
        downloaded_at = None
        if exists:
            downloaded_at = datetime.fromtimestamp(
                self.cache_file.stat().st_mtime, tz=timezone.utc
            )
        return TleCacheStatus(
            cache_file=self.cache_file,
            downloaded_at_utc=downloaded_at,
            exists=exists,
        )

    def _parse_tle_blocks(self, text: str) -> list[ParsedTle]:
        """Parses 3-line TLE blocks and extracts epoch metadata for merge decisions."""

        return [
            ParsedTle(
                satnum=record.satnum,
                name=record.satellite.name,
                line1=record.tle_lines[0],
                line2=record.tle_lines[1],
                epoch_utc=record.epoch_utc,
            )
            for record in parse_orbital_elements(text, self.timescale)
            if record.tle_lines is not None
        ]
