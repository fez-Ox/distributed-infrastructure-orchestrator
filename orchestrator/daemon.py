"""Non-interactive service wrapper for the orchestrator CLI.

The CLI remains useful over a terminal, while VM1 can accept newline-delimited
commands through a durable input file without receiving EOF on an idle service.
The reader starts at the end of the file, so service restarts never replay old
commands.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path


class PollingInput:
    def __init__(self, path: str, poll_seconds: float = 0.25) -> None:
        self.path = Path(path)
        self.poll_seconds = poll_seconds
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        self._file = self.path.open("r", encoding="utf-8")
        self._file.seek(0, os.SEEK_END)

    def readline(self, _size: int = -1) -> str:
        while True:
            line = self._file.readline()
            if line:
                return line
            time.sleep(self.poll_seconds)

    def isatty(self) -> bool:
        return False

    def close(self) -> None:
        self._file.close()


def main() -> None:
    input_path = os.environ.get("MAO_INPUT_PATH", "./data/orchestrator.input")
    stream = PollingInput(input_path)
    sys.stdin = stream  # type: ignore[assignment]
    try:
        from orchestrator.agent import main as agent_main
        agent_main()
    finally:
        stream.close()


if __name__ == "__main__":
    main()
