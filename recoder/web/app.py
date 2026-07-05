"""FastAPI application for the Recoder web UI (spec §4.3).

Single-page app served from ``static/index.html`` plus a small JSON API. All
capture/pipeline state lives in a :class:`RecordingManager`; the routes here are
thin adapters over it and the meeting store, with strict validation of meeting
names and frame paths (no arbitrary filesystem access).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from recoder.analysis.action_items import extract_action_items
from recoder.config import Config
from recoder.store import Meeting, MeetingStore
from recoder.web.recording import RecordingManager

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_INDEX = _STATIC_DIR / "index.html"


class StartBody(BaseModel):
    title: str | None = None
    context_note: str | None = None


class ReprocessBody(BaseModel):
    context_note: str | None = None


def create_app(config: Config, manager: RecordingManager | None = None) -> FastAPI:
    app = FastAPI(title="Recoder", docs_url=None, redoc_url=None)
    manager = manager or RecordingManager(config)
    store: MeetingStore = manager.store

    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # -- helpers ------------------------------------------------------------

    def _resolve_meeting(name: str) -> Meeting:
        """Load a meeting by folder name, validated against the store list.

        Prevents path traversal / arbitrary folder access: only names returned
        by :meth:`MeetingStore.list_meetings` are ever loaded.
        """
        for meeting in store.list_meetings():
            if meeting.folder.name == name:
                return meeting
        raise HTTPException(status_code=404, detail="unknown meeting")

    def _read_text(path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

    def _frame_files(meeting: Meeting) -> list[str]:
        frames_dir = meeting.frames_dir
        if not frames_dir.exists():
            return []
        return sorted(
            p.name
            for p in frames_dir.iterdir()
            if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png"}
        )

    # -- page ---------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        if _INDEX.exists():
            return HTMLResponse(_INDEX.read_text(encoding="utf-8"))
        return HTMLResponse("<h1>Recoder</h1><p>UI not built.</p>")

    # -- recording API ------------------------------------------------------

    @app.post("/api/record/start")
    def record_start(body: StartBody) -> dict:
        try:
            meeting = manager.start(body.title, body.context_note)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"folder": meeting.folder.name}

    @app.post("/api/record/stop")
    def record_stop() -> dict:
        try:
            meeting = manager.stop()
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"folder": meeting.folder.name}

    @app.get("/api/status")
    def status() -> dict:
        return manager.status()

    # -- archive API --------------------------------------------------------

    @app.get("/api/meetings")
    def meetings() -> list[dict]:
        result: list[dict] = []
        for meeting in store.list_meetings():
            try:
                meta = meeting.read_meta()
            except OSError:
                continue
            result.append(
                {
                    "folder": meeting.folder.name,
                    "title": meta.get("title"),
                    "date": meta.get("started_at"),
                    "state": meta.get("state"),
                    "has_summary": meeting.summary_md.exists(),
                }
            )
        return result

    @app.get("/api/meetings/{name}")
    def meeting_detail(name: str) -> dict:
        meeting = _resolve_meeting(name)
        meta = meeting.read_meta()
        summary = _read_text(meeting.summary_md)
        return {
            "folder": meeting.folder.name,
            "meta": meta,
            "transcript": _read_text(meeting.transcript_md),
            "summary": summary,
            "action_items": extract_action_items(summary),
            "frames": _frame_files(meeting),
        }

    @app.get("/api/meetings/{name}/frames/{file}")
    def meeting_frame(name: str, file: str) -> FileResponse:
        meeting = _resolve_meeting(name)
        frames_dir = meeting.frames_dir.resolve()
        target = (frames_dir / file).resolve()
        # Path-traversal guard: the resolved file must live directly in frames/.
        if target.parent != frames_dir:
            raise HTTPException(status_code=400, detail="invalid frame path")
        if not target.is_file():
            raise HTTPException(status_code=404, detail="frame not found")
        return FileResponse(str(target), media_type="image/jpeg")

    @app.post("/api/meetings/{name}/reprocess")
    def meeting_reprocess(name: str, body: ReprocessBody) -> dict:
        meeting = _resolve_meeting(name)
        updates: dict[str, object] = {}
        if body.context_note is not None:
            updates["context_note"] = body.context_note
        # Rewind to diarized so the runner redoes analyze + commit against the
        # (optionally) corrected context note.
        updates["state"] = "diarized"
        meeting.update_meta(**updates)
        manager.start_pipeline(meeting.folder)
        return {"folder": meeting.folder.name, "state": "diarized"}

    @app.post("/api/meetings/{name}/resume")
    def meeting_resume(name: str) -> dict:
        meeting = _resolve_meeting(name)
        # The runner's error -> predecessor logic reruns the failed stage.
        manager.start_pipeline(meeting.folder)
        return {"folder": meeting.folder.name}

    return app
