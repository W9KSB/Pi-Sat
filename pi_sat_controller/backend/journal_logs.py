from __future__ import annotations

from collections.abc import Callable, Mapping
import json
import subprocess
from typing import Any


JOURNAL_OUTPUT_FIELDS = (
    "__CURSOR",
    "__REALTIME_TIMESTAMP",
    "_BOOT_ID",
    "_COMM",
    "_EXE",
    "_PID",
    "_SYSTEMD_UNIT",
    "_SYSTEMD_USER_UNIT",
    "PRIORITY",
    "SYSLOG_IDENTIFIER",
    "MESSAGE",
)
MAX_MONITOR_LOG_LIMIT = 2000
MAX_MESSAGE_CHARS = 32768

PRIORITY_LABELS = {
    0: "EMERG",
    1: "ALERT",
    2: "CRIT",
    3: "ERROR",
    4: "WARNING",
    5: "NOTICE",
    6: "INFO",
    7: "DEBUG",
}


def _journal_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    if isinstance(value, list) and all(isinstance(item, int) for item in value):
        try:
            return bytes(value).decode("utf-8", errors="replace")
        except ValueError:
            return ""
    return str(value) if value is not None else ""


def _journal_priority(value: object) -> str:
    try:
        priority = int(value)
    except (TypeError, ValueError):
        return "INFO"
    return PRIORITY_LABELS.get(priority, "INFO")


def _bounded_message(value: object) -> str:
    message = _journal_text(value)
    if len(message) <= MAX_MESSAGE_CHARS:
        return message
    return f"{message[:MAX_MESSAGE_CHARS]}\n[log entry truncated]"


def parse_journal_entry(record: Mapping[str, Any]) -> dict[str, object]:
    """Convert one journalctl JSON record into the Monitor API shape."""

    try:
        timestamp_ms = int(record.get("__REALTIME_TIMESTAMP", 0)) // 1000
    except (TypeError, ValueError):
        timestamp_ms = 0

    unit = _journal_text(record.get("_SYSTEMD_UNIT"))
    user_unit = _journal_text(record.get("_SYSTEMD_USER_UNIT"))
    identifier = _journal_text(record.get("SYSLOG_IDENTIFIER"))
    command = _journal_text(record.get("_COMM"))
    executable = _journal_text(record.get("_EXE"))
    source = identifier or command or unit or user_unit or executable or "journal"

    try:
        pid = int(record.get("_PID", 0))
    except (TypeError, ValueError):
        pid = 0

    return {
        "timestamp_ms": timestamp_ms,
        "level": _journal_priority(record.get("PRIORITY")),
        "source": source,
        "message": _bounded_message(record.get("MESSAGE")),
        "cursor": _journal_text(record.get("__CURSOR")),
        "boot_id": _journal_text(record.get("_BOOT_ID")),
        "unit": unit,
        "user_unit": user_unit,
        "identifier": identifier,
        "comm": command,
        "executable": executable,
        "pid": pid,
    }


class JournalLogReader:
    """Read recent system-journal entries without binding to a single unit."""

    def __init__(
        self,
        *,
        executable: str = "journalctl",
        timeout_seconds: float = 8.0,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.executable = executable
        self.timeout_seconds = timeout_seconds
        self._run = run

    def _command(self, limit: int, *, include_fields: bool) -> list[str]:
        command = [
            self.executable,
            "--no-pager",
            "--output=json",
            f"--lines={limit}",
        ]
        if include_fields:
            command.append(f"--output-fields={','.join(JOURNAL_OUTPUT_FIELDS)}")
        return command

    def _execute(
        self, command: list[str]
    ) -> subprocess.CompletedProcess[str]:
        return self._run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout_seconds,
            check=False,
        )

    def read(self, limit: int = 1000) -> dict[str, object]:
        safe_limit = max(1, min(int(limit), MAX_MONITOR_LOG_LIMIT))
        try:
            completed = self._execute(
                self._command(safe_limit, include_fields=True)
            )
            if (
                completed.returncode != 0
                and "output-fields" in completed.stderr.lower()
            ):
                completed = self._execute(
                    self._command(safe_limit, include_fields=False)
                )
        except FileNotFoundError:
            return {
                "entries": [],
                "source": "journalctl",
                "warning": "journalctl is not installed on this host.",
            }
        except subprocess.TimeoutExpired:
            return {
                "entries": [],
                "source": "journalctl",
                "warning": "The system journal did not respond before the read timeout.",
            }
        except OSError as error:
            return {
                "entries": [],
                "source": "journalctl",
                "warning": f"Unable to read the system journal: {error}",
            }

        warning = completed.stderr.strip()
        if completed.returncode != 0:
            return {
                "entries": [],
                "source": "journalctl",
                "warning": warning or "Unable to read the system journal.",
            }

        entries: list[dict[str, object]] = []
        invalid_records = 0
        for line in completed.stdout.splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                invalid_records += 1
                continue
            if isinstance(record, Mapping):
                entries.append(parse_journal_entry(record))
            else:
                invalid_records += 1

        if invalid_records and not warning:
            warning = (
                f"{invalid_records} malformed journal "
                f"{'entry was' if invalid_records == 1 else 'entries were'} skipped."
            )
        return {
            "entries": entries,
            "source": "journalctl",
            "warning": warning,
        }
