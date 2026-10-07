"""Index rebuild orchestration with non-blocking writes.

Design summary
--------------
* **Change log**: every upsert/delete appends a monotonically increasing
  row to ``changes`` inside the same transaction that mutates the
  document and the *current* index.
* **Generations**: each index (one per :class:`Rules`) lives under a
  ``gen`` id; exactly one generation is ``current``, the rebuild target
  is ``building`` until an atomic switch retires the previous one.
* **Rebuild phases**:
    1. *backfill* — tokenize every live document from a single WAL
       snapshot (one read transaction), watermark = max(change seq) in
       that snapshot; committed in chunks, each chunk a short write
       transaction; the global write lock is only held per chunk.
    2. *catch-up* — replay change-log rows past the watermark in small
       batches, yielding the lock between batches so writers keep
       flowing.
    3. *switch* — under the write lock, replay the final tail and flip
       ``active_gen`` in one transaction (the tail application and the
       flip commit together, so no committed write can be missed and no
       delete can resurrect).
* **Old index reclaim**: retired generations are deleted only after all
  in-flight read leases drain.
* **Crash**: ``building`` generations are discarded on startup (safe
  cleanup; the current generation is never touched by a rebuild until
  the switch commits, so a crash cannot expose a half-built index).
"""
from __future__ import annotations

import contextlib
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from . import db
from .tokenizer import Rules, content_tokens

_CHUNK = 500
_CATCHUP_POLL = 0.05
_TAIL_LAG = 500


class GenRetired(Exception):
    """Raised when a read lease is taken on a generation being retired."""


class ReadLeases:
    """Reference counts per generation for in-flight reads."""

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._counts: Dict[int, int] = {}
        self._closing: set[int] = set()

    @contextlib.contextmanager
    def lease(self, gen: int) -> Iterator[None]:
        with self._cv:
            if gen in self._closing:
                raise GenRetired(gen)
            self._counts[gen] = self._counts.get(gen, 0) + 1
        try:
            yield
        finally:
            with self._cv:
                self._counts[gen] -= 1
                if self._counts[gen] == 0:
                    self._cv.notify_all()

    def retire_and_wait(self, gen: int) -> None:
        """Block new leases on ``gen`` and wait for readers to finish."""
        with self._cv:
            self._closing.add(gen)
            while self._counts.get(gen, 0) > 0:
                self._cv.wait()

    def forget(self, gen: int) -> None:
        with self._cv:
            self._closing.discard(gen)
            self._counts.pop(gen, None)


@dataclass
class _Job:
    gen: int
    rules: Rules
    pause_at: Optional[str]  # 'backfill' | 'catchup' | 'switch' | None
    cond: threading.Condition = field(default_factory=threading.Condition)
    cancelled: bool = False
    finished: bool = False
    phase: str = "init"
    paused: Optional[str] = None
    error: Optional[str] = None
    switched: bool = False
    backfill_total: int = 0
    backfill_done: int = 0
    watermark: int = -1

    def barrier(self, phase: str) -> bool:
        """Block while paused at ``phase``. Returns False if cancelled."""
        with self.cond:
            if self.pause_at == phase and not self.cancelled:
                self.paused = phase
                self.phase = f"paused:{phase}"
                self.cond.notify_all()
                while self.pause_at == phase and not self.cancelled:
                    self.cond.wait()
            self.paused = None
            return not self.cancelled

    def resume(self) -> None:
        with self.cond:
            self.pause_at = None
            self.cond.notify_all()

    def cancel(self) -> None:
        with self.cond:
            self.cancelled = True
            self.pause_at = None
            self.cond.notify_all()

    def set_phase(self, phase: str) -> None:
        with self.cond:
            self.phase = phase
            self.cond.notify_all()

    def status(self) -> Dict[str, Any]:
        with self.cond:
            return {
                "gen": self.gen,
                "phase": self.phase,
                "running": not self.finished,
                "paused": self.paused,
                "paused_at": self.pause_at,
                "cancelled": self.cancelled,
                "switched": self.switched,
                "error": self.error,
                "backfill_total": self.backfill_total,
                "backfill_done": self.backfill_done,
                "watermark": self.watermark,
                "rules": self.rules.to_dict(),
            }


class Manager:
    def __init__(self, db_path: str) -> None:
        self.path = db_path
        self._wlock = threading.RLock()
        self._leases = ReadLeases()
        self._start_lock = threading.Lock()
        self._job: Optional[_Job] = None
        self._active_lock = threading.Lock()
        self._active: Optional[Tuple[int, int, Rules]] = None

        conn = db.connect(db_path)
        try:
            db.init_db(conn)
            self._crash_cleanup(conn)
            self._bootstrap(conn)
        finally:
            conn.close()

    # ------------------------------------------------------------ startup

    def _crash_cleanup(self, conn) -> None:
        """Discard half-built gens; reap retired gens (no readers at boot).

        Safe-cleanup policy: a rebuild interrupted by a process restart is
        discarded rather than resumed; start a new rebuild afterwards.
        """
        with self._wlock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for row in db.generations_by_state(conn, "building"):
                    db.delete_gen_tokens(conn, row["gen"])
                for row in db.generations_by_state(conn, "retired"):
                    db.delete_gen_tokens(conn, row["gen"])
                db.gc_doc_versions(conn)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def _bootstrap(self, conn) -> None:
        with self._wlock:
            active = db.active_generation(conn)
            if active is not None:
                row = db.get_generation(conn, active[0])
                rules = Rules.from_dict(json.loads(row["rules"]))
                self._set_active(active[0], active[1], rules)
                return
            rules = Rules()  # case folding on, no stopwords
            conn.execute("BEGIN IMMEDIATE")
            try:
                gen = self._allocate_gen(conn)
                wm = db.max_change_seq(conn)
                conn.execute(
                    "INSERT INTO gen_meta(gen, state, rules, watermark, "
                    " created_at) VALUES(?, 'current', ?, ?, ?)",
                    (gen, rules.to_json(), wm, time.time()))
                db.meta_set(conn, "active_gen", str(gen))
                db.meta_set(conn, "active_watermark", str(wm))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            self._set_active(gen, wm, rules)

    def _set_active(self, gen: int, watermark: int, rules: Rules) -> None:
        with self._active_lock:
            self._active = (gen, watermark, rules)

    def current(self) -> Tuple[int, int, Rules]:
        with self._active_lock:
            assert self._active is not None
            return self._active

    @staticmethod
    def _allocate_gen(conn) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(gen), 0) + 1 AS g FROM gen_meta").fetchone()
        return row["g"]

    # ------------------------------------------------------------- writes

    def upsert(self, doc_id: str, text: str) -> Dict[str, Any]:
        with self._wlock:
            conn = db.connect(self.path)
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    gen, _, rules = self.current()
                    version, seq = db.upsert_document(conn, doc_id, text)
                    db.delete_gen_doc(conn, gen, doc_id)
                    rows = self._token_rows(gen, doc_id, version, text, rules)
                    db.insert_token_rows(conn, rows)
                    conn.execute(
                        "UPDATE gen_meta SET watermark=? WHERE gen=?",
                        (seq, gen))
                    db.meta_set(conn, "active_watermark", str(seq))
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
            finally:
                conn.close()
        return {"doc_id": doc_id, "version": version, "seq": seq,
                "indexed_gen": gen, "tokens": len(rows)}

    def delete(self, doc_id: str) -> Optional[Dict[str, Any]]:
        with self._wlock:
            conn = db.connect(self.path)
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    gen, _, _ = self.current()
                    res = db.delete_document(conn, doc_id)
                    if res is None:
                        conn.execute("ROLLBACK")
                        return None
                    _version, seq = res
                    db.delete_gen_doc(conn, gen, doc_id)
                    conn.execute(
                        "UPDATE gen_meta SET watermark=? WHERE gen=?",
                        (seq, gen))
                    db.meta_set(conn, "active_watermark", str(seq))
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
            finally:
                conn.close()
        return {"doc_id": doc_id, "seq": seq, "indexed_gen": gen}

    def get_document(self, doc_id: str,
                     version: Optional[int] = None) -> Optional[Dict[str, Any]]:
        conn = db.connect(self.path, readonly=True)
        try:
            if version is None:
                got = db.get_document(conn, doc_id)
                if got is None:
                    return None
                v, text, _ = got
            else:
                text = db.get_version_text(conn, doc_id, version)
                if text is None:
                    return None
                v = version
            return {"doc_id": doc_id, "version": v, "text": text,
                    "length": len(text)}
        finally:
            conn.close()

    @staticmethod
    def _token_rows(gen: int, doc_id: str, version: int, text: str,
                    rules: Rules) -> List[tuple]:
        toks = content_tokens(rules.tokenize(text), rules)
        return [(gen, t.text, doc_id, version, t.pos, t.start, t.end)
                for t in toks]

    # ------------------------------------------------------------- search

    def search(self, terms: Sequence[str], kind: str, limit: int
               ) -> Dict[str, Any]:
        """Run a query against one generation.

        A read lease pins the generation; the whole query executes in one
        read-only transaction, so hits and their referenced doc versions
        always belong to the same snapshot.
        """
        for _ in range(10):
            gen, watermark, rules = self.current()
            try:
                cm = self._leases.lease(gen)
            except GenRetired:
                continue
            with cm:
                conn = db.connect(self.path, readonly=True)
                try:
                    conn.execute("BEGIN")
                    if kind == "phrase" and len(terms) > 1:
                        hits, truncated = db.search_phrase(
                            conn, gen, terms, limit)
                        out_kind = "phrase"
                    elif kind == "term_or" and len(terms) > 1:
                        hits, truncated = self._search_or(
                            conn, gen, terms, limit)
                        out_kind = "term_or"
                    else:
                        hits, truncated = db.search_term(
                            conn, gen, terms[0], limit)
                        out_kind = "term"
                    conn.execute("COMMIT")
                finally:
                    conn.close()
                return {
                    "query": list(terms),
                    "kind": out_kind,
                    "gen": gen,
                    "index_watermark": watermark,
                    "rules": rules.to_dict(),
                    "count": len(hits),
                    "truncated": truncated,
                    "hits": hits,
                }
        raise RuntimeError("search retried across too many rebuild switches")

    @staticmethod
    def _search_or(conn, gen: int, terms: Sequence[str], limit: int
                   ) -> Tuple[List[dict], bool]:
        """Whole-word OR across ``terms`` inside one snapshot transaction."""
        merged: Dict[Tuple[str, int], dict] = {}
        order: List[Tuple[str, int]] = []
        truncated = False
        for term in terms:
            term_hits, tr = db.search_term(conn, gen, term, limit)
            truncated = truncated or tr
            for hit in term_hits:
                key = (hit["doc_id"], hit["doc_version"])
                if key not in merged:
                    if len(order) >= limit:
                        truncated = True
                        continue
                    merged[key] = {"doc_id": hit["doc_id"],
                                   "doc_version": hit["doc_version"],
                                   "kind": "term_or", "matches": []}
                    order.append(key)
                merged[key]["matches"].append(
                    {"term": term, "spans": hit["spans"]})
        return [merged[k] for k in order], truncated

    # ------------------------------------------------------------ rebuild

    def start_rebuild(self, rules: Rules,
                      pause_at: Optional[str] = None) -> Dict[str, Any]:
        if pause_at not in (None, "backfill", "catchup", "switch"):
            raise ValueError("pause_at must be backfill|catchup|switch")
        if not self._start_lock.acquire(blocking=False):
            raise RebuildRunning(self.rebuild_status())

        with self._wlock:
            conn = db.connect(self.path)
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    gen = self._allocate_gen(conn)
                    db.create_generation(conn, gen, rules.to_json())
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
            finally:
                conn.close()

        job = _Job(gen=gen, rules=rules, pause_at=pause_at, phase="starting",
                   watermark=-1)
        self._job = job
        try:
            t = threading.Thread(target=self._run, args=(job,), daemon=True,
                                 name=f"rebuild-gen{gen}")
            t.start()
        except Exception:
            self._start_lock.release()
            raise
        return job.status()

    def rebuild_status(self) -> Optional[Dict[str, Any]]:
        job = self._job
        return job.status() if job is not None else None

    def resume_rebuild(self) -> Optional[Dict[str, Any]]:
        job = self._job
        if job is None:
            return None
        job.resume()
        return job.status()

    def cancel_rebuild(self) -> Optional[Dict[str, Any]]:
        job = self._job
        if job is None:
            return None
        job.cancel()
        return job.status()

    def wait_rebuild(self, timeout: Optional[float] = None) -> bool:
        job = self._job
        if job is None:
            return True
        deadline = None if timeout is None else time.time() + timeout
        with job.cond:
            while not job.finished:
                remaining = None if deadline is None else deadline - time.time()
                if remaining is not None and remaining <= 0:
                    return False
                job.cond.wait(timeout=remaining)
        return True

    # ----------------------------------------------------- rebuild worker

    def _run(self, job: _Job) -> None:
        try:
            old_gen = self.current()[0]
            self._backfill(job)
            if job.cancelled:
                self._discard(job)
                return
            self._catchup(job)
            if job.cancelled:
                self._discard(job)
                return
            if not self._switch(job, old_gen):
                # cancelled while parked at the switch barrier:
                # the switch transaction never ran
                self._discard(job)
                return
            self._retire(old_gen)
            job.set_phase("done")
            job.switched = True
        except Exception as exc:  # pragma: no cover - defensive
            job.error = repr(exc)
            job.set_phase("error")
            try:
                self._discard(job)
            except Exception:
                pass
        finally:
            # release the single-winner slot before observers are woken,
            # so a waiter can start the next rebuild immediately
            self._start_lock.release()
            with job.cond:
                job.finished = True
                job.cond.notify_all()

    def _backfill(self, job: _Job) -> None:
        job.set_phase("backfill")
        snap = db.connect(self.path, readonly=True)
        try:
            snap.execute("BEGIN")  # snapshot begins here
            watermark = db.max_change_seq(snap)
            docs = db.list_snapshot_docs(snap)
            job.watermark = watermark
            job.backfill_total = len(docs)

            conn = db.connect(self.path)
            try:
                # persist the starting watermark
                with self._wlock:
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        db.set_gen_watermark(conn, job.gen, watermark)
                        conn.execute("COMMIT")
                    except Exception:
                        conn.execute("ROLLBACK")
                        raise

                # drill boundary #1: snapshot taken, nothing indexed yet
                if not job.barrier("backfill"):
                    return

                for i in range(0, len(docs), _CHUNK):
                    if job.cancelled:
                        return
                    chunk = docs[i:i + _CHUNK]
                    rows: List[tuple] = []
                    for doc_id, version in chunk:
                        text = db.get_version_text(snap, doc_id, version)
                        if text is None:
                            continue
                        rows.extend(self._token_rows(
                            job.gen, doc_id, version, text, job.rules))
                    with self._wlock:
                        conn.execute("BEGIN IMMEDIATE")
                        try:
                            db.insert_token_rows(conn, rows)
                            conn.execute("COMMIT")
                        except Exception:
                            conn.execute("ROLLBACK")
                            raise
                    job.backfill_done = min(i + _CHUNK, len(docs))
            finally:
                conn.close()
        finally:
            snap.close()

    def _catchup(self, job: _Job) -> None:
        """Replay change-log rows past the backfill watermark.

        Drains batches while a backlog exists (yielding the lock and GIL
        between batches).  Once the log lag drops below ``_TAIL_LAG`` we
        stop: the switch transaction atomically drains whatever final
        tail exists, so waiting for writers to go fully quiet is neither
        necessary nor possible under sustained writes.
        """
        job.set_phase("catchup")
        seen_empty = False
        conn = db.connect(self.path)
        try:
            while not job.cancelled:
                # how far behind are we?
                lag = self._log_head(conn) - job.watermark
                if lag <= _TAIL_LAG:
                    # close enough: the drill boundary fires on first
                    # approach, and the switch transaction atomically
                    # drains whatever final tail exists.
                    if not seen_empty:
                        seen_empty = True
                        if not job.barrier("catchup"):
                            return
                    return
                applied = self._apply_batch(job, conn=conn)
                if applied:
                    time.sleep(0)  # yield lock + GIL between batches
                else:
                    time.sleep(_CATCHUP_POLL)
        finally:
            conn.close()

    @staticmethod
    def _log_head(conn) -> int:
        return db.max_change_seq(conn)

    def _apply_batch(self, job: _Job, conn=None,
                     max_rows: int = 2000) -> int:
        own_conn = conn is None
        applied = 0
        with self._wlock:
            if own_conn:
                conn = db.connect(self.path)
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    batch = db.changes_after(conn, job.watermark)[:max_rows]
                    for ch in batch:
                        self._apply_change(conn, job, ch)
                        job.watermark = ch["seq"]
                        applied += 1
                    if batch:
                        db.set_gen_watermark(conn, job.gen, job.watermark)
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
            finally:
                if own_conn:
                    conn.close()
        return applied

    def _apply_change(self, conn, job: _Job, ch) -> None:
        db.delete_gen_doc(conn, job.gen, ch["doc_id"])
        if ch["kind"] == "upsert":
            text = db.get_version_text(conn, ch["doc_id"], ch["version"])
            if text is not None:
                rows = self._token_rows(
                    job.gen, ch["doc_id"], ch["version"], text, job.rules)
                db.insert_token_rows(conn, rows)

    def _switch(self, job: _Job, old_gen: int) -> bool:
        """Run the final tail+flip transaction.

        Returns False when the job was cancelled while parked at the
        switch barrier (nothing was committed).  Once the transaction
        starts it cannot be interrupted, so a True return means the
        candidate generation is irrevocably current.
        """
        job.set_phase("switch")
        # drill boundary #3: everything is replayed, the blocking tail
        # transaction has not run yet
        if not job.barrier("switch"):
            return False
        with self._wlock:
            conn = db.connect(self.path)
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for ch in db.changes_after(conn, job.watermark):
                        self._apply_change(conn, job, ch)
                        job.watermark = ch["seq"]
                    db.atomic_switch(conn, job.gen, job.watermark)
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
            finally:
                conn.close()
        self._set_active(job.gen, job.watermark, job.rules)
        job.switched = True
        return True

    def _retire(self, old_gen: int) -> None:
        """Wait out in-flight readers of the old gen, then reclaim it."""
        if old_gen == self.current()[0]:
            return
        job = self._job
        if job is not None:
            job.set_phase("retiring")
        # block new leases on the old gen, then wait for existing readers
        self._leases.retire_and_wait(old_gen)
        with self._wlock:
            conn = db.connect(self.path)
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    db.delete_gen_tokens(conn, old_gen)
                    db.gc_doc_versions(conn)
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
            finally:
                conn.close()
        self._leases.forget(old_gen)

    def _discard(self, job: _Job) -> None:
        # defensive: never delete the generation currently serving queries
        if job.gen == self.current()[0]:
            return
        with self._wlock:
            conn = db.connect(self.path)
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    db.delete_gen_tokens(conn, job.gen)
                    db.gc_doc_versions(conn)
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
            finally:
                conn.close()
        self._leases.forget(job.gen)

    # -------------------------------------------------------------- state

    def meta_state(self) -> Dict[str, Any]:
        conn = db.connect(self.path, readonly=True)
        try:
            active = db.active_generation(conn)
            gens = [dict(g) for g in conn.execute(
                "SELECT gen, state, watermark FROM gen_meta ORDER BY gen")]
            wm = db.max_change_seq(conn)
            ndocs = conn.execute(
                "SELECT COUNT(*) c FROM docs WHERE deleted=0").fetchone()["c"]
            cur = None
            if active is not None:
                gen, watermark = active
                row = db.get_generation(conn, gen)
                cur = {"gen": gen, "watermark": watermark,
                       "rules": Rules.from_dict(
                           json.loads(row["rules"])).to_dict()}
        finally:
            conn.close()
        return {"active": cur, "generations": gens,
                "change_log_max": wm, "live_documents": ndocs,
                "rebuild": self.rebuild_status()}


class RebuildRunning(Exception):
    def __init__(self, status: Optional[Dict[str, Any]]):
        self.status = status
        super().__init__("a rebuild is already running")
