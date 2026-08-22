"""Run identity, provenance manifest and atomic file publication.

Every pipeline run gets an id and writes a manifest recording what produced
the outputs: code revision, config, universe, data-as-of date and thresholds.
When a screen result looks surprising weeks later, the manifest is what makes
it explainable.
"""

import json
import os
import subprocess
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

import pandas as pd


def new_run_id() -> str:
    """Sortable, unique run identifier: ``20260822T164500Z-a1b2c3d4``."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def code_revision(project_root: str) -> str:
    """Current git revision, or 'unknown' outside a repository."""
    try:
        result = subprocess.run(
            ["git", "-C", project_root, "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            revision = result.stdout.strip()
            dirty = subprocess.run(
                ["git", "-C", project_root, "status", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return f"{revision}-dirty" if dirty.stdout.strip() else revision
    except Exception:
        pass
    return "unknown"


@dataclass
class RunManifest:
    """Provenance record for one pipeline run."""

    run_id: str
    stage: str
    exchange: str
    instrument_type: str
    started_at: str
    code_revision: str
    finished_at: str | None = None
    status: str = "running"
    data_as_of: str | None = None
    universe_file: str | None = None
    universe_total: int | None = None
    universe_screened: int | None = None
    provider: str = "yahoo_finance"
    thresholds: dict[str, Any] = field(default_factory=dict)
    counts: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def finish(self, status: str = "success", error: str | None = None) -> None:
        self.finished_at = datetime.now(UTC).isoformat()
        self.status = status
        self.error = error

    def write(self, path: str) -> str:
        """Write the manifest atomically as JSON."""
        payload = json.dumps(asdict(self), indent=2, default=str)
        atomic_write_text(payload, path)
        return path


def _temp_path(directory: str, path: str) -> str:
    """A temp filename unique to this process and call.

    A fixed ``.<name>.tmp`` lets two concurrent runs writing the same output
    remove or overwrite each other's partial file, so the rename is no longer
    atomic with respect to the other run.
    """
    unique = f"{os.getpid()}.{uuid.uuid4().hex[:8]}"
    return os.path.join(directory, f".{os.path.basename(path)}.{unique}.tmp")


def read_manifest(path: str) -> dict | None:
    """Load a manifest, returning None when it is absent or unreadable."""
    if not os.path.exists(path):
        return None
    try:
        with open(path) as handle:
            return json.load(handle)
    except (json.JSONDecodeError, OSError):
        return None


def atomic_write_text(text: str, path: str) -> str:
    """Write text via a temporary file and rename into place.

    A direct write leaves a truncated file behind if the process dies partway;
    rename within a filesystem is atomic, so readers see either the old file
    or the complete new one.
    """
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    temp_path = _temp_path(directory, path)
    with open(temp_path, "w") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)
    return path


def atomic_write_csv(df: pd.DataFrame, path: str) -> str:
    """Write a DataFrame to CSV atomically, preserving the header when empty."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    temp_path = _temp_path(directory, path)
    df.to_csv(temp_path, index=False)
    os.replace(temp_path, path)
    return path
