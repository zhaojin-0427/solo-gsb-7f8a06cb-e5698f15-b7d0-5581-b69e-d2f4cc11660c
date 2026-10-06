"""HTTP API (FastAPI)."""
from __future__ import annotations

import os
from typing import List, Optional

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .query import QueryError
from .service import (
    InvalidPausePoint,
    NotPaused,
    RebuildInProgress,
    SearchService,
)
from .tokenizer import TokenizerConfig


class DocIn(BaseModel):
    text: str


class RebuildIn(BaseModel):
    config: Optional[dict] = None


class PausePointsIn(BaseModel):
    points: List[str]


def create_app(db_path: Optional[str] = None) -> FastAPI:
    path = db_path or os.environ.get("SEARCH_DB", os.path.join(os.getcwd(), "search.db"))
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    service = SearchService(path)

    app = FastAPI(title="hot-reindex full-text search")
    app.state.service = service

    @app.exception_handler(QueryError)
    async def _query_error(_: Request, exc: QueryError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(InvalidPausePoint)
    async def _bad_point(_: Request, exc: InvalidPausePoint):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(RebuildInProgress)
    async def _in_progress(_: Request, exc: RebuildInProgress):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(NotPaused)
    async def _not_paused(_: Request, exc: NotPaused):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    # ---------------- documents ----------------
    @app.put("/docs/{doc_id}")
    def put_doc(doc_id: str, body: DocIn):
        version = service.put_document(doc_id, body.text)
        return {"doc_id": doc_id, "version": version}

    @app.get("/docs/{doc_id}")
    def get_doc(doc_id: str):
        doc = service.get_document(doc_id)
        if doc is None:
            raise HTTPException(404, "document not found")
        return doc

    @app.delete("/docs/{doc_id}")
    def delete_doc(doc_id: str):
        return {"doc_id": doc_id, "deleted": service.delete_document(doc_id)}

    # ---------------- search ----------------
    @app.get("/search")
    def search(q: str = "", limit: int = Query(100, ge=1, le=1000)):
        return {"query": q, **service.search(q, limit=limit)}

    # ---------------- tokenizer / index ----------------
    @app.get("/tokenizer")
    def tokenizer():
        return service.tokenizer_info()

    @app.post("/index/rebuild", status_code=202)
    def rebuild(body: Optional[RebuildIn] = Body(default=None)):
        config = None
        if body is not None and body.config is not None:
            try:
                config = TokenizerConfig.from_dict(body.config)
            except ValueError as exc:
                raise HTTPException(400, str(exc))
        return service.start_rebuild(config)

    @app.get("/index/rebuild")
    def rebuild_status():
        return service.rebuild_status()

    @app.post("/index/rebuild/cancel")
    def rebuild_cancel():
        return {"cancelled": service.cancel_rebuild()}

    @app.get("/admin/indexes")
    def admin_indexes():
        return {"indexes": service.list_indexes()}

    # ---------------- drill: pause points ----------------
    @app.put("/admin/rebuild/pause-points")
    def arm_pause_points(body: PausePointsIn):
        return {"points": service.arm_pause_points(body.points)}

    @app.get("/admin/rebuild/pause-points")
    def get_pause_points():
        return service.pause_points()

    @app.post("/admin/rebuild/resume")
    def resume():
        return {"resumed": service.resume()}

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.on_event("shutdown")
    def _shutdown():
        service.close()

    return app


app = create_app()
