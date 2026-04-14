"""Tests for BackupService — daily rolling backups of .gilbert/ directory."""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
import time

import pytest

from gilbert.core.services.backup import BackupService


from gilbert.core.services.scheduler import SchedulerService


class FakeScheduler(SchedulerService):
    """Captures add_job calls without running real scheduler logic."""

    def __init__(self) -> None:
        self.jobs: list[dict[str, Any]] = []

    def add_job(self, **kwargs: Any) -> MagicMock:  # type: ignore[override]
        self.jobs.append(kwargs)
        return MagicMock()


class FakeConfigService:
    """Returns config sections — satisfies ConfigurationReader protocol."""

    def __init__(self, sections: dict[str, dict[str, Any]] | None = None) -> None:
        self._sections = sections or {}

    def get(self, path: str) -> Any:
        parts = path.split(".", 1)
        section = self._sections.get(parts[0], {})
        return section.get(parts[1], None) if len(parts) > 1 else section

    def get_section(self, name: str) -> dict[str, Any]:
        return self._sections.get(name, {})

    def get_section_safe(self, name: str) -> dict[str, Any]:
        return self._sections.get(name, {})

    async def set(self, path: str, value: Any) -> dict[str, Any]:
        return {}

    def service_info(self) -> Any:
        from gilbert.interfaces.service import ServiceInfo
        return ServiceInfo(name="configuration", capabilities=frozenset({"configuration"}))


class FakeResolver:
    def __init__(self) -> None:
        self.caps: dict[str, Any] = {}

    def get_capability(self, cap: str) -> Any:
        return self.caps.get(cap)

    def require_capability(self, cap: str) -> Any:
        svc = self.caps.get(cap)
        if svc is None:
            raise LookupError(cap)
        return svc

    def get_all(self, cap: str) -> list[Any]:
        svc = self.caps.get(cap)
        return [svc] if svc else []


@pytest.fixture
def backup_service() -> BackupService:
    return BackupService()


@pytest.fixture
def scheduler() -> FakeScheduler:
    return FakeScheduler()


@pytest.fixture
def resolver(scheduler: FakeScheduler) -> FakeResolver:
    r = FakeResolver()
    r.caps["scheduler"] = scheduler
    return r


class TestBackupServiceInfo:
    def test_service_info(self, backup_service: BackupService) -> None:
        info = backup_service.service_info()
        assert info.name == "backup"
        assert "backup" in info.capabilities
        assert "scheduler" in info.requires


class TestBackupStart:
    @pytest.mark.asyncio
    async def test_registers_daily_job(
        self, backup_service: BackupService, resolver: FakeResolver, scheduler: FakeScheduler
    ) -> None:
        await backup_service.start(resolver)
        assert len(scheduler.jobs) == 1
        job = scheduler.jobs[0]
        assert job["name"] == "backup.daily"
        assert job["system"] is True

    @pytest.mark.asyncio
    async def test_reads_config(
        self, backup_service: BackupService, scheduler: FakeScheduler
    ) -> None:
        config_svc = FakeConfigService({"backup": {
            "enabled": True,
            "retention_days": 7,
            "backup_hour": 5,
            "backup_minute": 30,
        }})
        resolver = FakeResolver()
        resolver.caps["scheduler"] = scheduler
        resolver.caps["configuration"] = config_svc

        await backup_service.start(resolver)

        assert backup_service._retention_days == 7
        assert backup_service._backup_hour == 5
        assert backup_service._backup_minute == 30


class TestCreateArchive:
    def test_creates_tar_gz(self, backup_service: BackupService, tmp_path: Path) -> None:
        """Test that _create_archive creates a tar.gz file."""
        # Create a fake .gilbert directory
        data_dir = tmp_path / ".gilbert"
        data_dir.mkdir()
        (data_dir / "test.txt").write_text("hello")
        (data_dir / "backups").mkdir()
        (data_dir / "backups" / "old.tar.gz").write_text("old")

        archive_path = tmp_path / "test-backup.tar.gz"

        with patch("gilbert.core.services.backup.DATA_DIR", data_dir), \
             patch("gilbert.core.services.backup._BACKUPS_DIR", data_dir / "backups"):
            backup_service._create_archive(archive_path)

        assert archive_path.exists()
        assert archive_path.stat().st_size > 0

        # Verify the archive contents
        import tarfile
        with tarfile.open(archive_path, "r:gz") as tar:
            names = tar.getnames()
            # Should include test.txt but not backups/
            assert any("test.txt" in n for n in names)


class TestPruneOldBackups:
    def test_prunes_old_files(self, backup_service: BackupService, tmp_path: Path) -> None:
        """Test that old backups are pruned."""
        backups_dir = tmp_path / "backups"
        backups_dir.mkdir()

        # Create an "old" backup file (set mtime to 60 days ago)
        old_file = backups_dir / "backup-2020-01-01_030000.tar.gz"
        old_file.write_text("old")
        old_mtime = time.time() - (60 * 86400)
        import os
        os.utime(old_file, (old_mtime, old_mtime))

        # Create a "recent" backup
        new_file = backups_dir / "backup-2026-04-01_030000.tar.gz"
        new_file.write_text("new")

        backup_service._retention_days = 30

        with patch("gilbert.core.services.backup._BACKUPS_DIR", backups_dir):
            backup_service._prune_old_backups()

        assert not old_file.exists()
        assert new_file.exists()

    def test_keeps_files_within_retention(self, backup_service: BackupService, tmp_path: Path) -> None:
        """Test that recent backups are kept."""
        backups_dir = tmp_path / "backups"
        backups_dir.mkdir()

        recent_file = backups_dir / "backup-2026-04-05_030000.tar.gz"
        recent_file.write_text("recent")

        backup_service._retention_days = 30

        with patch("gilbert.core.services.backup._BACKUPS_DIR", backups_dir):
            backup_service._prune_old_backups()

        assert recent_file.exists()
