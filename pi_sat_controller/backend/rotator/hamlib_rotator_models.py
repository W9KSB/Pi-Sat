from __future__ import annotations

import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class HamlibRotatorModel:
    model_id: int
    label: str

    def to_dict(self) -> dict[str, object]:
        return {
            "model_id": self.model_id,
            "label": self.label,
        }


def parse_rotctl_model_list(output: str) -> list[HamlibRotatorModel]:
    models: list[HamlibRotatorModel] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        first = parts[0]
        try:
            model_id = int(first)
        except ValueError:
            continue
        manufacturer = parts[1]
        model_name = parts[2]
        label_parts = [part for part in (manufacturer, model_name) if part]
        label = " ".join(label_parts) if label_parts else str(model_id)
        models.append(HamlibRotatorModel(model_id=model_id, label=label))
    return models


def load_hamlib_rotator_models(timeout_s: float = 5.0) -> list[HamlibRotatorModel]:
    result = subprocess.run(
        ["rotctl", "-l"],
        capture_output=True,
        check=False,
        text=True,
        timeout=timeout_s,
    )
    if result.returncode != 0:
        error = (result.stderr or result.stdout or "rotctl -l failed").strip()
        raise RuntimeError(error)
    return parse_rotctl_model_list(result.stdout)
