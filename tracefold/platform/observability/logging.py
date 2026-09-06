import sys
from pathlib import Path
from typing import Any

from loguru import logger

LOG_FORMAT = "<level>{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | {message}</level>"
FILE_FORMAT = "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | {message}"


def setup_logging(log_file: Path | str) -> Any:
    logger.remove()
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger.add(
        log_path,
        rotation="10 MB",
        retention="7 days",
        level="INFO",
        format=FILE_FORMAT,
        colorize=False,
        diagnose=False,
    )

    logger.add(
        sys.stderr,
        level="INFO",
        format=LOG_FORMAT,
        colorize=True,
        diagnose=False,
    )

    return logger
