from __future__ import annotations

import ipaddress
import logging
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import subprocess
import sys
from tempfile import TemporaryDirectory

from pi_sat_controller.backend.config import ServerConfig


logger = logging.getLogger("uvicorn.error")


def _subject_alt_names(config: ServerConfig) -> str:
    names = {"DNS:localhost", "IP:127.0.0.1", "IP:::1"}

    def add(value: str) -> None:
        value = value.strip()
        if not value:
            return
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            # Names become OpenSSL extension values, so reject syntax/control chars.
            labels = value.rstrip(".").split(".")
            if len(value) > 253 or any(
                not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                for label in labels
            ):
                raise ValueError("Invalid TLS DNS name; use comma-separated hostnames or IP addresses")
            names.add(f"DNS:{value.rstrip('.').lower()}")
        else:
            if not address.is_unspecified:
                names.add(f"IP:{address}")

    for value in config.tls_names.split(","):
        add(value)
    add(config.host)
    for hostname in {socket.gethostname(), socket.getfqdn()}:
        try:
            add(hostname)
        except ValueError:
            continue
        try:
            for result in socket.getaddrinfo(hostname, None):
                add(result[4][0].split("%")[0])
        except socket.gaierror:
            pass
    # On Pi OS the hostname may resolve only to loopback. Read actual interface
    # addresses without opening a connection to any radio or external service.
    hostname_command = shutil.which("hostname") if sys.platform.startswith("linux") else None
    if hostname_command:
        try:
            result = subprocess.run(
                [hostname_command, "-I"], capture_output=True, text=True, timeout=5, check=True,
            )
            for value in result.stdout.split():
                add(str(ipaddress.ip_address(value)))
        except (OSError, subprocess.SubprocessError, ValueError):
            logger.warning("Could not enumerate TLS interface addresses; use [server] tls_names if needed")
    return ",".join(sorted(names))


def _validate_pair(certfile: Path, keyfile: Path) -> None:
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        # Explicit password callback prevents an interactive prompt on a service.
        context.load_cert_chain(certfile, keyfile, password=lambda: "")
    except (OSError, ssl.SSLError) as exc:
        raise RuntimeError(
            "Cannot load TLS certificate/key pair. Check PEM format, matching key, "
            "permissions and an unencrypted service key. Existing files were not replaced."
        ) from exc


def ensure_certificate(config: ServerConfig) -> tuple[Path, Path]:
    """Reuse supplied PEM files, or create a local self-signed pair on first start."""
    certfile, keyfile = config.tls_certfile, config.tls_keyfile
    if certfile.resolve() == keyfile.resolve():
        raise ValueError("TLS certificate and private key must use different files")
    if certfile.exists():
        if not keyfile.is_file():
            raise RuntimeError("TLS certificate exists but its private key is missing; restore the matching key")
        _validate_pair(certfile, keyfile)
        return certfile, keyfile
    if certfile.is_symlink() or keyfile.is_symlink():
        raise RuntimeError("Will not generate TLS files through a symbolic link")
    openssl = shutil.which("openssl")
    if not openssl:
        raise RuntimeError(
            "HTTPS needs OpenSSL to generate a missing certificate. Install the openssl "
            "system package or supply [server] tls_certfile and tls_keyfile. HTTP was not enabled."
        )
    alt_names = _subject_alt_names(config)
    certfile.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    keyfile.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".tls-", dir=certfile.parent) as directory:
        staging = Path(directory)
        staged_cert, staged_key = staging / "server.crt", staging / "server.key"
        openssl_config = staging / "openssl.cnf"
        openssl_config.write_text("[req]\ndistinguished_name = dn\n[dn]\n", encoding="ascii")
        reuse_key = keyfile.exists()
        if reuse_key:
            key_args = ["-key", str(keyfile), "-passin", "pass:"]
        else:
            # Precreate mode 0600 before OpenSSL writes any private key bytes.
            fd = os.open(staged_key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
            key_args = ["-newkey", "rsa:2048", "-nodes", "-keyout", str(staged_key)]
        command = [
            openssl, "req", "-x509", "-new", "-batch", "-sha256", "-days", "365",
            "-config", str(openssl_config), "-subj", "/CN=Pi-Sat",
            "-addext", f"subjectAltName={alt_names}",
            "-addext", "basicConstraints=critical,CA:FALSE",
            "-addext", "keyUsage=critical,digitalSignature,keyEncipherment",
            "-addext", "extendedKeyUsage=serverAuth",
            "-out", str(staged_cert), *key_args,
        ]
        try:
            subprocess.run(command, capture_output=True, timeout=30, check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError("Self-signed TLS certificate generation failed; HTTPS startup stopped") from exc
        _validate_pair(staged_cert, keyfile if reuse_key else staged_key)
        # Exclusive publication never overwrites a supplied pair, even if another
        # startup creates it concurrently. An interrupted key-only start is reusable.
        if not reuse_key:
            # Stage beside the key too: custom certificate/key dirs can be on
            # different filesystems, where hard-linking from cert dir would fail.
            with TemporaryDirectory(prefix=".tls-", dir=keyfile.parent) as key_directory:
                publish_key = Path(key_directory) / "server.key"
                shutil.copyfile(staged_key, publish_key)
                publish_key.chmod(0o600)
                os.link(publish_key, keyfile)
        os.link(staged_cert, certfile)
    logger.warning("Generated self-signed HTTPS certificate; browser trust/acceptance is required")
    return certfile, keyfile
