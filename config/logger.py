"""Centralised logging setup.

Call `setup_logging()` once at the start of a run; use `logging.getLogger(__name__)`
everywhere else.

Console shows INFO and above. A timestamped file under `logs/` captures everything,
which is what you actually read when an agent misbehaves halfway through a pipeline.
"""

import logging
import logging.handlers
import sys
from datetime import datetime
from pathlib import Path

LOG_FORMAT = "[%(asctime)s | %(name)s | %(levelname)s | %(funcName)s]: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_configured = False


def setup_logging(
    level: int | str = logging.INFO,
    log_dir: str | Path = "logs",
    console: bool = True,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
) -> Path:
    """Configure the root logger once and return the log file it writes to.

    Repeat calls are ignored rather than stacking duplicate handlers, which is what
    made the legacy version emit every line twice when a notebook cell was re-run.
    """
    global _configured

    root = logging.getLogger()
    if _configured:
        return _current_log_file(root)

    root.setLevel(level)
    root.handlers.clear()
    formatter = logging.Formatter(fmt=LOG_FORMAT, datefmt=DATE_FORMAT)

    if console:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(level)
        console_handler.setFormatter(formatter)
        root.addHandler(console_handler)

    log_dir_path = Path(log_dir)
    log_dir_path.mkdir(parents=True, exist_ok=True)
    file_path = log_dir_path / f"run_{datetime.now():%Y-%m-%d_%H-%M-%S}.log"

    file_handler = logging.handlers.RotatingFileHandler(
        file_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    _configured = True
    return file_path


def _current_log_file(root: logging.Logger) -> Path:
    for handler in root.handlers:
        if isinstance(handler, logging.handlers.RotatingFileHandler):
            return Path(handler.baseFilename)
    return Path()
