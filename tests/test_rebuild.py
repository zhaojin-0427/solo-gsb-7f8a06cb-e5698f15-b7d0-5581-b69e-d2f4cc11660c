from conftest import doc_ids, search, wait_for, wait_idle, wait_paused

NEW_CONFIG = {"case_fold": True, "stopwords": []}


def test_rebuild_switches_tokenizer_rules(client):
    client.put("/docs/a", json={"text": "The Cat sat"})
    # default rules: "the" is a stopword, so it cannot be searched at all
    assert client.get("/search", params={"q": "the"}).status_code == 400
    r = client.post("/index/rebuild", json={"config": NEW_CONFIG})
    assert r.status_code == 202
    wait_idle(client)
    tok = client.get("/tokenizer").json()
    assert tok["config"]["stopwords"] == []
    assert tok["gen"] == r.json()["gen"]
    assert doc_ids(search(client, "the")) == ["a"]


def test_writes_during_rebuild_are_visible_after_switch(client):
    for i in range(5):
        client.put(f"/docs/base{i}", json={"text": f"base term{i}"})
    client.put("/admin/rebuild/pause-points", json={"points": ["switch"]})
    client.post("/index/rebuild", json={"config": NEW_CONFIG})
    wait_paused(client, "switch")
    # acknowledged while the worker is parked at the switch boundary
    client.put("/docs/late", json={"text": "late arrival"})
    client.delete("/docs/base0")
    client.post("/admin/rebuild/resume")
    wait_idle(client)
    assert doc_ids(search(client, "late")) == ["late"]
    assert doc_ids(search(client, "term0")) == []          # delete did not resurrect
    assert doc_ids(search(client, "term1")) == ["base1"]


def test_only_one_rebuild_wins(client):
    client.put("/admin/rebuild/pause-points", json={"points": ["backfill"]})
    r1 = client.post("/index/rebuild", json={"config": NEW_CONFIG})
    assert r1.status_code == 202
    wait_paused(client, "backfill")
    r2 = client.post("/index/rebuild", json={"config": NEW_CONFIG})
    assert r2.status_code == 409
    client.post("/admin/rebuild/resume")
    wait_idle(client)


def test_cancel_never_switches_half_built_index(client):
    client.put("/docs/a", json={"text": "keep me"})
    client.put("/admin/rebuild/pause-points", json={"points": ["backfill"]})
    client.post("/index/rebuild", json={"config": NEW_CONFIG})
    wait_paused(client, "backfill")
    assert client.post("/index/rebuild/cancel").json()["cancelled"] is True
    status = client.get("/index/rebuild").json()
    assert status["state"] == "IDLE"
    # still on the original generation and tokenizer rules
    assert client.get("/tokenizer").json()["gen"] == 1
    assert doc_ids(search(client, "keep")) == ["a"]
    gens = {i["gen"]: i["status"] for i in client.get("/admin/indexes").json()["indexes"]}
    assert gens[1] == "active" and gens[2] == "cancelled"
    assert client.post("/index/rebuild/cancel").json()["cancelled"] is False


def test_old_generation_reclaimed_after_readers_finish(client):
    client.put("/docs/a", json={"text": "some text"})
    client.post("/index/rebuild", json={"config": NEW_CONFIG})
    wait_idle(client)
    wait_for(
        lambda: {i["gen"]: i["status"] for i in client.get("/admin/indexes").json()["indexes"]}.get(1) == "dropped",
        msg="old generation was not reclaimed",
    )
    idx = {i["gen"]: i for i in client.get("/admin/indexes").json()["indexes"]}
    assert idx[1]["postings"] == 0
    assert idx[2]["status"] == "active" and idx[2]["postings"] > 0


def test_gc_waits_for_inflight_reader(client):
    service = client.app.state.service
    client.put("/docs/a", json={"text": "some text"})
    with service._leases.lease(1):  # simulate a slow reader on gen 1
        client.post("/index/rebuild", json={"config": NEW_CONFIG})
        wait_idle(client)
        idx = {i["gen"]: i["status"] for i in client.get("/admin/indexes").json()["indexes"]}
        assert idx[1] == "retired"  # switched but not reclaimed yet
    wait_for(
        lambda: {i["gen"]: i["status"] for i in client.get("/admin/indexes").json()["indexes"]}[1] == "dropped",
        msg="gen 1 not reclaimed after reader left",
    )


def test_rebuild_without_config_keeps_rules(client):
    client.put("/docs/a", json={"text": "hello there"})
    before = client.get("/tokenizer").json()
    client.post("/index/rebuild")
    wait_idle(client)
    after = client.get("/tokenizer").json()
    assert after["gen"] == before["gen"] + 1
    assert after["config"] == before["config"]
    assert doc_ids(search(client, "hello")) == ["a"]


def test_bad_rebuild_config_rejected(client):
    r = client.post("/index/rebuild", json={"config": {"case_fold": "yes"}})
    assert r.status_code == 400
