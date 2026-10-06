import threading

from conftest import doc_ids, search


def test_whole_word_and_codepoint_spans(client):
    client.put("/docs/a", json={"text": "héllo wörld, héllo again"})
    client.put("/docs/b", json={"text": "concatenate hélloWörld"})
    r = search(client, "héllo")
    assert doc_ids(r) == ["a"]  # whole word: no substring match inside "hélloWörld"
    spans = r["results"][0]["spans"]
    assert (spans[0]["start"], spans[0]["end"], spans[0]["text"]) == (0, 5, "héllo")
    assert (spans[1]["start"], spans[1]["end"], spans[1]["text"]) == (13, 18, "héllo")


def test_phrase_query(client):
    client.put("/docs/a", json={"text": "the quick brown fox jumps"})
    client.put("/docs/b", json={"text": "quick brown"})
    client.put("/docs/c", json={"text": "brown quick fox"})
    r = search(client, '"quick brown fox"')
    assert doc_ids(r) == ["a"]
    span = r["results"][0]["spans"][0]
    assert (span["start"], span["end"], span["text"]) == (4, 19, "quick brown fox")


def test_stopwords_do_not_compress_positions(client):
    # default config treats "the" as a stopword and it keeps its position
    client.put("/docs/a", json={"text": "quick the fox jumped"})
    assert doc_ids(search(client, '"quick fox"')) == []          # gap of one
    r = search(client, '"quick the fox"')                        # stopword gap preserved
    assert doc_ids(r) == ["a"]
    span = r["results"][0]["spans"][0]
    assert span["text"] == "quick the fox"


def test_and_semantics_and_multiple_terms(client):
    client.put("/docs/a", json={"text": "red green blue"})
    client.put("/docs/b", json={"text": "red green"})
    assert doc_ids(search(client, "red blue")) == ["a"]
    assert doc_ids(search(client, "red green")) == ["a", "b"]


def test_update_and_delete(client):
    client.put("/docs/a", json={"text": "alpha beta"})
    assert doc_ids(search(client, "alpha")) == ["a"]
    client.put("/docs/a", json={"text": "gamma delta"})
    assert doc_ids(search(client, "alpha")) == []
    assert doc_ids(search(client, "gamma")) == ["a"]
    r = client.delete("/docs/a")
    assert r.json()["deleted"] is True
    assert doc_ids(search(client, "gamma")) == []
    assert client.delete("/docs/a").json()["deleted"] is False
    assert client.get("/docs/a").status_code == 404


def test_empty_and_stopword_only_queries_rejected(client):
    client.put("/docs/a", json={"text": "the quick brown fox"})
    for bad in ("", "   ", "the", "the and of", '"the and"'):
        r = client.get("/search", params={"q": bad})
        assert r.status_code == 400, bad
    assert client.get("/search", params={"q": '"unmatched'}).status_code == 400


def test_result_version_and_text_consistency_under_writes(client):
    # A query's spans must always match the very document version returned.
    client.put("/docs/x", json={"text": "seed"})
    errors = []

    def writer():
        try:
            for i in range(60):
                client.put("/docs/x", json={"text": f"apple turnover {i}"})
                client.put("/docs/x", json={"text": f"banana split {i}"})
        except Exception as e:  # pragma: no cover
            errors.append(e)

    t = threading.Thread(target=writer)
    t.start()
    while t.is_alive():
        for term, forbidden in (("apple", "banana"), ("banana", "apple")):
            r = search(client, term)
            for hit in r["results"]:
                for span in hit["spans"]:
                    if span["text"] == forbidden:
                        errors.append(f"stale span: {span}")
        t.join(0.001)
    t.join()
    assert not errors
