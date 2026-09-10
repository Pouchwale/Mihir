from __future__ import annotations

import logging
import logging.handlers
import sys

import structlog

from .config import get_settings


def setup_logging() -> None:
    s = get_settings()
    handlers: list[logging.Handler] = []
    # Replies contain Hindi, Gujarati and emoji. A Windows console or a redirected log file often
    # defaults to cp1252, where writing that text raises - and because the log line is written while
    # a message is being processed, the customer would get "service unavailable" instead of a reply.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    level = getattr(logging, s.log_level.upper(), logging.INFO)
    handlers.append(logging.StreamHandler(sys.stdout))
    # A long-running Windows service has nothing rotating its log for it, so do it here.
    if s.log_file:
        path = get_settings().resolve_path(s.log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                path, maxBytes=max(1, s.log_max_mb) * 1024 * 1024, backupCount=s.log_backups, encoding="utf-8"
            )
        )
    logging.basicConfig(format="%(message)s", level=level, handlers=handlers, force=True)
    for noisy in ("httpx", "httpcore", "apscheduler", "aiosqlite"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    renderer = structlog.dev.ConsoleRenderer() if s.is_dev else structlog.processors.JSONRenderer()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        # Route through stdlib logging so the rotating file handler above receives every line.
        logger_factory=structlog.stdlib.LoggerFactory() if s.log_file else structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
