"""SQLite storage layer: documents, versions, change log, generations.

A *generation* is a complete index built under one set of
:class:`~app.tokenizer.Rules`.  Exactly one generation is ``current``
(the one served to queries); generations being rebuilt are ``building``;
a generation superseded by a switch becomes ``retired`` and its token
rows are deleted once in-flight readers drain (see manager).
"""
from __future__ import annotations

import os
import sqlite3
import time
from typing import Iterable, List, Optional, Sequence, Tuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS docs (
    id              TEXT PRIMARY KEY,
    current_version INTEGER NOT NULL,
    deleted         INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS doc_versions (
    id      TEXT NOT NULL,
    version INTEGER NOT NULL,
    text    TEXT NOT NULL,
    PRIMARY KEY (id, version)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS changes (
    seq      INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id   TEXT NOT NULL,
    version  INTEGER,          -- NULL when kind = 'delete'
    kind     TEXT NOT NULL     -- 'upsert' | 'delete'
);

CREATE TABLE IF NOT EXISTS gen_meta (
    gen        INTEGER PRIMARY KEY,
    state      TEXT NOT NULL,  -- building | current | retired
    rules      TEXT NOT NULL,
    watermark  INTEGER NOT NULL DEFAULT -1,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS tokens (
    gen         INTEGER NOT NULL,
    term        TEXT NOT NULL,
    doc_id      TEXT NOT NULL,
    doc_version INTEGER NOT NULL,
    pos         INTEGER NOT NULL,
    start_off   INTEGER NOT NULL,
    end_off     INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tokens_term ON tokens(term, gen);
CREATE INDEX IF NOT EXISTS idx_tokens_doc  ON tokens(gen, doc_id);
"""


# --------------------------------------------------------------------- connect

def connect(path: str, *, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        uri = f"file:{os.path.abspath(path)}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=30,
                               isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn
    conn = sqlite3.connect(os.path.abspath(path), timeout=30,
                           isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA wal_autocheckpoint = 1000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


# ---------------------------------------------------------------------- meta

def meta_get(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value))


def active_generation(conn: sqlite3.Connection) -> Optional[Tuple[int, int]]:
    """Return ``(gen, watermark)`` of the current generation, if any."""
    g = meta_get(conn, "active_gen")
    if g is None:
        return None
    w = meta_get(conn, "active_watermark") or "-1"
    return int(g), int(w)


# --------------------------------------------------------------- docs/changes

def upsert_document(conn: sqlite3.Connection, doc_id: str, text: str,
                    ) -> Tuple[int, int]:
    """Insert a new document version and append the change log entry.

    Caller owns the transaction.  Returns ``(version, seq)``.
    """
    row = conn.execute(
        "SELECT current_version FROM docs WHERE id=?", (doc_id,)).fetchone()
    version = (row["current_version"] + 1) if row else 1
    conn.execute(
        "INSERT INTO doc_versions(id, version, text) VALUES(?, ?, ?) "
        "ON CONFLICT(id, version) DO NOTHING",
        (doc_id, version, text))
    if row is None:
        conn.execute(
            "INSERT INTO docs(id, current_version, deleted) VALUES(?, ?, 0)",
            (doc_id, version))
    else:
        conn.execute(
            "UPDATE docs SET current_version=?, deleted=0 WHERE id=?",
            (version, doc_id))
    cur = conn.execute(
        "INSERT INTO changes(doc_id, version, kind) VALUES(?, ?, 'upsert')",
        (doc_id, version))
    return version, cur.lastrowid


def delete_document(conn: sqlite3.Connection, doc_id: str
                    ) -> Optional[Tuple[int, int]]:
    """Mark a document deleted + log it.  Returns ``(current_version, seq)``
    or ``None`` if the document is unknown/already deleted."""
    row = conn.execute(
        "SELECT current_version, deleted FROM docs WHERE id=?",
        (doc_id,)).fetchone()
    if row is None or row["deleted"]:
        return None
    conn.execute("UPDATE docs SET deleted=1 WHERE id=?", (doc_id,))
    cur = conn.execute(
        "INSERT INTO changes(doc_id, version, kind) VALUES(?, NULL, 'delete')",
        (doc_id,))
    return row["current_version"], cur.lastrowid


def get_document(conn: sqlite3.Connection, doc_id: str
                 ) -> Optional[Tuple[int, str, int]]:
    row = conn.execute(
        "SELECT d.current_version, d.deleted, v.text "
        "FROM docs d JOIN doc_versions v "
        "  ON v.id=d.id AND v.version=d.current_version "
        "WHERE d.id=?", (doc_id,)).fetchone()
    if row is None or row["deleted"]:
        return None
    return row["current_version"], row["text"], 0


def get_version_text(conn: sqlite3.Connection, doc_id: str, version: int
                     ) -> Optional[str]:
    row = conn.execute(
        "SELECT text FROM doc_versions WHERE id=? AND version=?",
        (doc_id, version)).fetchone()
    return row["text"] if row else None


def max_change_seq(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM changes").fetchone()
    return row["m"]


def changes_after(conn: sqlite3.Connection, seq: int
                  ) -> List[sqlite3.Row]:
    return list(conn.execute(
        "SELECT seq, doc_id, version, kind FROM changes "
        "WHERE seq > ? ORDER BY seq", (seq,)))


def list_snapshot_docs(conn: sqlite3.Connection) -> List[Tuple[str, int]]:
    """All live ``(doc_id, current_version)`` rows — for rebuild backfill."""
    return [(r["id"], r["current_version"])
            for r in conn.execute(
                "SELECT id, current_version FROM docs WHERE deleted=0")]


# ------------------------------------------------------------- gen meta/tokens

def create_generation(conn: sqlite3.Connection, gen: int, rules_json: str) -> None:
    conn.execute(
        "INSERT INTO gen_meta(gen, state, rules, watermark, created_at) "
        "VALUES(?, 'building', ?, -1, ?)",
        (gen, rules_json, time.time()))


def get_generation(conn: sqlite3.Connection, gen: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM gen_meta WHERE gen=?", (gen,)).fetchone()


def generations_by_state(conn: sqlite3.Connection, state: str
                         ) -> List[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM gen_meta WHERE state=? ORDER BY gen", (state,)))


def set_gen_watermark(conn: sqlite3.Connection, gen: int, watermark: int) -> None:
    conn.execute("UPDATE gen_meta SET watermark=? WHERE gen=?",
                 (watermark, gen))


def atomic_switch(conn: sqlite3.Connection, new_gen: int, watermark: int,
                  ) -> None:
    """Promote ``new_gen`` to current, retire the old current.

    The final increment of the candidate index (all changes after
    ``watermark``) happens in the *same* transaction, so any committed
    write is either already reflected or — if it committed after the
    change scan — is applied here before the swap becomes visible.
    """
    old = active_generation(conn)
    conn.execute(
        "UPDATE gen_meta SET state='current', watermark=? WHERE gen=?",
        (watermark, new_gen))
    if old is not None:
        conn.execute("UPDATE gen_meta SET state='retired' WHERE gen=?",
                     (old[0],))
    meta_set(conn, "active_gen", str(new_gen))
    meta_set(conn, "active_watermark", str(watermark))


def insert_token_rows(conn: sqlite3.Connection,
                      rows: Sequence[Tuple[int, str, str, int, int, int, int]]
                      ) -> None:
    """Rows: ``(gen, term, doc_id, doc_version, pos, start, end)``."""
    conn.executemany(
        "INSERT INTO tokens(gen, term, doc_id, doc_version, pos, "
        " start_off, end_off) VALUES(?,?,?,?,?,?,?)", rows)


def delete_gen_doc(conn: sqlite3.Connection, gen: int, doc_id: str) -> None:
    conn.execute("DELETE FROM tokens WHERE gen=? AND doc_id=?", (gen, doc_id))


def delete_gen_tokens(conn: sqlite3.Connection, gen: int) -> None:
    conn.execute("DELETE FROM tokens WHERE gen=?", (gen,))
    conn.execute("DELETE FROM gen_meta WHERE gen=?", (gen,))


def gc_doc_versions(conn: sqlite3.Connection) -> int:
    """Drop version rows nobody can read anymore.

    Kept when referenced by any non-retired generation, or when it is a
    live document's current or immediately-previous version (the latter
    covers a query that read its hit from the index just as the document
    was updated and the old index retired).
    """
    cur = conn.execute(
        "DELETE FROM doc_versions "
        "WHERE NOT EXISTS ("
        "  SELECT 1 FROM docs d "
        "  WHERE d.id=doc_versions.id AND d.deleted=0 "
        "    AND d.current_version - doc_versions.version <= 1) "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM tokens t JOIN gen_meta g ON g.gen=t.gen "
        "  WHERE t.doc_id=doc_versions.id "
        "    AND t.doc_version=doc_versions.version "
        "    AND g.state <> 'retired')")
    return cur.rowcount


# ----------------------------------------------------------------- searching

def search_term(conn: sqlite3.Connection, gen: int, term: str, limit: int
                ) -> Tuple[List[dict], bool]:
    """All occurrences of ``term`` under ``gen`` grouped by document.

    Returns ``(hits, truncated)``.
    """
    rows = conn.execute(
        "SELECT doc_id, doc_version, start_off, end_off FROM tokens "
        "WHERE gen=? AND term=? ORDER BY doc_id, pos",
        (gen, term)).fetchall()
    hits: List[dict] = []
    cur_doc = None
    truncated = False
    for r in rows:
        if cur_doc is None or cur_doc["doc_id"] != r["doc_id"]:
            if len(hits) >= limit:
                truncated = True
                break
            cur_doc = {"doc_id": r["doc_id"],
                       "doc_version": r["doc_version"],
                       "spans": [], "kind": "term", "term": term}
            hits.append(cur_doc)
        cur_doc["spans"].append([r["start_off"], r["end_off"]])
    return hits, truncated


def search_phrase(conn: sqlite3.Connection, gen: int, terms: Sequence[str],
                  limit: int, candidate_cap: int = 65536
                  ) -> Tuple[List[dict], bool]:
    """Adjacency phrase search: terms occur at consecutive word positions.

    Stopwords in the *query* are removed before calling this; the stored
    positions are uncompressed (stopwords leave gaps), so a stopword
    sitting between two query words correctly breaks adjacency.

    Returns ``(hits, truncated)``; each hit carries every matched phrase
    span and the spans of the individual matched terms.
    """
    placeholders = ",".join("?" for _ in terms)
    rows = conn.execute(
        f"SELECT doc_id, doc_version, term, pos, start_off, end_off "
        f"FROM tokens WHERE gen=? AND term IN ({placeholders}) "
        f"ORDER BY doc_id, doc_version, pos LIMIT ?",
        (gen, *terms, candidate_cap)).fetchall()
    truncated_candidates = len(rows) == candidate_cap

    # group candidate tokens per (doc, version), keyed by position.
    # a word position maps to exactly one surface term.
    groups: dict = {}
    order: List[Tuple[str, int]] = []
    for r in rows:
        key = (r["doc_id"], r["doc_version"])
        g = groups.get(key)
        if g is None:
            g = {}
            groups[key] = g
            order.append(key)
        g[r["pos"]] = (r["term"], r["start_off"], r["end_off"])

    hits: List[dict] = []
    truncated = truncated_candidates
    for (doc_id, doc_version) in order:
        if len(hits) >= limit:
            truncated = True
            break
        by_pos = groups[(doc_id, doc_version)]
        phrases: List[List[int]] = []
        term_spans: List[List[int]] = []
        for p in by_pos:
            first = by_pos.get(p)
            if first is None or first[0] != terms[0]:
                continue
            ok = True
            for k in range(1, len(terms)):
                t = by_pos.get(p + k)
                if t is None or t[0] != terms[k]:
                    ok = False
                    break
            if ok:
                start = first[1]
                end = by_pos[p + len(terms) - 1][2]
                phrases.append([start, end])
                for k in range(len(terms)):
                    _, s, e = by_pos[p + k]
                    term_spans.append([s, e])
        if phrases:
            hits.append({"doc_id": doc_id, "doc_version": doc_version,
                         "spans": phrases, "term_spans": _dedup(term_spans),
                         "kind": "phrase", "terms": list(terms)})
    return hits, truncated


def _dedup(spans: Iterable[List[int]]) -> List[List[int]]:
    seen = set()
    out = []
    for s in spans:
        t = tuple(s)
        if t not in seen:
            seen.add(t)
            out.append(s)
    return out
