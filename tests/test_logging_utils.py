"""Only successful routine discovery chatter is demoted."""

import logging

import pytest

from logging_utils import RoutineDiscoveryFilter, configure_library_logging


@pytest.mark.parametrize("logger,message", [
    ("aidot.discover", "_schedule_broadcast"),
    ("aidot.discover", "send_broadcast {'method': 'devDiscoveryReq'}"),
    ("aidot.discover", "datagram_received {'method': 'devDiscoveryResp'}"),
    ("aidot.client", "setup_discover"),
])
def test_routine_messages_are_debug_only(caplog, logger, message):
    source = logging.getLogger(logger)
    filter_ = RoutineDiscoveryFilter()
    source.addFilter(filter_)
    try:
        with caplog.at_level(logging.WARNING, logger=logger):
            source.warning(message)
        assert not caplog.records
        with caplog.at_level(logging.DEBUG, logger=logger):
            source.warning(message)
        assert caplog.records[-1].levelno == logging.DEBUG
    finally:
        source.removeFilter(filter_)


def test_actual_discovery_errors_and_unknown_warnings_are_kept(caplog):
    source = logging.getLogger("aidot.discover")
    filter_ = RoutineDiscoveryFilter()
    source.addFilter(filter_)
    try:
        with caplog.at_level(logging.WARNING, logger=source.name):
            source.error("datagram_received error: invalid packet")
            source.warning("unexpected discovery condition")
        assert [r.levelno for r in caplog.records] == [logging.ERROR, logging.WARNING]
    finally:
        source.removeFilter(filter_)


def test_installing_filters_is_idempotent():
    configure_library_logging()
    configure_library_logging()
    for name in ("aidot.discover", "aidot.client"):
        logger = logging.getLogger(name)
        filters = [f for f in logger.filters if f.name == "aidot_routine_discovery"]
        assert len(filters) == 1
        for filter_ in filters:
            logger.removeFilter(filter_)
