from __future__ import annotations

import os
import logging
import threading
import weakref
from logging.handlers import RotatingFileHandler
from time import strftime
from typing import Protocol

from .models import OptionalLogCallback


def format_log_line(message: str) -> str:
    timestamp = strftime("%Y-%m-%d %H:%M:%S")
    return f"[{timestamp}] {message}"


class LoggerProtocol(Protocol):
    def log(self, message: str) -> None: ...


class EventLogger:
    _handlers = weakref.WeakValueDictionary()
    _lock = threading.Lock()

    def __init__(self, log_file: str, log_callback: OptionalLogCallback = None) -> None:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        self.log_file = log_file
        self.log_callback = log_callback
        with self._lock:
            handler = self._handlers.get(os.path.abspath(log_file))
            if handler is None:
                handler = RotatingFileHandler(
                    log_file, maxBytes=1024 * 1024, backupCount=3, encoding="utf-8"
                )
                handler.setFormatter(logging.Formatter("%(message)s"))
                self._handlers[os.path.abspath(log_file)] = handler
            self._handler = handler

    def log(self, message: str) -> None:
        line = format_log_line(message)
        self._handler.handle(
            logging.LogRecord("reward", logging.INFO, "", 0, line, (), None)
        )
        if self.log_callback is not None:
            self.log_callback(line)

    def close(self) -> None:
        with self._lock:
            self._handler.close()
            self._handlers.pop(os.path.abspath(self.log_file), None)


class ScopedLogger:
    def __init__(self, logger: LoggerProtocol, scope: str) -> None:
        self.logger = logger
        self.scope = scope

    def log(self, message: str) -> None:
        self.logger.log(f"{self.scope} {message}")
