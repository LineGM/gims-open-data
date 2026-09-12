"""Exclusive create: never steals a live or stale lock automatically."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from uuid import uuid4

from .client import ImportStopped


class SyncLock:
    def __init__(self, root: Path) -> None:
        self.path = root / "state/sync.lock"
        self.token = str(uuid4())

    def __enter__(self) -> "SyncLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.path.open("x", encoding="utf-8") as stream:
                json.dump(
                    {
                        "pid": os.getpid(),
                        "started_at": datetime.now(UTC).isoformat(),
                        "owner": self.token,
                    },
                    stream,
                )
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            raise ImportStopped(
                "Sync lock exists. Check PID in state/sync.lock; if the process has ended, "
                "remove that stale lock explicitly before retrying."
            ) from None
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self.path.exists():
            owner = json.loads(self.path.read_text(encoding="utf-8"))
            if owner.get("owner") == self.token:
                self.path.unlink()
