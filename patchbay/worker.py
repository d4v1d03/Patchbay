import socket

from celery import Celery

from patchbay.config import get_settings

settings = get_settings()

celery_app = Celery("patchbay", broker=settings.redis_url, backend=settings.redis_url)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    # long-running tasks: ack after completion, one at a time per worker
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    result_expires=3600,
    # a redelivered message must not be re-run while a copy is still executing
    broker_transport_options={"visibility_timeout": 6 * 3600},
    # periodic housekeeping; run `celery -A patchbay.worker beat` (or worker -B)
    beat_schedule={
        "maintenance": {
            "task": "patchbay.maintenance",
            "schedule": settings.maintenance_interval_s,
        },
    },
    task_routes={"patchbay.maintenance": {"queue": "maintenance"}},
    task_default_queue="runs",
)


@celery_app.task(name="patchbay.ping")
def ping(message: str = "pong") -> dict:
    return {"message": message, "worker": socket.gethostname()}


@celery_app.task(name="patchbay.run_session")
def run_session(session_id: str) -> str:
    from patchbay.db.engine import init_db
    from patchbay.jobs.runner import run_session as _run

    init_db()
    return _run(session_id)


@celery_app.task(name="patchbay.maintenance")
def maintenance() -> dict:
    from patchbay.db.engine import init_db
    from patchbay.jobs.maintenance import reap, sweep_stuck_sessions

    init_db()
    return {"reaped": reap(), "rescued": sweep_stuck_sessions()}
