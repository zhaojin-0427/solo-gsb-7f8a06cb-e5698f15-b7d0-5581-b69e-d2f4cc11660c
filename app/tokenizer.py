"""Unicode-aware tokenization and tokenizer rules.

Words are maximal runs of Unicode letters/numbers (``str.isalnum``).
Every token keeps its *original* position, so stopwords leave a gap
(positions are never compressed) — required for adjacency phrase
matching.  Offsets are code-point offsets into the Python ``str``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import List, Sequence


@dataclass(frozen=True)
class Token:
    text: str          # surface form (after case folding when enabled)
    pos: int           # word position, counting stopwords too
    start: int         # inclusive code-point offset
    end: int           # exclusive code-point offset

    def to_json(self) -> str:
        return json.dumps({"text": self.text, "pos": self.pos,
                           "start": self.start, "end": self.end},
                          ensure_ascii=False)


@dataclass(frozen=True)
class Rules:
    """Configurable tokenization rules for one index generation."""
    case_fold: bool = True
    stopwords: frozenset[str] = frozenset()

    def fold(self, word: str) -> str:
        return word.casefold() if self.case_fold else word

    def tokenize(self, text: str) -> List[Token]:
        out: List[Token] = []
        pos = 0
        n = len(text)
        i = 0
        while i < n:
            if not text[i].isalnum():
                i += 1
                continue
            j = i + 1
            while j < n and text[j].isalnum():
                j += 1
            word = text[i:j]
            folded = self.fold(word)
            out.append(Token(folded, pos, i, j))
            pos += 1
            i = j
        return out

    def to_dict(self) -> dict:
        return {"case_fold": self.case_fold,
                "stopwords": sorted(self.stopwords)}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: dict) -> "Rules":
        case_fold = bool(d.get("case_fold", True))
        raw = d.get("stopwords", []) or []
        if not isinstance(raw, (list, tuple, set)):
            raise ValueError("stopwords must be a list of strings")
        norm = set()
        for w in raw:
            if not isinstance(w, str):
                raise ValueError("stopwords must be a list of strings")
            w = w.strip()
            w = w.casefold() if case_fold else w
            if w:
                norm.add(w)
        return cls(case_fold=case_fold, stopwords=frozenset(norm))


def content_tokens(tokens: Sequence[Token], rules: Rules) -> List[Token]:
    """Tokens that survive the stopword filter; positions are kept."""
    return [t for t in tokens if t.text not in rules.stopwords]
