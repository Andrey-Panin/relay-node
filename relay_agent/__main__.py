from __future__ import annotations

import logging
import sys

from .config import Config, ConfigError
from .service import run


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        config = Config.from_env()
        run(config)
    except ConfigError as exc:
        logging.getLogger("relay_agent").error("configuration error: %s", exc)
        return 2
    except Exception:
        logging.getLogger("relay_agent").exception("fatal relay-agent error")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

