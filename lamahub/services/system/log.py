"""Logging service for the Lamahub application."""

import os
import re
import logging
import logging.handlers
from collections import deque

from lamahub.env import env

# Matches a formatted log line: "[2026-07-08 21:04:15] INFO in module.func: message"
_LINE_RE = re.compile(
    r"^\[(?P<time>[\d\-: ]+)\] (?P<level>\w+) in (?P<source>[\w.]+): (?P<message>.*)$"
)


class Log:
    """Application logger with console and rotating file output."""

    def __init__(self):
        """Initialize the logger instance."""
        self.log = logging.getLogger("lamahub_log")
        self.logfile: str | None = None

        level = getattr(logging, env.log_level, logging.INFO)
        self.log.setLevel(level)

    def init_lamahub(self, instance_path: str):
        """Initialize logging handlers with file and console output.

        Args:
            instance_path: Directory path for the log file.

        Returns:
            The configured logger instance.
        """
        logfile = os.path.join(instance_path, "lamahub.log")
        self.logfile = logfile

        if not self.log.hasHandlers():
            formatter = logging.Formatter(
                "[%(asctime)s] %(levelname)s in %(module)s.%(funcName)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
            )

            console_handler = logging.StreamHandler()
            console_handler.setFormatter(formatter)

            rotation_handler = logging.handlers.RotatingFileHandler(logfile, maxBytes=5 * 1024 * 1024, backupCount=2)
            rotation_handler.setFormatter(formatter)

            self.log.addHandler(console_handler)
            self.log.addHandler(rotation_handler)

        return self.log

    def info(self, message):
        """Log an info message."""
        self.log.info(message)

    def debug(self, message):
        """Log a debug message."""
        self.log.debug(message)

    def error(self, message):
        """Log an error message."""
        self.log.error(message)

    def warning(self, message):
        """Log a warning message."""
        self.log.warning(message)

    def read_entries(self, limit: int = 500) -> list[dict]:
        """Return the most recent parsed log entries, oldest first.

        Continuation lines (e.g. tracebacks) are appended to the preceding
        entry's message so multi-line records stay intact. Lines that do not
        match the expected format are kept as raw ``message`` entries.
        """
        if not self.logfile or not os.path.isfile(self.logfile):
            return []

        # Keep only the tail in memory; the log file rotates at a few MB.
        with open(self.logfile, encoding="utf-8", errors="replace") as handle:
            lines = deque(handle, maxlen=limit)

        entries: list[dict] = []
        for line in lines:
            line = line.rstrip("\n")
            match = _LINE_RE.match(line)
            if match:
                entries.append(
                    {
                        "time": match["time"],
                        "level": match["level"].upper(),
                        "source": match["source"],
                        "message": match["message"],
                    }
                )
            elif entries:
                entries[-1]["message"] += "\n" + line
            elif line:
                entries.append({"time": "", "level": "", "source": "", "message": line})

        return entries
