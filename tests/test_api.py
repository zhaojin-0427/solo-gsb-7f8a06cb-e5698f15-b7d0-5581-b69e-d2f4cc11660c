def test_put_get_delete_search(client):
    r = client.put("/documents/d1", json={"text": "The quick brown fox"})
    assert r.status_code == 200
    assert r.json()["version"] == 1

    r = client.get("/documents/d1")
    assert r.status_code == 200
    assert r.json()["text"] == "The quick brown fox"

    # whole word
    r = client.get("/search", params={"q": "brown"})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    hit = body["hits"][0]
    assert hit["doc_id"] == "d1"
    assert hit["spans"] == [[10, 15]]

    # case folding (default rules)
    assert client.get("/search", params={"q": "QUICK"}).json()["count"] == 1

    # partial word must not match
    assert client.get("/search", params={"q": "brow"}).json()["count"] == 0

    # delete
    assert client.delete("/documents/d1").status_code == 200
    assert client.delete("/documents/d1").status_code == 404
    assert client.get("/search", params={"q": "brown"}).json()["count"] == 0
    assert client.get("/documents/d1").status_code == 404


def test_update_creates_new_versions_and_reindexes(client):
    client.put("/documents/d1", json={"text": "alpha alpha"})
    client.put("/documents/d1", json={"text": "beta beta beta"})
    body = client.get("/search", params={"q": "alpha"}).json()
    assert body["count"] == 0
    body = client.get("/search", params={"q": "beta"}).json()
    assert body["hits"][0]["spans"] == [[0, 4], [5, 9], [10, 14]]
    assert body["hits"][0]["doc_version"] == 2


def test_empty_and_stopword_queries_rejected(client):
    client.put("/documents/d1", json={"text": "hello world"})
    # rebuild with stopwords
    client.post("/rebuild", json={
        "rules": {"case_fold": True, "stopwords": ["the", "a"]}})
    client.manager.wait_rebuild(timeout=30)

    for q in ["", "   ", "!!!", "the a", "THE"]:
        r = client.get("/search", params={"q": q})
        assert r.status_code == 400, q


def test_phrase_adjacency_and_stopword_gap(client):
    client.put("/documents/d1",
               json={"text": "the quick fox and the quick red fox"})
    r = client.get("/search", params={"q": '"quick fox"'})
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "phrase"
    # one occurrence (positions 1,2); "quick red fox" is not adjacent
    assert body["hits"][0]["spans"] == [[4, 13]]
    assert body["hits"][0]["term_spans"] == [[4, 9], [10, 13]]

    # stopword gap breaks adjacency: "fox ... the ... quick" not adjacent
    assert client.get("/search", params={
        "q": '"fox quick"'}).json()["count"] == 0


def test_phrase_after_switch_with_stopword_rules(client):
    client.put("/documents/d1", json={"text": "a quick a fox"})
    # 'a' is a stopword: stored positions 1 (quick) and 3 (fox) ->
    # adjacency must NOT match across the stopword gap
    client.post("/rebuild", json={
        "rules": {"case_fold": True, "stopwords": ["a"]}})
    client.manager.wait_rebuild(timeout=30)
    body = client.get("/search", params={"q": '"quick fox"'}).json()
    assert body["count"] == 0
    assert body["rules"]["stopwords"] == ["a"]
    # whole word still works
    assert client.get("/search", params={"q": "quick"}).json()["count"] == 1


def test_multiline_unicode_ranges(client):
    text = "Erste Zeile\nzweite Zeile 😀\n日本語 テスト"
    client.put("/documents/d1", json={"text": text})
    body = client.get("/search", params={"q": "zeile"}).json()
    assert body["hits"][0]["spans"] == [[6, 11], [19, 24]]
    for s, e in body["hits"][0]["spans"]:
        assert text[s:e].lower() == "zeile"
    body = client.get("/search", params={"q": "日本語"}).json()
    s, e = body["hits"][0]["spans"][0]
    assert text[s:e] == "日本語"


def test_term_or_bare_multiword(client):
    client.put("/documents/d1", json={"text": "foo bar"})
    client.put("/documents/d2", json={"text": "baz foo"})
    body = client.get("/search", params={"q": "foo baz"}).json()
    assert body["kind"] == "term_or"
    docs = {h["doc_id"]: h for h in body["hits"]}
    assert set(docs) == {"d1", "d2"}
    assert [m["term"] for m in docs["d1"]["matches"]] == ["foo"]
    assert sorted(m["term"] for m in docs["d2"]["matches"]) == ["baz", "foo"]


def test_get_specific_version(client):
    client.put("/documents/d1", json={"text": "vone text"})
    client.put("/documents/d1", json={"text": "vtwo text"})
    r = client.get("/documents/d1", params={"version": 1})
    assert r.status_code == 200
    assert r.json()["text"] == "vone text"
    assert client.get("/documents/d1", params={"version": 9}).status_code == 404
