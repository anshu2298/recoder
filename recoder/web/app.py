"""FastAPI application for the Recoder web UI (spec §4.3).

Single-page app served from ``static/index.html`` plus a small JSON API. All
capture/pipeline state lives in a :class:`RecordingManager`; the routes here are
thin adapters over it and the meeting store, with strict validation of meeting
names and frame paths (no arbitrary filesystem access).
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

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

    @app.get("/api/register")
    def register() -> dict:
        """Live rollup of the configured worktree family (computed on read)."""
        from recoder.analysis.register import build_register, render_register_md

        def _commit(entry) -> dict:
            return {
                "cid": entry.cid,
                "ts": entry.ts,
                "branch": entry.branch,
                "title": entry.title,
                "what": entry.what,
                "why": entry.why,
                "files": entry.files,
                "next": entry.next_step,
                "score": entry.score,
            }

        trees = build_register(config)
        return {
            "markdown": render_register_md(trees),
            "trees": [
                {
                    "name": t.name,
                    "last_active": t.last_active,
                    "stale_days": t.stale_days,
                    "focus": t.current_focus,
                    "next": t.next_step,
                    "task": _commit(t.task) if t.task else None,
                    "recent": [_commit(c) for c in t.recent],
                    "git": (
                        {
                            "is_repo": t.git.is_repo,
                            "repo": t.git.repo,
                            "branch": t.git.branch,
                            "detached": t.git.detached,
                            "upstream": t.git.upstream,
                            "ahead": t.git.ahead,
                            "behind": t.git.behind,
                            "dirty": t.git.dirty,
                            "head": t.git.head,
                            "commits": t.git.commits,
                        }
                        if t.git is not None
                        else None
                    ),
                    "milestones": [
                        {"ts": ts, "text": text} for ts, text in t.milestones
                    ],
                    "stores": [
                        {"path": str(s.path), "exists": s.exists}
                        for s in t.stores
                    ],
                }
                for t in trees
            ],
        }

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
        from recoder.analysis.spec import load_action_items

        meeting = _resolve_meeting(name)
        meta = meeting.read_meta()
        summary = _read_text(meeting.summary_md)
        return {
            "folder": meeting.folder.name,
            "meta": meta,
            "transcript": _read_text(meeting.transcript_md),
            "summary": summary,
            # Structured items (action-items.json) with table fallback inside.
            "action_items": load_action_items(meeting.folder),
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

    # -- todos + specs -------------------------------------------------------

    @app.get("/api/todos")
    def todos() -> list[dict]:
        """Action items across all summarized meetings, newest meeting first."""
        from recoder.analysis.spec import load_action_items, spec_status

        result: list[dict] = []
        for meeting in store.list_meetings():
            if not meeting.summary_md.exists():
                continue
            try:
                meta = meeting.read_meta()
            except OSError:
                continue
            items = load_action_items(meeting.folder)
            if not items:
                continue
            result.append(
                {
                    "folder": meeting.folder.name,
                    "title": meta.get("title"),
                    "date": meta.get("started_at"),
                    "items": [
                        {
                            **item,
                            "spec": spec_status(
                                meeting.folder, str(item.get("id"))
                            ),
                        }
                        for item in items
                    ],
                }
            )
        return result

    _ITEM_ID_RE = r"^[A-Za-z0-9_-]{1,32}$"

    def _validate_item_id(item_id: str) -> str:
        import re as _re

        if not _re.fullmatch(_ITEM_ID_RE, item_id):
            raise HTTPException(status_code=400, detail="invalid item id")
        return item_id

    @app.get("/api/meetings/{name}/items/{item_id}/spec")
    def item_spec(name: str, item_id: str) -> dict:
        from recoder.analysis.spec import spec_status

        meeting = _resolve_meeting(name)
        item_id = _validate_item_id(item_id)
        status = spec_status(meeting.folder, item_id)
        content = None
        if status.get("status") == "done":
            content = _read_text(meeting.folder / "specs" / f"{item_id}.md")
        return {**status, "content": content}

    @app.post("/api/meetings/{name}/items/{item_id}/spec")
    def item_spec_generate(name: str, item_id: str) -> dict:
        """Kick off spec generation in a DETACHED child (survives app close)."""
        import subprocess
        import sys

        from recoder.analysis.spec import load_action_items, spec_status

        meeting = _resolve_meeting(name)
        item_id = _validate_item_id(item_id)
        if not any(
            i.get("id") == item_id for i in load_action_items(meeting.folder)
        ):
            raise HTTPException(status_code=404, detail="unknown action item")
        status = spec_status(meeting.folder, item_id)
        if status.get("status") in ("running", "done"):
            return status

        creationflags = 0
        if os.name == "nt":
            creationflags = (
                subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        log_path = meeting.folder / "specs" / f"{item_id}.log"
        log_path.parent.mkdir(exist_ok=True)
        with log_path.open("ab") as log:
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "recoder",
                    "spec",
                    str(meeting.folder),
                    item_id,
                ],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                creationflags=creationflags,
                close_fds=True,
            )
        return {"status": "running"}

    return app
