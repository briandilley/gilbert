"""Output file management — shared utilities for service-generated files."""

import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Base directory for transient service output
OUTPUT_DIR = Path(".gilbert/output")

# Default TTL in seconds (1 hour)
DEFAULT_TTL_SECONDS = 3600


def get_output_dir(service_name: str) -> Path:
    """Get (and create) the output directory for a service."""
    path = OUTPUT_DIR / service_name
    path.mkdir(parents=True, exist_ok=True)
    return path


def cleanup_old_files(directory: Path, max_age_seconds: int) -> int:
    """Delete files in directory older than max_age_seconds.

    Returns the number of files deleted.
    """
    if not directory.is_dir():
        return 0

    now = time.time()
    deleted = 0
    for file in directory.iterdir():
        if not file.is_file():
            continue
        age = now - file.stat().st_mtime
        if age > max_age_seconds:
            try:
                file.unlink()
                deleted += 1
            except OSError:
                logger.warning("Failed to delete expired output file: %s", file)

    if deleted:
        logger.debug("Cleaned up %d expired files from %s", deleted, directory)

    return deleted
