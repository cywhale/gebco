"""Tests for src.logger lifecycle invariants — v0.5.3 H13."""
from __future__ import annotations

import threading

from src import logger as gebco_logger


def _gebco_listener_threads() -> int:
    """Count active threads whose target looks like a QueueListener thread.

    Python's logging.QueueListener spawns a single daemon thread named
    "Thread-N (_monitor)". We count by looking at the listener handle
    instead, which is more reliable.
    """
    return 1 if gebco_logger._listener is not None else 0


def test_configure_is_idempotent():
    gebco_logger.shutdown()  # ensure clean start
    first = gebco_logger.configure()
    second = gebco_logger.configure()
    assert first is second
    gebco_logger.shutdown()


def test_shutdown_clears_listener():
    gebco_logger.configure()
    assert gebco_logger._listener is not None
    gebco_logger.shutdown()
    assert gebco_logger._listener is None


def test_shutdown_is_safe_before_configure():
    gebco_logger.shutdown()  # should not raise even if never configured
    gebco_logger.shutdown()


def test_reconfigure_after_shutdown_creates_new_listener():
    gebco_logger.shutdown()
    first = gebco_logger.configure()
    gebco_logger.shutdown()
    second = gebco_logger.configure()
    assert first is not second  # fresh listener instance after shutdown
    gebco_logger.shutdown()


def test_no_thread_leak_across_cycles():
    """configure(); shutdown() × N must NOT leak listener threads."""
    gebco_logger.shutdown()
    baseline = threading.active_count()
    for _ in range(5):
        gebco_logger.configure()
        gebco_logger.shutdown()
    # listener stop is async; allow a few of its joins to complete.
    import time
    time.sleep(0.05)
    assert threading.active_count() <= baseline + 1
