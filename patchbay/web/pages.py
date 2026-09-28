import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse

from patchbay.db.engine import db_session
from patchbay.db.models import Session
from patchbay.events.bus import get_bus
from patchbay.web.templating import templates

router = APIRouter()


@router.get("/", include_in_schema=False)
def index(request: Request):
    with db_session() as db:
        sessions = [
            s.to_dict() for s in db.query(Session).order_by(Session.created_at.desc()).limit(30)
        ]
    return templates.TemplateResponse(request, "index.html", {"sessions": sessions})


@router.get("/sessions/{session_id}", include_in_schema=False)
def session_page(request: Request, session_id: str):
    with db_session() as db:
        sess = db.get(Session, session_id)
        if sess is None:
            raise HTTPException(404, "no such session")
        data = sess.to_dict()
    events = get_bus().replay(session_id)
    from patchbay.jobs.runner import project_zip_path

    return templates.TemplateResponse(
        request,
        "session.html",
        {
            "s": data,
            "events": events,
            "last_id": events[-1]["id"] if events else 0,
            "project_ready": project_zip_path(session_id).is_file(),
        },
    )


@router.get("/sessions/{session_id}/diff.patch", include_in_schema=False)
def session_patch(session_id: str):
    from patchbay.web.api import get_diff_data

    data = get_diff_data(session_id)
    return PlainTextResponse(
        data["patch"],
        media_type="text/x-patch",
        headers={"Content-Disposition": f'attachment; filename="{session_id}.patch"'},
    )


@router.get("/sessions/{session_id}/project.zip", include_in_schema=False)
def session_project(session_id: str):
    from patchbay.jobs.runner import project_zip_path

    path = project_zip_path(session_id)
    if not re.fullmatch(r"[\w-]+", session_id) or not path.is_file():
        raise HTTPException(404, "no project saved yet; it's saved when a run ends")
    return FileResponse(path, media_type="application/zip", filename=f"{session_id}.zip")
