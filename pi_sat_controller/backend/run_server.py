from __future__ import annotations

import uvicorn

from pi_sat_controller.backend.config import load_config


def main() -> None:
    config = load_config()
    uvicorn.run(
        "pi_sat_controller.backend.app:app",
        host=config.server.host,
        port=config.server.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
