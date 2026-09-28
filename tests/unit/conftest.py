import fakeredis
import pytest

from patchbay.config import get_settings
from patchbay.db import engine as engine_mod
from patchbay.events import bus as bus_mod


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/test.db")
    get_settings.cache_clear()
    engine_mod.reset_engine()
    engine_mod.init_db()
    yield
    engine_mod.reset_engine()
    get_settings.cache_clear()


@pytest.fixture
def bus(db):
    b = bus_mod.EventBus(fakeredis.FakeRedis())
    bus_mod.set_bus(b)
    yield b
    bus_mod.set_bus(None)


@pytest.fixture(autouse=True)
def projects_dir(tmp_path, monkeypatch):
    from patchbay.jobs import runner

    monkeypatch.setattr(runner, "PROJECTS_DIR", tmp_path / "projects")
    return tmp_path / "projects"
