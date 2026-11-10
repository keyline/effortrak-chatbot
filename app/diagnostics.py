"""Bounded, metadata-only application diagnostics for the single-worker service."""

import json
import logging
from logging.handlers import RotatingFileHandler
import time
import traceback
from pathlib import Path


class DiagnosticFormatter(logging.Formatter):
    def format(self, record):
        entry = {
            'time': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(record.created)),
            'level': record.levelname,
            'pid': record.process,
            'logger': record.name,
            'event': record.getMessage(),
        }
        if record.exc_info and record.exc_info[1] is not None:
            # Exception messages and source lines may contain credentials or report data.
            entry['exception_type'] = type(record.exc_info[1]).__name__
            entry['stack'] = [
                {'file': Path(frame.filename).name, 'line': frame.lineno, 'function': frame.name}
                for frame in traceback.extract_tb(record.exc_info[2])
            ]
        return json.dumps(entry, ensure_ascii=True)


def configure_logging(path: Path):
    logger = logging.getLogger('app')
    path = path.resolve()
    # App factories are also used repeatedly by tests; do not duplicate handlers.
    for handler in logger.handlers:
        if isinstance(handler, RotatingFileHandler) and handler.baseFilename == str(path):
            return
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=5,
                                  encoding='utf-8')
    handler.setFormatter(DiagnosticFormatter())
    for old in list(logger.handlers):
        if getattr(old, '_effortrak_diagnostics', False):
            logger.removeHandler(old)
            old.close()
    handler._effortrak_diagnostics = True
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
