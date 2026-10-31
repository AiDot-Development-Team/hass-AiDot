"""Keep routine messages from python-aidot out of warning-level logs."""

import logging


class RoutineDiscoveryFilter(logging.Filter):
    """Demote known successful discovery messages, preserving real failures."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno != logging.WARNING:
            return True
        message = record.getMessage()
        routine = (
            record.name == "aidot.discover"
            and (
                message == "_schedule_broadcast"
                or message.startswith("send_broadcast {")
                or message.startswith("datagram_received {")
            )
        ) or (record.name == "aidot.client" and message == "setup_discover")
        if not routine:
            return True
        if not logging.getLogger(record.name).isEnabledFor(logging.DEBUG):
            return False
        record.levelno = logging.DEBUG
        record.levelname = "DEBUG"
        return True


def configure_library_logging() -> None:
    """Install narrow, idempotent filters without changing user log levels."""
    for name in ("aidot.discover", "aidot.client"):
        logger = logging.getLogger(name)
        if not any(f.name == "aidot_routine_discovery" for f in logger.filters):
            logger.addFilter(RoutineDiscoveryFilter("aidot_routine_discovery"))
