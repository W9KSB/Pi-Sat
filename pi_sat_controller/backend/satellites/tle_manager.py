from __future__ import annotations

"""TLE download and merge management.

Pi-Sat can ingest multiple TLE sources. When duplicate NORAD IDs are present,
the cache keeps the entry with the newest epoch and discards older duplicates.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from pathlib import Path
from typing import NamedTuple

import requests
from skyfield.api import EarthSatellite, load

LOGGER = logging.getLogger(__name__)


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
    """Downloads, merges, caches, and parses TLE data for the orbital engine."""

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
        """Downloads all configured TLE sources and writes one merged cache file."""

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        errors: list[str] = []
        merged_blocks_by_norad: dict[int, ParsedTle] = {}
        source_urls = self._source_urls()
        LOGGER.info("TLE refresh started with %s source(s)", len(source_urls))
        for url in source_urls:
            LOGGER.info("TLE download attempt: %s", url)
            try:
                response = requests.get(url, timeout=15)
                response.raise_for_status()
                added_from_source = 0
                replaced_from_source = 0
                duplicates_from_source = 0
                for parsed_tle in self._parse_tle_blocks(response.text):
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
                    "TLE download succeeded: %s (added=%s, replaced_with_newer=%s, duplicates_skipped=%s)",
                    url,
                    added_from_source,
                    replaced_from_source,
                    duplicates_from_source,
                )
            except requests.RequestException as exc:
                LOGGER.warning("TLE download failed: %s (%s)", url, exc)
                errors.append(f"{url}: {exc}")
        if merged_blocks_by_norad:
            merged_blocks = sorted(
                merged_blocks_by_norad.values(),
                key=lambda block: block.satnum,
            )
            rendered = "\n".join(
                f"{name}\n{line1}\n{line2}"
                for _, name, line1, line2, _ in merged_blocks
            ) + "\n"
            self.cache_file.write_text(rendered, encoding="utf-8")
            LOGGER.info(
                "TLE refresh complete: cached %s unique satellites at %s",
                len(merged_blocks),
                self.cache_file,
            )
            return TleCacheStatus(
                cache_file=self.cache_file,
                downloaded_at_utc=datetime.now(timezone.utc),
                exists=True,
            )
        error_text = "; ".join(errors) if errors else "No TLE source URLs configured"
        LOGGER.error("TLE refresh failed: %s", error_text)
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

        lines = [
            line.strip()
            for line in text.splitlines()
            if line.strip()
        ]
        blocks: list[ParsedTle] = []
        for index in range(0, len(lines) - 2, 3):
            name = lines[index]
            line1 = lines[index + 1]
            line2 = lines[index + 2]
            if not line1.startswith("1 ") or not line2.startswith("2 "):
                continue
            try:
                satellite = EarthSatellite(line1, line2, name, self.timescale)
            except Exception:
                continue
            blocks.append(
                ParsedTle(
                    satnum=satellite.model.satnum,
                    name=name,
                    line1=line1,
                    line2=line2,
                    epoch_utc=satellite.epoch.utc_datetime().replace(tzinfo=timezone.utc),
                )
            )
        return blocks
