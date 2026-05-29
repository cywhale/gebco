"""Structured app-level logging with QueueHandler (v0.5.3 H13).

Why this exists:
  Pre-v0.5.3 the codebase used `print()` for debug output in
  polyhandler. Gunicorn access log captures the request URL but not
  what the server actually processed (mode breakdown, polygon row
  count, elapsed time, etc.).

Why QueueHandler:
  Sync `StreamHandler` in the FastAPI request hot path can stall the
  event loop on filesystem fsync under heavy traffic. `QueueHandler`
  enqueues a LogRecord in ~1 µs; the actual `StreamHandler` write
  happens on a dedicated background thread (`QueueListener`).

Public surface:
  * `configure() -> QueueListener` — idempotent setup. Reads
    `GEBCO_LOG_LEVEL` (default WARNING).
  * `shutdown() -> None` — stop the listener and clear the global.
    `lifespan()` calls this on app shutdown; test fixtures call it on
    teardown to prevent thread accumulation across the suite.
  * `logger = logging.getLogger("gebco")` — the application logger,
    importable from anywhere. Modules below `src/` already do
    `logging.getLogger("gebco")` so they pick up the configuration
    automatically.

Lifecycle invariants (verified by tests/test_logger_lifecycle.py):
  * `configure(); configure()` is idempotent and returns the same
    QueueListener.
  * `configure(); shutdown(); configure()` creates a NEW listener and
    leaves no orphan threads.
"""
from __future__ import annotations

import logging
import queue
from logging.handlers import QueueHandler, QueueListener
from typing import Optional

import src.config as config

_q: "queue.Queue[logging.LogRecord]" = queue.Queue(maxsize=10_000)
_listener: Optional[QueueListener] = None
_root: logging.Logger = logging.getLogger("gebco")


def configure() -> QueueListener:
    """Idempotently configure the application logger.

    Returns the active `QueueListener` so callers (e.g. lifespan) can
    keep a handle if they want to.
    """
    global _listener
    if _listener is not None:
        return _listener

    _root.setLevel(config.LOG_LEVEL)
    # Remove any prior handlers to avoid double-emission across
    # configure() / shutdown() cycles in test fixtures.
    for h in list(_root.handlers):
        _root.removeHandler(h)
    _root.addHandler(QueueHandler(_q))

    sink = logging.StreamHandler()
    sink.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    _listener = QueueListener(_q, sink, respect_handler_level=True)
    _listener.start()
    return _listener


def shutdown() -> None:
    """Stop the background listener thread and clear the global handle.

    Safe to call multiple times. Safe to call before `configure()`.
    """
    global _listener
    if _listener is None:
        return
    try:
        _listener.stop()
    finally:
        _listener = None


# Re-export the logger for convenience.
logger = _root
