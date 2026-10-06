"""Posting-list maintenance and per-generation index search."""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Dict, List, Tuple

from .query import PhrasePart, TermPart
from .tokenizer import TokenizerConfig, tokenize


def index_document(conn, gen: int, doc_id: str, text: str, config: TokenizerConfig) -> None:
    """(Re)build the postings of one document inside generation ``gen``.
    Idempotent: existing postings for the document are replaced."""
    conn.execute("DELETE FROM postings WHERE gen=? AND doc_id=?", (gen, doc_id))
    positions: Dict[str, List[int]] = defaultdict(list)
    for tok in tokenize(text, config):
        if not tok.is_stop:
            positions[tok.term].append(tok.pos)
    conn.executemany(
        "INSERT INTO postings(gen, term, doc_id, positions) VALUES (?,?,?,?)",
        [(gen, term, doc_id, json.dumps(pos)) for term, pos in positions.items()],
    )


def remove_document(conn, gen: int, doc_id: str) -> None:
    conn.execute("DELETE FROM postings WHERE gen=? AND doc_id=?", (gen, doc_id))


def fetch_postings(conn, gen: int, terms: List[str]) -> Dict[str, Dict[str, List[int]]]:
    out: Dict[str, Dict[str, List[int]]] = {}
    for term in terms:
        rows = conn.execute(
            "SELECT doc_id, positions FROM postings WHERE gen=? AND term=?",
            (gen, term),
        ).fetchall()
        out[term] = {r["doc_id"]: json.loads(r["positions"]) for r in rows}
    return out


def part_matches(part, postings: Dict[str, Dict[str, List[int]]]) -> Dict[str, List[Tuple[int, int]]]:
    """Return {doc_id: [(pos_start, pos_end), ...]} token-position spans."""
    if isinstance(part, TermPart):
        return {
            d: [(p, p) for p in poss]
            for d, poss in postings.get(part.term, {}).items()
        }
    # PhrasePart: every word must appear at base_position + offset.
    words, offsets = part.words, part.offsets
    first = postings.get(words[0], {})
    others = [postings.get(w, {}) for w in words[1:]]
    result: Dict[str, List[Tuple[int, int]]] = {}
    for doc_id, poss in first.items():
        other_sets = []
        for o in others:
            ps = o.get(doc_id)
            if ps is None:
                break
            other_sets.append(set(ps))
        else:
            hits = [
                (p, p + offsets[-1])
                for p in poss
                if all((p + off) in s for off, s in zip(offsets[1:], other_sets))
            ]
            if hits:
                result[doc_id] = hits
    return result
