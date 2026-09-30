import logging
import sys
from pathlib import Path
from typing import Any

from loguru import logger

LOG_FORMAT = "<level>{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | {message}</level>"
FILE_FORMAT = "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | {message}"


class _ApplicationLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        logger.opt(exception=record.exc_info).log(record.levelname, record.getMessage())


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

    # Bridge only application records. HTTP client INFO logs may contain signed
    # URLs and must not be promoted into the application's persisted log.
    application = logging.getLogger("tracefold")
    for handler in tuple(application.handlers):
        if isinstance(handler, _ApplicationLogHandler):
            application.removeHandler(handler)
    application.addHandler(_ApplicationLogHandler())
    application.setLevel(logging.INFO)
    application.propagate = False

    logger.add(
        sys.stderr,
        level="INFO",
        format=LOG_FORMAT,
        colorize=True,
        diagnose=False,
    )

    return logger
