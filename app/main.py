"""FastAPI application: document write/delete/query and index rebuild."""
from __future__ import annotations

import os
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from .manager import Manager, RebuildRunning
from .tokenizer import Rules, content_tokens

DB_PATH = os.environ.get("FTS_DB_PATH", "/data/fts.db")


class RulesPayload(BaseModel):
    case_fold: bool = True
    stopwords: List[str] = Field(default_factory=list)

    @field_validator("stopwords")
    @classmethod
    def _check(cls, v):
        for w in v:
            if not isinstance(w, str):
                raise ValueError("stopwords must be strings")
        return v

    def to_rules(self) -> Rules:
        return Rules.from_dict(self.model_dump())


class DocumentPayload(BaseModel):
    text: str


class RebuildPayload(BaseModel):
    rules: Optional[RulesPayload] = None
    # drill entry point: pause the background rebuild at a phase boundary
    pause_at: Optional[str] = Field(
        default=None,
        description="backfill | catchup | switch — pause at this boundary")

    @field_validator("pause_at")
    @classmethod
    def _phase(cls, v):
        if v not in (None, "backfill", "catchup", "switch"):
            raise ValueError("pause_at must be one of "
                             "backfill, catchup, switch")
        return v


def create_app(db_path: Optional[str] = None) -> FastAPI:
    db_path = db_path or DB_PATH
    parent = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(parent, exist_ok=True)
    app = FastAPI(
        title="Unicode full-text search with online rebuild",
        version="1.0.0")
    app.state.manager = Manager(db_path)
    mgr: Manager = app.state.manager

    # ---------------------------------------------------------- documents

    @app.put("/documents/{doc_id}")
    def put_document(doc_id: str, payload: DocumentPayload):
        if not doc_id:
            raise HTTPException(400, "doc_id must not be empty")
        return mgr.upsert(doc_id, payload.text)

    @app.delete("/documents/{doc_id}")
    def remove_document(doc_id: str):
        res = mgr.delete(doc_id)
        if res is None:
            raise HTTPException(404, f"document {doc_id!r} not found")
        return res

    @app.get("/documents/{doc_id}")
    def read_document(doc_id: str, version: Optional[int] = None):
        if version is not None and version < 1:
            raise HTTPException(400, "version must be >= 1")
        res = mgr.get_document(doc_id, version)
        if res is None:
            raise HTTPException(404, f"document {doc_id!r} not found")
        return res

    # ------------------------------------------------------------- search

    @app.get("/search")
    def search(q: str = Query(..., description='phrase in quotes, '
                                              'or bare word(s)'),
               limit: int = Query(100, ge=1, le=1000)):
        gen, _wm, rules = mgr.current()
        raw_tokens = rules.tokenize(q)
        terms = [t.text for t in content_tokens(raw_tokens, rules)]
        if not terms:
            # empty query, or every word was a stopword -> reject
            raise HTTPException(
                400,
                "query must contain at least one non-stopword alphanumeric "
                "word")
        stripped = q.strip()
        is_phrase = len(stripped) >= 2 and stripped[0] == '"' \
            and stripped[-1] == '"'
        kind = "phrase" if is_phrase else \
            ("term_or" if len(terms) > 1 else "term")
        return mgr.search(terms, kind, limit)

    # ------------------------------------------------------------ rebuild

    @app.post("/rebuild")
    def rebuild(payload: Optional[RebuildPayload] = None):
        payload = payload or RebuildPayload()
        rules = (payload.rules.to_rules() if payload.rules
                 else mgr.current()[2])
        try:
            return mgr.start_rebuild(rules, pause_at=payload.pause_at)
        except RebuildRunning as exc:
            return JSONResponse(
                status_code=409,
                content={"error": "a rebuild is already running",
                         "rebuild": exc.status})

    @app.get("/rebuild")
    def rebuild_status():
        st = mgr.rebuild_status()
        if st is None:
            return {"running": False, "rebuild": None}
        return st

    @app.post("/rebuild/resume")
    def rebuild_resume():
        st = mgr.resume_rebuild()
        if st is None:
            raise HTTPException(404, "no rebuild to resume")
        return st

    @app.post("/rebuild/cancel")
    def rebuild_cancel():
        st = mgr.cancel_rebuild()
        if st is None:
            raise HTTPException(404, "no rebuild to cancel")
        return st

    # -------------------------------------------------------------- state

    @app.get("/meta")
    def meta():
        return mgr.meta_state()

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    return app


# "app" is resolved lazily via the module-level __getattr__ below so that
# importing this module does not require the data directory to exist.
app: FastAPI


def _app_factory() -> FastAPI:
    global app
    if globals().get("app") is None:
        app = create_app()
    return app


def __getattr__(name):  # module-level: lazy "app" for `uvicorn app.main:app`
    if name == "app":
        return _app_factory()
    raise AttributeError(name)

