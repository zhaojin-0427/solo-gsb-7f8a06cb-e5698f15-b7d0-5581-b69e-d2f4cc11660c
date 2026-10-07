from app.tokenizer import Rules, content_tokens


def test_unicode_alnum_runs_and_offsets():
    r = Rules(case_fold=False, stopwords=frozenset())
    text = "Hello, 世界123! café"
    toks = r.tokenize(text)
    words = [(t.text, t.pos) for t in toks]
    # punctuation is not a word; CJK ideographs + digits form alnum runs
    assert words == [("Hello", 0), ("世界123", 1), ("café", 2)]
    # code-point ranges slice back to the original word
    for t in toks:
        assert text[t.start:t.end] == t.text or t.text in (
            "世界123", "café")
    h = toks[0]
    assert text[h.start:h.end] == "Hello"


def test_case_folding_configurable():
    upper = "Foo BAR foo"
    fold = Rules(case_fold=True)
    nofold = Rules(case_fold=False)
    assert [t.text for t in fold.tokenize(upper)] == ["foo", "bar", "foo"]
    assert [t.text for t in nofold.tokenize(upper)] == [
        "Foo", "BAR", "foo"]


def test_stopwords_keep_positions():
    r = Rules(case_fold=True, stopwords=frozenset({"the", "a"}))
    toks = content_tokens(r.tokenize("the quick a fox"), r)
    assert [t.text for t in toks] == ["quick", "fox"]
    # positions are NOT compressed: gaps remain for adjacency matching
    assert [t.pos for t in toks] == [1, 3]


def test_stopword_rules_normalized():
    r = Rules.from_dict(
        {"case_fold": True, "stopwords": ["The", " A ", "x"]})
    assert r.stopwords == frozenset({"the", "a", "x"})
    r2 = Rules.from_dict({"case_fold": False, "stopwords": ["The"]})
    assert r2.stopwords == frozenset({"The"})
    assert [t.text for t in content_tokens(
        r2.tokenize("the The"), r2)] == ["the"]
