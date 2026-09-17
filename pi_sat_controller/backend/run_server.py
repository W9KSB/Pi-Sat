from __future__ import annotations

import uvicorn

from pi_sat_controller.backend.config import load_config
from pi_sat_controller.backend.tls import ensure_certificate


# A console page keeps requests and sockets open, and uvicorn's shutdown drains
# every one of them before it runs the application teardown. Left unbounded
# (uvicorn's default) a single lingering browser connection held a restart for
# roughly 30 seconds, so the port and the radio stayed claimed the whole time.
GRACEFUL_SHUTDOWN_TIMEOUT_S = 5


def main() -> None:
    config = load_config()
    tls_options = {}
    if config.server.https_enabled:
        certfile, keyfile = ensure_certificate(config.server)
        tls_options = {"ssl_certfile": str(certfile), "ssl_keyfile": str(keyfile)}
    uvicorn.run(
        "pi_sat_controller.backend.app:app",
        host=config.server.host,
        port=config.server.listen_port,
        log_level="info",
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_TIMEOUT_S,
        **tls_options,
    )


if __name__ == "__main__":
    main()
