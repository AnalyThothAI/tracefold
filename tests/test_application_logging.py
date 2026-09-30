import logging
import sys

from loguru import logger

from tracefold.platform.observability.logging import setup_logging


def test_application_standard_logs_reach_file_once_without_http_client_urls(tmp_path) -> None:
    application = logging.getLogger("tracefold")
    before = (list(application.handlers), application.level, application.propagate)
    try:
        target = tmp_path / "application.log"
        setup_logging(target)
        setup_logging(target)
        logging.getLogger("tracefold.app.executor").warning("fixture_reconcile_failure")
        logging.getLogger("httpx").warning("fixture HTTP url signature=do-not-persist")
        content = target.read_text()
        assert content.count("fixture_reconcile_failure") == 1
        assert "signature=" not in content
    finally:
        application.handlers, application.level, application.propagate = before
        logger.remove()
        logger.add(sys.stderr)
