# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Structured (JSON lines) logging on top of the standard library.

Usage: ``logger.info("app.startup", extra={"app_env": "development"})``.
The message is a stable event name; context goes in ``extra`` and becomes
top-level JSON fields.
"""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any, TextIO

# Attributes every LogRecord has; anything else was passed via ``extra``.
# ``color_message`` is uvicorn's ANSI-coloured duplicate of the message.
_STANDARD_RECORD_ATTRS = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys()
    | {"message", "asctime", "color_message"}
)

# Uvicorn installs its own handlers; route them through ours instead.
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD_RECORD_ATTRS:
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str, stream: TextIO | None = None) -> None:
    """Send all application and uvicorn logs to ``stream`` (default stdout) as JSON lines."""
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)

    for name in _UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
