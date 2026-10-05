"""Small filesystem helpers."""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

ATTEMPTS = 6        # tries to replace the target before giving up
FIRST_WAIT = 0.05   # seconds; doubles after every failed try (0.05, 0.1, 0.2, 0.4, 0.8)


def atomic_write_json(path: Path, data, attempts: int = ATTEMPTS, sleep=time.sleep) -> None:
    """Write JSON to a temp file in the same directory, then os.replace it over the target.

    On Windows the replace fails with PermissionError while another program (antivirus, indexer, a cloud sync
    client, a preview pane) has the target open for a moment, so that is retried with growing waits."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        for attempt in range(attempts):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == attempts - 1:
                    raise
                sleep(FIRST_WAIT * 2**attempt)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
