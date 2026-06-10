from __future__ import annotations

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


def parse_rigctl_model_list(output: str) -> list[HamlibRadioModel]:
    models: list[HamlibRadioModel] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        first, _, rest = line.partition(" ")
        try:
            model_id = int(first)
        except ValueError:
            continue
        label = " ".join(rest.split()) if rest else str(model_id)
        models.append(HamlibRadioModel(model_id=model_id, label=label))
    return models


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
    return parse_rigctl_model_list(result.stdout)
