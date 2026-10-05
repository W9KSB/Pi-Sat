from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from pathlib import Path
from queue import Empty, Full, Queue
import re
from threading import Event, Lock, Thread
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

LOGGER = logging.getLogger(__name__)

DISCORD_MESSAGE = "New SSTV image from Pi-Sat"
_DISCORD_WEBHOOK_PATH = re.compile(
    r"^/api(?:/v\d+)?/webhooks/[0-9]+/[A-Za-z0-9._-]+/?$"
)


def validate_discord_webhook_url(value: str) -> str:
    """Return a normalized Discord webhook URL, or raise for unsafe input."""

    url = str(value or "").strip()
    if not url:
        return ""
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(
            "SSTV Discord webhook URL must be a Discord HTTPS webhook URL"
        ) from exc
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
        or not (hostname == "discord.com" or hostname.endswith(".discord.com"))
        or not _DISCORD_WEBHOOK_PATH.fullmatch(parsed.path)
    ):
        raise ValueError("SSTV Discord webhook URL must be a Discord HTTPS webhook URL")
    return url


@dataclass(frozen=True)
class _Notification:
    webhook_url: str
    image_path: Path
    filename: str


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class DiscordWebhookNotifier:
    """Uploads saved SSTV images without blocking the decoder or its pipe readers."""

    def __init__(
        self,
        get_webhook_url: Callable[[], str],
        *,
        opener=None,
    ) -> None:
        self.get_webhook_url = get_webhook_url
        self._opener = opener or build_opener(_NoRedirectHandler())
        self._queue: Queue[_Notification | None] = Queue(maxsize=25)
        self._thread_lock = Lock()
        self._thread: Thread | None = None
        self._stop = Event()

    def enqueue(self, image_path: Path, metadata: dict[str, object]) -> None:
        try:
            webhook_url = validate_discord_webhook_url(self.get_webhook_url())
        except Exception:
            LOGGER.exception("Unable to read the SSTV Discord webhook setting")
            return
        if not webhook_url:
            return

        filename = str(metadata.get("filename") or image_path.name)
        notification = _Notification(webhook_url, Path(image_path), filename)
        self._ensure_worker()
        try:
            self._queue.put_nowait(notification)
        except Full:
            LOGGER.error("SSTV Discord notification queue is full; image was not sent")

    def shutdown(self) -> None:
        with self._thread_lock:
            thread = self._thread
        if thread is None:
            return
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except Full:
            pass
        thread.join(timeout=5.0)

    def _ensure_worker(self) -> None:
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = Thread(
                target=self._run,
                name="sstv-discord-webhook",
                daemon=True,
            )
            self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                notification = self._queue.get(timeout=1.0)
            except Empty:
                continue
            try:
                if notification is None:
                    return
                self._deliver(notification)
            finally:
                self._queue.task_done()

    def _deliver(self, notification: _Notification) -> None:
        try:
            image = notification.image_path.read_bytes()
            filename = "".join(
                character
                for character in Path(notification.filename).name
                if character.isalnum() or character in "._-"
            ) or "sstv.png"
            boundary = f"pi-sat-{uuid4().hex}"
            payload = json.dumps(
                {
                    "content": DISCORD_MESSAGE,
                    "allowed_mentions": {"parse": []},
                    "attachments": [{"id": 0, "filename": filename}],
                }
            ).encode("utf-8")
            body = b"".join(
                (
                    f"--{boundary}\r\n".encode("ascii"),
                    b'Content-Disposition: form-data; name="payload_json"\r\n',
                    b"Content-Type: application/json\r\n\r\n",
                    payload,
                    b"\r\n",
                    f"--{boundary}\r\n".encode("ascii"),
                    (
                        'Content-Disposition: form-data; name="files[0]"; '
                        f'filename="{filename}"\r\n'
                    ).encode("utf-8"),
                    b"Content-Type: image/png\r\n\r\n",
                    image,
                    b"\r\n",
                    f"--{boundary}--\r\n".encode("ascii"),
                )
            )
            request = Request(
                f"{notification.webhook_url}?wait=true",
                data=body,
                headers={
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "User-Agent": "Pi-Sat Controller/1.0",
                },
                method="POST",
            )
            response = self._opener.open(request, timeout=20.0)
            try:
                status = int(response.status)
            finally:
                response.close()
            if not 200 <= status < 300:
                LOGGER.error(
                    "Discord rejected an SSTV image notification with HTTP %d",
                    status,
                )
                return
            LOGGER.info("SSTV image sent to the configured Discord webhook")
        except HTTPError as exc:
            LOGGER.error(
                "Discord rejected an SSTV image notification with HTTP %d",
                exc.code,
            )
        except (OSError, URLError) as exc:
            # Request exception text can contain the credential-bearing webhook URL.
            LOGGER.error(
                "SSTV Discord notification failed (%s)",
                type(exc).__name__,
            )
