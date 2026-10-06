"""Tokenization: runs of Unicode letters/digits, configurable case folding
and stopwords. Stopwords keep their token positions (they are not indexed,
but the position counter is not compressed)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import FrozenSet, List

# Consecutive Unicode letters/digits (underscore and everything else split).
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)

DEFAULT_STOPWORDS = (
    "a", "an", "the", "and", "or", "but", "of", "to", "in", "on",
    "is", "it", "as", "at", "by", "for", "with",
)


@dataclass(frozen=True)
class TokenizerConfig:
    case_fold: bool = True
    stopwords: FrozenSet[str] = field(default_factory=lambda: frozenset(DEFAULT_STOPWORDS))

    @classmethod
    def from_dict(cls, d) -> "TokenizerConfig":
        if d is None:
            return cls()
        if not isinstance(d, dict):
            raise ValueError("tokenizer config must be an object")
        case_fold = d.get("case_fold", True)
        if not isinstance(case_fold, bool):
            raise ValueError("case_fold must be a boolean")
        stopwords = d.get("stopwords", list(DEFAULT_STOPWORDS))
        if not isinstance(stopwords, list) or not all(isinstance(s, str) for s in stopwords):
            raise ValueError("stopwords must be a list of strings")
        unknown = set(d) - {"case_fold", "stopwords"}
        if unknown:
            raise ValueError(f"unknown config keys: {sorted(unknown)}")
        return cls(case_fold=case_fold, stopwords=frozenset(stopwords))

    def to_dict(self) -> dict:
        return {"case_fold": self.case_fold, "stopwords": sorted(self.stopwords)}

    def effective_stopwords(self) -> FrozenSet[str]:
        if self.case_fold:
            return frozenset(s.casefold() for s in self.stopwords)
        return self.stopwords


@dataclass(frozen=True)
class Token:
    term: str
    start: int  # code point offset, inclusive
    end: int    # code point offset, exclusive
    pos: int    # token position; stopwords occupy positions too
    is_stop: bool


def tokenize(text: str, config: TokenizerConfig) -> List[Token]:
    stops = config.effective_stopwords()
    out: List[Token] = []
    for pos, m in enumerate(_TOKEN_RE.finditer(text)):
        raw = m.group(0)
        term = raw.casefold() if config.case_fold else raw
        out.append(Token(term=term, start=m.start(), end=m.end(), pos=pos,
                         is_stop=term in stops))
    return out
