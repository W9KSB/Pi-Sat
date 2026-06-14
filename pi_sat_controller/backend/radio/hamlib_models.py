from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class HamlibRadioModel:
    model_id: int
    label: str

    def to_dict(self) -> dict[str, object]:
        return {
            "model_id": self.model_id,
            "label": self.label,
        }


_ALLOWED_MANUFACTURERS = {
    "icom",
    "yaesu",
    "kenwood",
    "flrig",
}

_EXCLUDED_LABEL_KEYWORDS = (
    "dummy",
    "dummy no vfo",
    "powersdr",
    "thetis",
    "sdrconsole",
    "pihpsdr",
)

_INCLUDED_MODEL_IDS = {
    1001,  # Yaesu FT-847
    1020,  # Yaesu FT-817
    1021,  # Yaesu FT-100
    1022,  # Yaesu FT-857
    1023,  # Yaesu FT-897
    1035,  # Yaesu FT-991
    1038,  # Yaesu FT-847UNI
    1041,  # Yaesu FT-818
    1043,  # Yaesu FT-897D
    1047,  # Yaesu FT-650
    1051,  # Guohe Q900
    1052,  # Guohe PMR-171
    2007,  # Kenwood TS-790
    2014,  # Kenwood TS-2000
    3011,  # Icom IC-706MkIIG
    3023,  # Icom IC-746
    3046,  # Icom IC-746PRO
    3060,  # Icom IC-7000
    3068,  # Icom IC-9100
    3070,  # Icom IC-7100
    3081,  # Icom IC-9700
    3085,  # Icom IC-705
    3090,  # Icom IC-905
    31001, # Dorji DRA818V
    31002, # Dorji DRA818U
    35001, # GOMSPACE GS100
    36001, # MDS 4710
}

_BAND_RANGE_RE = re.compile(r"(\d+)\s+Hz\s+-\s+(\d+)\s+Hz")


@dataclass(frozen=True)
class HamlibRadioListEntry:
    model_id: int
    manufacturer: str
    model_name: str
    label: str

    def to_model(self) -> HamlibRadioModel:
        return HamlibRadioModel(model_id=self.model_id, label=self.label)


def parse_rigctl_model_list(output: str) -> list[HamlibRadioModel]:
    return [entry.to_model() for entry in parse_rigctl_model_entries(output)]


def parse_rigctl_model_entries(output: str) -> list[HamlibRadioListEntry]:
    models: list[HamlibRadioListEntry] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            model_id = int(parts[0])
        except ValueError:
            continue
        manufacturer = parts[1]
        model_name = parts[2]
        label_parts = [part for part in (manufacturer, model_name) if part]
        label = " ".join(label_parts) if label_parts else str(model_id)
        models.append(
            HamlibRadioListEntry(
                model_id=model_id,
                manufacturer=manufacturer,
                model_name=model_name,
                label=label,
            )
        )
    return models


def _is_excluded_entry(entry: HamlibRadioListEntry) -> bool:
    manufacturer = entry.manufacturer.strip().lower()
    label = entry.label.strip().lower()
    if manufacturer not in _ALLOWED_MANUFACTURERS:
        return True
    return any(keyword in label for keyword in _EXCLUDED_LABEL_KEYWORDS)


def _iter_relevant_range_lines(caps_output: str):
    in_ranges = False
    for raw_line in caps_output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("TX ranges #") or line.startswith("RX ranges #"):
            in_ranges = True
            continue
        if (
            line.startswith("status for")
            or line.startswith("Tuning steps:")
            or line.startswith("Filters:")
            or line.startswith("Bandwidths:")
        ):
            in_ranges = False
            continue
        if in_ranges:
            yield line


def _supports_vhf_or_uhf(caps_output: str) -> bool:
    for line in _iter_relevant_range_lines(caps_output):
        match = _BAND_RANGE_RE.search(line)
        if not match:
            continue
        low_hz = int(match.group(1))
        high_hz = int(match.group(2))
        if (low_hz <= 148_000_000 and high_hz >= 144_000_000) or (
            low_hz <= 450_000_000 and high_hz >= 430_000_000
        ):
            return True
    return False


def _load_rig_caps(model_id: int, timeout_s: float) -> str:
    result = subprocess.run(
        ["rigctl", "-m", str(model_id), "-u"],
        capture_output=True,
        check=False,
        text=True,
        timeout=timeout_s,
    )
    if result.returncode != 0:
        error = (result.stderr or result.stdout or f"rigctl -m {model_id} -u failed").strip()
        raise RuntimeError(error)
    return result.stdout


def load_hamlib_radio_models(timeout_s: float = 5.0) -> list[HamlibRadioModel]:
    result = subprocess.run(
        ["rigctl", "-l"],
        capture_output=True,
        check=False,
        text=True,
        timeout=timeout_s,
    )
    if result.returncode != 0:
        error = (result.stderr or result.stdout or "rigctl -l failed").strip()
        raise RuntimeError(error)
    filtered_models: list[HamlibRadioModel] = []
    for entry in parse_rigctl_model_entries(result.stdout):
        if _is_excluded_entry(entry):
            continue
        if entry.model_id in _INCLUDED_MODEL_IDS:
            filtered_models.append(entry.to_model())
            continue
        try:
            caps_output = _load_rig_caps(entry.model_id, timeout_s=timeout_s)
        except Exception:
            continue
        if _supports_vhf_or_uhf(caps_output):
            filtered_models.append(entry.to_model())
    return filtered_models
