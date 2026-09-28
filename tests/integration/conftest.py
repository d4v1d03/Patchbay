"""Integration tests talk to a real Docker daemon. They are marked `docker`
and skipped automatically when no daemon is reachable or the sandbox image
hasn't been built, so `pytest` stays green on a laptop without Docker."""

import pytest

from patchbay.config import get_settings


def _docker_ready() -> tuple[bool, str]:
    try:
        import docker

        client = docker.from_env()
        client.ping()
    except Exception as e:  # noqa: BLE001 — any failure means "no docker"
        return False, f"docker daemon unavailable: {e}"
    try:
        client.images.get(get_settings().sandbox_image)
    except Exception:
        return False, f"sandbox image {get_settings().sandbox_image} not built (make build-sandbox)"
    return True, ""


@pytest.fixture(scope="session", autouse=True)
def require_docker(request):
    ok, reason = _docker_ready()
    if not ok:
        pytest.skip(reason, allow_module_level=True)
