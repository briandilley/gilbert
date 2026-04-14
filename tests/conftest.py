"""Shared test fixtures."""

from pathlib import Path
from typing import AsyncGenerator

import pytest

from gilbert.core.events import InMemoryEventBus
from gilbert.core.registry import ServiceRegistry
from gilbert.core.service_manager import ServiceManager
from gilbert.storage.sqlite import SQLiteStorage


@pytest.fixture
def service_registry() -> ServiceRegistry:
    return ServiceRegistry()


@pytest.fixture
def service_manager() -> ServiceManager:
    return ServiceManager()


@pytest.fixture
def event_bus() -> InMemoryEventBus:
    return InMemoryEventBus()


@pytest.fixture
async def sqlite_storage(tmp_path: Path) -> AsyncGenerator[SQLiteStorage, None]:
    db_path = tmp_path / "test.db"
    storage = SQLiteStorage(str(db_path))
    await storage.initialize()
    yield storage
    await storage.close()
