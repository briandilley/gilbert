"""Backup service — daily rolling tar.gz backups of the .gilbert/ data directory.

Creates compressed archives on a configurable schedule and prunes
old backups beyond a retention window.
"""

from __future__ import annotations

import asyncio
import logging
import tarfile
import time
from datetime import UTC, datetime
from pathlib import Path

from typing import Any

from gilbert.config import DATA_DIR
from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.tools import ToolParameterType

logger = logging.getLogger(__name__)

_BACKUPS_DIR = DATA_DIR / "backups"


class BackupService(Service):
    """Creates daily rolling tar.gz backups of the .gilbert/ data directory.

    Capabilities: backup
    """

    def __init__(self) -> None:
        self._retention_days: int = 30
        self._backup_hour: int = 3
        self._backup_minute: int = 0
        self._resolver: ServiceResolver | None = None

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="backup",
            capabilities=frozenset({"backup"}),
            requires=frozenset({"scheduler"}),
        )

    async def start(self, resolver: ServiceResolver) -> None:
        self._resolver = resolver

        # Load config
        config_svc = resolver.get_capability("configuration")
        if config_svc is not None:
            from gilbert.core.services.configuration import ConfigurationService

            if isinstance(config_svc, ConfigurationService):
                section = config_svc.get_section("backup")
                self._retention_days = int(section.get("retention_days", 30))
                self._backup_hour = int(section.get("backup_hour", 3))
                self._backup_minute = int(section.get("backup_minute", 0))

        # Ensure backups directory exists
        _BACKUPS_DIR.mkdir(parents=True, exist_ok=True)

        # Register daily backup job with the scheduler
        from gilbert.core.services.scheduler import SchedulerService
        from gilbert.interfaces.scheduler import Schedule

        scheduler = resolver.require_capability("scheduler")
        if isinstance(scheduler, SchedulerService):
            scheduler.add_job(
                name="backup.daily",
                schedule=Schedule.daily_at(
                    hour=self._backup_hour,
                    minute=self._backup_minute,
                ),
                callback=self._run_backup,
                system=True,
            )

        logger.info(
            "Backup service started (daily at %02d:%02d, retain %d days)",
            self._backup_hour,
            self._backup_minute,
            self._retention_days,
        )

    async def stop(self) -> None:
        pass

    # --- Configurable protocol ---

    @property
    def config_namespace(self) -> str:
        return "backup"

    @property
    def config_category(self) -> str:
        return "Infrastructure"

    def config_params(self) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="enabled", type=ToolParameterType.BOOLEAN,
                description="Whether the backup service is enabled.",
                default=False, restart_required=True,
            ),
            ConfigParam(
                key="retention_days", type=ToolParameterType.INTEGER,
                description="Number of days to retain backups.",
                default=30,
            ),
            ConfigParam(
                key="backup_hour", type=ToolParameterType.INTEGER,
                description="Hour of day to run backup (0-23).",
                default=3, restart_required=True,
            ),
            ConfigParam(
                key="backup_minute", type=ToolParameterType.INTEGER,
                description="Minute of hour to run backup (0-59).",
                default=0, restart_required=True,
            ),
        ]

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        self._retention_days = int(config.get("retention_days", self._retention_days))

    async def _run_backup(self) -> None:
        """Scheduler callback — create archive and prune old backups."""
        timestamp = datetime.now(UTC).strftime("%Y-%m-%d_%H%M%S")
        archive_name = f"backup-{timestamp}.tar.gz"
        archive_path = _BACKUPS_DIR / archive_name

        logger.info("Starting backup: %s", archive_name)
        start = time.monotonic()

        try:
            await asyncio.to_thread(self._create_archive, archive_path)
            elapsed = time.monotonic() - start
            size_mb = archive_path.stat().st_size / (1024 * 1024)
            logger.info(
                "Backup complete: %s (%.1f MB, %.1fs)",
                archive_name,
                size_mb,
                elapsed,
            )
        except Exception:
            logger.exception("Backup failed: %s", archive_name)
            return

        self._prune_old_backups()

    def _create_archive(self, archive_path: Path) -> None:
        """Create a tar.gz archive of the .gilbert/ directory.

        Excludes the backups directory itself to avoid recursive growth.
        """
        backups_abs = _BACKUPS_DIR.resolve()

        def _filter(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo | None:
            # Exclude the backups directory
            member_path = Path(tarinfo.name).resolve()
            try:
                member_path.relative_to(backups_abs)
                return None  # inside backups dir — skip
            except ValueError:
                pass
            return tarinfo

        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(str(DATA_DIR), arcname=DATA_DIR.name, filter=_filter)

    def _prune_old_backups(self) -> None:
        """Delete backup archives older than the retention period."""
        if not _BACKUPS_DIR.exists():
            return

        cutoff = time.time() - (self._retention_days * 86400)
        pruned = 0

        for path in _BACKUPS_DIR.glob("backup-*.tar.gz"):
            if path.stat().st_mtime < cutoff:
                path.unlink()
                pruned += 1
                logger.debug("Pruned old backup: %s", path.name)

        if pruned:
            logger.info("Pruned %d old backup(s)", pruned)
