from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
import time

from pi_sat_controller.backend.config import PROJECT_ROOT

SCRIPTS_DIR = PROJECT_ROOT / "scripts"
ALLOWED_SCRIPT_SUFFIXES = {".py", ".sh"}


@dataclass(frozen=True)
class AutomationScript:
    name: str
    suffix: str

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "suffix": self.suffix,
        }


def list_automation_scripts() -> list[AutomationScript]:
    if not SCRIPTS_DIR.exists() or not SCRIPTS_DIR.is_dir():
        return []
    scripts: list[AutomationScript] = []
    for path in sorted(SCRIPTS_DIR.iterdir(), key=lambda item: item.name.lower()):
        if not path.is_file():
            continue
        if path.suffix.lower() not in ALLOWED_SCRIPT_SUFFIXES:
            continue
        scripts.append(
            AutomationScript(
                name=path.name,
                suffix=path.suffix.lower(),
            )
        )
    return scripts


def run_automation_script(
    script_name: str,
    event_name: str,
    context: dict[str, object] | None = None,
    timeout_s: float = 30.0,
) -> dict[str, object]:
    script_path = resolve_automation_script(script_name)
    command = _build_script_command(script_path)
    env = os.environ.copy()
    env.update(
        {
            "PI_SAT_EVENT": event_name,
            "PI_SAT_SCRIPT_NAME": script_path.name,
            "PI_SAT_SCRIPTS_DIR": str(SCRIPTS_DIR),
            "PI_SAT_PROJECT_ROOT": str(PROJECT_ROOT),
        }
    )
    for key, value in (context or {}).items():
        if value is None:
            continue
        normalized_key = "".join(
            character if character.isalnum() else "_"
            for character in str(key).upper()
        ).strip("_")
        if not normalized_key:
            continue
        env[f"PI_SAT_{normalized_key}"] = str(value)
    started = time.monotonic()
    completed = subprocess.run(
        command,
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )
    duration_ms = int((time.monotonic() - started) * 1000)
    stdout = completed.stdout.strip()
    stderr = completed.stderr.strip()
    return {
        "ok": completed.returncode == 0,
        "script_name": script_path.name,
        "event_name": event_name,
        "command": command,
        "exit_code": completed.returncode,
        "duration_ms": duration_ms,
        "stdout": stdout,
        "stderr": stderr,
    }


def resolve_automation_script(script_name: str) -> Path:
    normalized_name = str(script_name or "").strip()
    if not normalized_name or normalized_name.lower() == "none":
        raise ValueError("No script selected.")
    candidate = (SCRIPTS_DIR / normalized_name).resolve()
    scripts_root = SCRIPTS_DIR.resolve()
    try:
        candidate.relative_to(scripts_root)
    except ValueError as exc:
        raise ValueError("Script path is outside the scripts directory.") from exc
    if not candidate.exists() or not candidate.is_file():
        raise ValueError(f"Script not found: {normalized_name}")
    if candidate.suffix.lower() not in ALLOWED_SCRIPT_SUFFIXES:
        raise ValueError("Only .sh and .py scripts are supported.")
    return candidate


def _build_script_command(script_path: Path) -> list[str]:
    suffix = script_path.suffix.lower()
    if suffix == ".py":
        python_path = shutil.which("python3") or shutil.which("python")
        if not python_path:
            raise RuntimeError("python3 is not available on this system.")
        return [python_path, str(script_path)]
    if suffix == ".sh":
        bash_path = shutil.which("bash") or "/bin/bash"
        return [bash_path, str(script_path)]
    raise ValueError("Unsupported script type.")
