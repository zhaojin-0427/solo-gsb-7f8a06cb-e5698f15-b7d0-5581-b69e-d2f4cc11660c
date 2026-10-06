"""Core search service: documents, the live index, and hot rebuilds.

Rebuild protocol
----------------
* Every acknowledged write is applied, in one SQLite transaction, to the
  documents table, the change log AND the current index generation, so the
  index served to queries is always complete. Rebuilds never block the write
  path except for the short final switch transaction.
* A rebuild claims the singleton ``rebuild_state`` row with a fencing token,
  records the change-log high-water mark (``snapshot_seq``), backfills a
  *candidate* generation from a keyset-paginated scan, then replays the
  change log from the watermark (``applied_seq``) until it catches up.
* The switch is a single write transaction: it replays the remaining log
  under the write lock (so every write acknowledged before the switch is
  included and no delete can resurrect), flips ``active_gen`` and retires
  the old generation atomically. A crash mid-switch simply rolls back.
* The fencing token is checked inside every mutating transaction of the
  worker; cancel and crash recovery revoke it, so a half-built candidate
  can never be switched in. Only one rebuild can win the claim update.
* The retired generation's postings are dropped only after all in-flight
  readers release their leases; WAL snapshot isolation keeps any late
  reader fully consistent in the meantime, and a query always reads
  postings and document text from the same snapshot (same version).
"""
from __future__ import annotations

import json
import logging
import threading
import traceback
import uuid
from contextlib import contextmanager
from typing import Dict, List, Optional

from .index import fetch_postings, index_document, part_matches, remove_document
from .query import parse_query
from .store import Store
from .tokenizer import TokenizerConfig, tokenize

log = logging.getLogger("search.service")

PAUSE_POINTS = ("backfill", "catchup", "switch")
_BATCH = 200


class RebuildInProgress(Exception):
    pass


class NotPaused(Exception):
    pass


class InvalidPausePoint(Exception):
    pass


class _Aborted(Exception):
    """The worker lost its fencing token (cancelled or replaced by recovery)."""


class LeaseManager:
    """Tracks in-flight readers per index generation; GC waits for zero."""

    def __init__(self):
        self._cond = threading.Condition()
        self._counts: Dict[int, int] = {}

    @contextmanager
    def lease(self, gen: int):
        with self._cond:
            self._counts[gen] = self._counts.get(gen, 0) + 1
        try:
            yield
        finally:
            with self._cond:
                n = self._counts.get(gen, 0) - 1
                if n <= 0:
                    self._counts.pop(gen, None)
                else:
                    self._counts[gen] = n
                self._cond.notify_all()

    def wait_until_zero(self, gen: int, stop: threading.Event) -> bool:
        with self._cond:
            while self._counts.get(gen, 0):
                if stop.is_set():
                    return False
                self._cond.wait(0.1)
        return True

    def snapshot(self) -> Dict[int, int]:
        with self._cond:
            return dict(self._counts)


def _active_gen(c) -> int:
    return int(c.execute("SELECT value FROM meta WHERE key='active_gen'").fetchone()[0])


def _config_of(c, gen: int) -> TokenizerConfig:
    row = c.execute("SELECT config_json FROM indexes WHERE gen=?", (gen,)).fetchone()
    return TokenizerConfig.from_dict(json.loads(row[0]))


def _next_version(c) -> int:
    c.execute("UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key='version_seq'")
    return int(c.execute("SELECT value FROM meta WHERE key='version_seq'").fetchone()[0])


def _state_row(c):
    return c.execute("SELECT * FROM rebuild_state WHERE id=1").fetchone()


class SearchService:
    def __init__(self, db_path: str):
        self.store = Store(db_path)
        self.store.init_schema(json.dumps(TokenizerConfig().to_dict()))
        self._leases = LeaseManager()
        self._pause_cond = threading.Condition()
        self._rebuild_lock = threading.Lock()
        self._stop = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._recover()

    # ------------------------------------------------------------------
    # documents (write path always updates the *current* index generation)
    # ------------------------------------------------------------------
    def put_document(self, doc_id: str, text: str) -> int:
        with self.store.write_txn() as c:
            gen = _active_gen(c)
            config = _config_of(c, gen)
            version = _next_version(c)
            c.execute(
                "INSERT INTO documents(doc_id, text, version) VALUES (?,?,?) "
                "ON CONFLICT(doc_id) DO UPDATE SET text=excluded.text, version=excluded.version",
                (doc_id, text, version),
            )
            c.execute(
                "INSERT INTO change_log(doc_id, op, version) VALUES (?,?,?)",
                (doc_id, "upsert", version),
            )
            index_document(c, gen, doc_id, text, config)
            return version

    def get_document(self, doc_id: str) -> Optional[dict]:
        with self.store.read_txn() as c:
            row = c.execute(
                "SELECT doc_id, text, version FROM documents WHERE doc_id=?", (doc_id,)
            ).fetchone()
            return dict(row) if row else None

    def delete_document(self, doc_id: str) -> bool:
        with self.store.write_txn() as c:
            row = c.execute("SELECT 1 FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
            if row is None:
                return False
            version = _next_version(c)
            c.execute("DELETE FROM documents WHERE doc_id=?", (doc_id,))
            c.execute(
                "INSERT INTO change_log(doc_id, op, version) VALUES (?,?,?)",
                (doc_id, "delete", version),
            )
            remove_document(c, _active_gen(c), doc_id)
            return True

    # ------------------------------------------------------------------
    # search
    # ------------------------------------------------------------------
    def search(self, q: str, limit: int = 100) -> dict:
        limit = max(1, min(int(limit), 1000))
        with self.store.read_txn() as c:
            gen = _active_gen(c)
            config = _config_of(c, gen)
            parts = parse_query(q, config)
            # Lease the generation we are about to read so its postings are
            # not reclaimed underneath us; the read transaction pins a
            # consistent snapshot of postings + document text.
            with self._leases.lease(gen):
                terms = sorted({t for part in parts for t in part.terms()})
                postings = fetch_postings(c, gen, terms)
                per_part = [part_matches(p, postings) for p in parts]
                common = set(per_part[0])
                for m in per_part[1:]:
                    common &= m.keys()
                results = []
                for doc_id in sorted(common):
                    row = c.execute(
                        "SELECT text, version FROM documents WHERE doc_id=?", (doc_id,)
                    ).fetchone()
                    if row is None:
                        continue  # cannot happen inside one snapshot; be safe
                    spans = _spans(row["text"], config, [m[doc_id] for m in per_part])
                    results.append({
                        "doc_id": doc_id,
                        "version": row["version"],
                        "spans": spans,
                    })
        return {"gen": gen, "total": len(results), "results": results[:limit]}

    # ------------------------------------------------------------------
    # rebuild control
    # ------------------------------------------------------------------
    def start_rebuild(self, config: Optional[TokenizerConfig] = None) -> dict:
        with self._rebuild_lock:
            token = uuid.uuid4().hex
            with self.store.write_txn() as c:
                st = _state_row(c)
                if st["state"] != "IDLE":
                    raise RebuildInProgress(f"rebuild already running (state={st['state']}, gen={st['gen']})")
                if config is None:
                    config = _config_of(c, _active_gen(c))
                gen = c.execute("SELECT COALESCE(MAX(gen), 0) FROM indexes").fetchone()[0] + 1
                snapshot_seq = c.execute("SELECT COALESCE(MAX(seq), 0) FROM change_log").fetchone()[0]
                armed = json.loads(
                    c.execute("SELECT value FROM meta WHERE key='armed_pause_points'").fetchone()[0]
                )
                c.execute(
                    "INSERT INTO indexes(gen, status, config_json) VALUES (?, 'building', ?)",
                    (gen, json.dumps(config.to_dict())),
                )
                cur = c.execute(
                    "UPDATE rebuild_state SET state='BACKFILL', gen=?, owner=?, "
                    "snapshot_seq=?, applied_seq=?, pause_points=?, paused_at=NULL "
                    "WHERE id=1 AND state='IDLE'",
                    (gen, token, snapshot_seq, snapshot_seq, json.dumps(armed)),
                )
                if cur.rowcount != 1:
                    raise RebuildInProgress("lost the rebuild claim race")
            self._start_worker(token, gen)
            return {"state": "BACKFILL", "gen": gen, "snapshot_seq": snapshot_seq}

    def rebuild_status(self) -> dict:
        with self.store.read_txn() as c:
            st = dict(_state_row(c))
            max_seq = c.execute("SELECT COALESCE(MAX(seq), 0) FROM change_log").fetchone()[0]
            st["active_gen"] = _active_gen(c)
        st["pause_points"] = json.loads(st["pause_points"])
        st["lag"] = max(0, max_seq - st["applied_seq"])
        st["readers"] = self._leases.snapshot()
        st["worker_alive"] = self._worker is not None and self._worker.is_alive()
        return st

    def cancel_rebuild(self) -> bool:
        with self.store.write_txn() as c:
            st = _state_row(c)
            if st["state"] == "IDLE":
                return False
            gen = st["gen"]
            c.execute("DELETE FROM postings WHERE gen=?", (gen,))
            c.execute("UPDATE indexes SET status='cancelled' WHERE gen=?", (gen,))
            # No rebuild is in progress anymore: the log is fully covered by
            # the documents table, so it can be truncated.
            c.execute("DELETE FROM change_log")
            c.execute(
                "UPDATE rebuild_state SET state='IDLE', owner=NULL, gen=NULL, "
                "pause_points='[]', paused_at=NULL WHERE id=1"
            )
        with self._pause_cond:
            self._pause_cond.notify_all()
        return True

    # ------------------------------------------------------------------
    # drill: pause points at the backfill / catchup / switch boundaries
    # ------------------------------------------------------------------
    def arm_pause_points(self, points: List[str]) -> List[str]:
        bad = set(points) - set(PAUSE_POINTS)
        if bad:
            raise InvalidPausePoint(f"unknown pause points: {sorted(bad)}")
        points = sorted(set(points))
        with self.store.write_txn() as c:
            st = _state_row(c)
            if st["state"] != "IDLE":
                c.execute("UPDATE rebuild_state SET pause_points=? WHERE id=1",
                          (json.dumps(points),))
            else:
                c.execute("UPDATE meta SET value=? WHERE key='armed_pause_points'",
                          (json.dumps(points),))
        return points

    def pause_points(self) -> dict:
        with self.store.read_txn() as c:
            st = _state_row(c)
            armed = json.loads(
                c.execute("SELECT value FROM meta WHERE key='armed_pause_points'").fetchone()[0]
            )
        return {
            "armed": armed,
            "active": json.loads(st["pause_points"]) if st["state"] != "IDLE" else [],
            "paused_at": st["paused_at"],
        }

    def resume(self) -> str:
        with self.store.write_txn() as c:
            st = _state_row(c)
            point = st["paused_at"]
            if st["state"] == "IDLE" or point is None:
                raise NotPaused("rebuild worker is not paused")
            remaining = [p for p in json.loads(st["pause_points"]) if p != point]
            c.execute(
                "UPDATE rebuild_state SET pause_points=?, paused_at=NULL WHERE id=1",
                (json.dumps(remaining),),
            )
        with self._pause_cond:
            self._pause_cond.notify_all()
        return point

    # ------------------------------------------------------------------
    # introspection
    # ------------------------------------------------------------------
    def tokenizer_info(self) -> dict:
        with self.store.read_txn() as c:
            gen = _active_gen(c)
            config = _config_of(c, gen)
        return {"gen": gen, "config": config.to_dict()}

    def list_indexes(self) -> List[dict]:
        with self.store.read_txn() as c:
            rows = c.execute("SELECT gen, status, config_json FROM indexes ORDER BY gen").fetchall()
            counts = {r["gen"]: r["n"] for r in c.execute(
                "SELECT gen, COUNT(*) AS n FROM postings GROUP BY gen")}
        readers = self._leases.snapshot()
        return [{
            "gen": r["gen"],
            "status": r["status"],
            "config": json.loads(r["config_json"]),
            "postings": counts.get(r["gen"], 0),
            "readers": readers.get(r["gen"], 0),
        } for r in rows]

    # ------------------------------------------------------------------
    # rebuild worker
    # ------------------------------------------------------------------
    def _start_worker(self, token: str, gen: int) -> None:
        t = threading.Thread(target=self._rebuild_main, args=(token, gen),
                             name=f"rebuild-gen{gen}", daemon=True)
        self._worker = t
        t.start()

    def _rebuild_main(self, token: str, gen: int) -> None:
        try:
            with self.store.read_txn() as c:
                config = _config_of(c, gen)
            self._backfill(token, gen, config)
            self._transition(token, "BACKFILL", "CATCHUP")
            self._catchup(token, gen, config)
            self._transition(token, "CATCHUP", "SWITCH")
            self._maybe_pause(token, "switch")
            retired = self._switch(token, gen, config)
            if retired is not None:
                self._gc_retired(retired)
        except _Aborted:
            log.info("rebuild worker for gen %s aborted (fencing lost)", gen)
        except Exception:  # leave persisted state for recovery on restart
            log.error("rebuild worker crashed:\n%s", traceback.format_exc())
        finally:
            self._worker = None

    def _fenced(self, c, token: str, expect: Optional[str] = None):
        row = _state_row(c)
        if row is None or row["owner"] != token or (expect and row["state"] != expect):
            raise _Aborted
        return row

    def _backfill(self, token: str, gen: int, config: TokenizerConfig) -> None:
        # Restart-safe: wipe whatever a previous attempt left, then rescan.
        with self.store.write_txn() as c:
            self._fenced(c, token, "BACKFILL")
            c.execute("DELETE FROM postings WHERE gen=?", (gen,))
        cursor = ""
        while True:
            self._maybe_pause(token, "backfill")
            with self.store.read_txn() as c:
                rows = c.execute(
                    "SELECT doc_id, text FROM documents WHERE doc_id > ? "
                    "ORDER BY doc_id LIMIT ?", (cursor, _BATCH),
                ).fetchall()
            if not rows:
                return
            with self.store.write_txn() as c:
                self._fenced(c, token, "BACKFILL")
                for r in rows:
                    index_document(c, gen, r["doc_id"], r["text"], config)
            cursor = rows[-1]["doc_id"]

    def _catchup(self, token: str, gen: int, config: TokenizerConfig) -> None:
        while True:
            self._maybe_pause(token, "catchup")
            with self.store.write_txn() as c:
                st = self._fenced(c, token, "CATCHUP")
                rows = c.execute(
                    "SELECT seq, doc_id, op FROM change_log WHERE seq > ? "
                    "ORDER BY seq LIMIT ?", (st["applied_seq"], _BATCH),
                ).fetchall()
                if not rows:
                    return
                for row in rows:
                    self._apply(c, gen, row, config)
                c.execute(
                    "UPDATE rebuild_state SET applied_seq=? WHERE id=1 AND owner=?",
                    (rows[-1]["seq"], token),
                )

    @staticmethod
    def _apply(c, gen: int, row, config: TokenizerConfig) -> None:
        if row["op"] == "upsert":
            d = c.execute("SELECT text FROM documents WHERE doc_id=?",
                          (row["doc_id"],)).fetchone()
            if d is None:
                remove_document(c, gen, row["doc_id"])
            else:
                index_document(c, gen, row["doc_id"], d["text"], config)
        else:
            remove_document(c, gen, row["doc_id"])

    def _transition(self, token: str, frm: str, to: str) -> None:
        with self.store.write_txn() as c:
            cur = c.execute(
                "UPDATE rebuild_state SET state=? WHERE id=1 AND owner=? AND state=?",
                (to, token, frm),
            )
            if cur.rowcount != 1:
                raise _Aborted

    def _switch(self, token: str, gen: int, config: TokenizerConfig) -> Optional[int]:
        """Atomic cut-over. Runs entirely under the write lock: replay the
        remaining change log, flip the active generation, retire the old one
        and release the rebuild slot — or roll everything back."""
        with self.store.write_txn() as c:
            st = self._fenced(c, token, "SWITCH")
            rows = c.execute(
                "SELECT seq, doc_id, op FROM change_log WHERE seq > ? ORDER BY seq",
                (st["applied_seq"],),
            ).fetchall()
            for row in rows:
                self._apply(c, gen, row, config)
            final_seq = max([st["applied_seq"]] + [r["seq"] for r in rows])
            old = _active_gen(c)
            c.execute("UPDATE indexes SET status='retired' WHERE gen=? AND status='active'", (old,))
            cur = c.execute(
                "UPDATE indexes SET status='active' WHERE gen=? AND status='building'", (gen,))
            if cur.rowcount != 1:
                raise _Aborted
            c.execute("UPDATE meta SET value=? WHERE key='active_gen'", (str(gen),))
            cur = c.execute(
                "UPDATE rebuild_state SET state='IDLE', owner=NULL, gen=NULL, "
                "applied_seq=?, pause_points='[]', paused_at=NULL WHERE id=1 AND owner=?",
                (final_seq, token),
            )
            if cur.rowcount != 1:
                raise _Aborted
            # Applied to both indexes now; no in-flight rebuild needs them.
            c.execute("DELETE FROM change_log WHERE seq <= ?", (final_seq,))
            return old

    def _gc_retired(self, gen: int) -> None:
        """Reclaim the old generation only after in-flight readers finish."""
        if not self._leases.wait_until_zero(gen, self._stop):
            return
        with self.store.write_txn() as c:
            c.execute("DELETE FROM postings WHERE gen=?", (gen,))
            c.execute("UPDATE indexes SET status='dropped' WHERE gen=? AND status='retired'", (gen,))

    # ------------------------------------------------------------------
    # drill support: park the worker at a boundary until resumed/cancelled
    # ------------------------------------------------------------------
    def _maybe_pause(self, token: str, point: str) -> None:
        while True:
            if self._stop.is_set():
                raise _Aborted
            with self.store.read_txn() as c:
                row = c.execute(
                    "SELECT owner, pause_points, paused_at FROM rebuild_state WHERE id=1"
                ).fetchone()
            if row is None or row["owner"] != token:
                raise _Aborted
            if point not in json.loads(row["pause_points"]):
                return
            if row["paused_at"] != point:
                with self.store.write_txn() as c:
                    cur = c.execute(
                        "UPDATE rebuild_state SET paused_at=? WHERE id=1 AND owner=?",
                        (point, token),
                    )
                    if cur.rowcount != 1:
                        raise _Aborted
            with self._pause_cond:
                self._pause_cond.wait(0.2)

    # ------------------------------------------------------------------
    # crash recovery
    # ------------------------------------------------------------------
    def _recover(self) -> None:
        token = uuid.uuid4().hex
        resume_gen = None
        with self.store.write_txn() as c:
            st = _state_row(c)
            active = _active_gen(c)
            candidate = st["gen"] if st["state"] != "IDLE" else None
            # No readers exist at startup: collect every generation that is
            # neither active nor the resumable candidate.
            keep = {active} | ({candidate} if candidate is not None else set())
            for row in c.execute("SELECT DISTINCT gen FROM postings").fetchall():
                if row["gen"] not in keep:
                    c.execute("DELETE FROM postings WHERE gen=?", (row["gen"],))
            c.execute(
                "UPDATE indexes SET status='dropped' "
                "WHERE status IN ('retired','building') AND gen NOT IN (%s)"
                % ",".join(str(g) for g in keep)
            )
            if st["state"] != "IDLE":
                # Take over with a fresh fencing token; the old worker (if
                # any) aborts at its next fenced step. Restart the candidate
                # from the backfill boundary: snapshot_seq is still valid and
                # the backfill is idempotent.
                cur = c.execute(
                    "UPDATE rebuild_state SET state='BACKFILL', owner=?, "
                    "applied_seq=snapshot_seq, paused_at=NULL "
                    "WHERE id=1 AND state != 'IDLE'",
                    (token,),
                )
                if cur.rowcount == 1:
                    resume_gen = st["gen"]
            else:
                # No rebuild in flight: the log is fully reflected in the
                # documents table and the active index.
                c.execute("DELETE FROM change_log")
        if resume_gen is not None:
            log.info("resuming interrupted rebuild of gen %s", resume_gen)
            self._start_worker(token, resume_gen)

    def close(self) -> None:
        self._stop.set()
        with self._pause_cond:
            self._pause_cond.notify_all()
        worker = self._worker
        if worker is not None:
            worker.join(timeout=5)
        self.store.close()


def _spans(text: str, config: TokenizerConfig, match_lists) -> List[dict]:
    """Map token-position spans back to code point ranges in the original
    text by re-tokenizing the very same document version."""
    by_pos = {t.pos: (t.start, t.end) for t in tokenize(text, config)}
    seen = set()
    spans = []
    for matches in match_lists:
        for p0, p1 in matches:
            s, e = by_pos[p0][0], by_pos[p1][1]
            if (s, e) not in seen:
                seen.add((s, e))
                spans.append({"start": s, "end": e, "text": text[s:e]})
    spans.sort(key=lambda x: (x["start"], x["end"]))
    return spans
