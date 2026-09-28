def enqueue_run(session_id: str) -> None:
    from patchbay.worker import celery_app

    celery_app.send_task("patchbay.run_session", args=[session_id])
