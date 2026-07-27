import logging
import os
import sys
from datetime import datetime


def setup_logger(name="MoneyPrinter", level=logging.INFO):
    """
    Configures a shared logger that writes to a unique timestamped file.
    """
    log_dir = "logs"
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    # Generate timestamped filename
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = f"money_printer_{timestamp}.log"

    # Create a custom logger
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False  # Prevent double logging

    # Check if handlers already exist (critical for shared process space)
    if logger.handlers:
        return logger

    # Create handlers
    file_path = os.path.join(log_dir, log_file)
    f_handler = logging.FileHandler(file_path, encoding="utf-8")
    f_handler.setLevel(level)

    # Create formatters and add it to handlers
    log_format = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    f_handler.setFormatter(log_format)

    # Add handlers to the logger
    logger.addHandler(f_handler)

    return logger


# Singleton Accessor
logger = setup_logger()


def get_active_log_path():
    """Absolute path of this process's active ``money_printer_*.log``.

    FR-0.3: every archive/cleanup sweep must whitelist this path so the
    process can never delete the log file it is currently writing to
    (2026-07-24 review: the startup sweep os.remove()'d its own log 27s
    after startup — 27 days of INFO went to a deleted inode).

    Returns None if the shared logger has no file handler (should not
    happen in normal operation).
    """
    for handler in logger.handlers:
        if isinstance(handler, logging.FileHandler):
            return os.path.abspath(handler.baseFilename)
    return None


def configure_root_logging(level=logging.INFO):
    """Attach the shared file handler to the ROOT logger (FR-0.3).

    Module-level loggers (``logging.getLogger(__name__)`` in strategies,
    bots, mixins, and providers) previously had no handler in the runtime
    entrypoints, so all their INFO output was silently dropped: only the
    named "MoneyPrinter" logger had a FileHandler. Both entrypoints
    (run_dashboard.py, run_web_dashboard.py) call this at startup.

    Topology after this call (no duplicate lines by construction):
      - root logger: level INFO, holds the SAME FileHandler instance as the
        "MoneyPrinter" logger -> module-level records propagate to root and
        are written to the active money_printer_*.log exactly once.
      - "MoneyPrinter" logger: unchanged (propagate=False, own handler) ->
        its records hit the shared handler exactly once and never reach root.
      - console: no console handler is added here; any pre-existing console
        (stdout/stderr) handler on root is raised to WARNING so the terminal
        dashboard UI is not flooded with INFO lines.

    Idempotent: safe to call from every entrypoint and repeatedly.
    """
    root = logging.getLogger()
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)

    file_handler = None
    for handler in logger.handlers:
        if isinstance(handler, logging.FileHandler):
            file_handler = handler
            break
    if file_handler is not None and file_handler not in root.handlers:
        root.addHandler(file_handler)

    # Keep any real console handler at WARNING+ (never touch non-console
    # StreamHandlers such as test-framework capture handlers).
    for handler in root.handlers:
        if isinstance(handler, logging.StreamHandler) and not isinstance(
            handler, logging.FileHandler
        ):
            stream = getattr(handler, "stream", None)
            if stream in (sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__):
                if handler.level < logging.WARNING:
                    handler.setLevel(logging.WARNING)

    # Third-party INFO chatter would otherwise flood the file via root.
    for noisy in ("urllib3", "requests", "httpx", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return root
