"""Query parsing: whole-word terms and adjacent-word phrases.

Syntax: whitespace separated terms, double-quoted spans are phrases.
Everything is ANDed. Stopwords are dropped from the query but keep their
position offsets inside phrases (positions are not compressed), so
``"quick the fox"`` with ``the`` as a stopword still requires the gap.
An empty query or a query with no searchable (non-stopword) term is rejected.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Tuple

from .tokenizer import TokenizerConfig, tokenize


class QueryError(ValueError):
    pass


@dataclass(frozen=True)
class TermPart:
    term: str

    def terms(self) -> List[str]:
        return [self.term]


@dataclass(frozen=True)
class PhrasePart:
    words: Tuple[str, ...]
    # offsets[i] = token distance of words[i] from words[0] in the original
    # query token stream (stopword gaps preserved).
    offsets: Tuple[int, ...]

    def terms(self) -> List[str]:
        return list(self.words)


_PART_RE = re.compile(r'"([^"]*)"|([^\s"]+)')


def parse_query(q: str, config: TokenizerConfig) -> List:
    if q is None or not q.strip():
        raise QueryError("empty query")
    if q.count('"') % 2:
        raise QueryError("unmatched double quote")
    parts: List = []
    for m in _PART_RE.finditer(q):
        quoted, bare = m.group(1), m.group(2)
        text = quoted if quoted is not None else bare
        kept = [t for t in tokenize(text, config) if not t.is_stop]
        if not kept:
            continue
        if quoted is not None:
            if len(kept) == 1:
                parts.append(TermPart(kept[0].term))
            else:
                base = kept[0].pos
                parts.append(PhrasePart(
                    tuple(t.term for t in kept),
                    tuple(t.pos - base for t in kept),
                ))
        else:
            parts.extend(TermPart(t.term) for t in kept)
    if not parts:
        raise QueryError("query has no searchable terms (only stopwords)")
    return parts
