from app.tokenizer import TokenizerConfig, tokenize


def test_unicode_alnum_runs():
    cfg = TokenizerConfig(case_fold=True, stopwords=frozenset())
    toks = tokenize("Hello, 世界_2 x!", cfg)
    assert [t.term for t in toks] == ["hello", "世界", "2", "x"]
    assert [(t.start, t.end) for t in toks] == [(0, 5), (7, 9), (10, 11), (12, 13)]


def test_case_fold_configurable():
    folded = tokenize("Hello", TokenizerConfig(case_fold=True, stopwords=frozenset()))
    plain = tokenize("Hello", TokenizerConfig(case_fold=False, stopwords=frozenset()))
    assert folded[0].term == "hello"
    assert plain[0].term == "Hello"
    assert folded[0].start == plain[0].start == 0


def test_stopwords_keep_positions():
    cfg = TokenizerConfig(case_fold=True, stopwords=frozenset({"the"}))
    toks = tokenize("the cat and the dog", cfg)
    stops = [t for t in toks if t.is_stop]
    kept = [t for t in toks if not t.is_stop]
    assert [t.term for t in stops] == ["the", "the"]
    # positions are NOT compressed: dog sits at position 4, not 2
    assert [(t.term, t.pos) for t in kept] == [("cat", 1), ("and", 2), ("dog", 4)]


def test_config_roundtrip_and_validation():
    cfg = TokenizerConfig.from_dict({"case_fold": False, "stopwords": ["Foo"]})
    assert cfg.to_dict() == {"case_fold": False, "stopwords": ["Foo"]}
    assert TokenizerConfig.from_dict(cfg.to_dict()) == cfg
    for bad in ({"case_fold": "yes"}, {"stopwords": "the"}, {"nope": 1}):
        try:
            TokenizerConfig.from_dict(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted bad config {bad}")
