from fastapi.testclient import TestClient

from patchbay import main


def make_client() -> TestClient:
    return TestClient(main.app)


def test_index_renders_new_task_form(db):
    r = make_client().get("/")
    assert r.status_code == 200
    assert "New task" in r.text
    assert 'name="prompt"' in r.text


def test_health_ok_when_dependencies_up(monkeypatch):
    monkeypatch.setattr(main, "check_redis", lambda: True)
    monkeypatch.setattr(main, "check_db", lambda: True)
    r = make_client().get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_health_503_when_redis_down(monkeypatch):
    monkeypatch.setattr(main, "check_redis", lambda: False)
    monkeypatch.setattr(main, "check_db", lambda: True)
    r = make_client().get("/health")
    assert r.status_code == 503
    body = r.json()
    assert body["ok"] is False and body["redis"] is False and body["db"] is True
