"""Drill: park the rebuild worker at the backfill / catchup / switch
boundaries and hammer the service with writes, deletes and queries."""
from conftest import doc_ids, search, wait_idle, wait_paused

NEW_CONFIG = {"case_fold": True, "stopwords": []}


def test_full_boundary_walk(client):
    client.put("/docs/d1", json={"text": "alpha beta"})
    client.put("/docs/d2", json={"text": "gamma delta"})
    client.put("/admin/rebuild/pause-points",
               json={"points": ["backfill", "catchup", "switch"]})
    r = client.post("/index/rebuild", json={"config": NEW_CONFIG})
    assert r.status_code == 202
    new_gen = r.json()["gen"]

    # --- parked at the backfill boundary: candidate is empty -------------
    wait_paused(client, "backfill")
    client.put("/docs/d3", json={"text": "epsilon zeta"})
    client.delete("/docs/d1")
    # queries keep working on the complete current index, writes included
    assert doc_ids(search(client, "epsilon")) == ["d3"]
    assert doc_ids(search(client, "alpha")) == []
    assert search(client, "gamma")["gen"] == 1
    client.post("/admin/rebuild/resume")

    # --- parked at the catchup boundary ---------------------------------
    wait_paused(client, "catchup")
    client.put("/docs/d4", json={"text": "theta iota"})
    client.post("/admin/rebuild/resume")

    # --- parked at the switch boundary ----------------------------------
    wait_paused(client, "switch")
    client.put("/docs/d5", json={"text": "kappa lambda"})
    client.delete("/docs/d3")
    assert doc_ids(search(client, "kappa")) == ["d5"]  # still served by gen 1
    client.post("/admin/rebuild/resume")

    wait_idle(client)
    assert client.get("/tokenizer").json()["gen"] == new_gen
    assert doc_ids(search(client, "theta")) == ["d4"]
    assert doc_ids(search(client, "kappa")) == ["d5"]
    assert doc_ids(search(client, "epsilon")) == []      # deleted at switch
    assert doc_ids(search(client, "alpha")) == []        # deleted at backfill
    assert doc_ids(search(client, "gamma")) == ["d2"]
    # new tokenizer rules are live: stopword list is empty now
    client.put("/docs/d6", json={"text": "the end"})
    assert doc_ids(search(client, "the")) == ["d6"]


def test_resume_without_pause_is_409(client):
    r = client.post("/admin/rebuild/resume")
    assert r.status_code == 409


def test_invalid_pause_point_rejected(client):
    r = client.put("/admin/rebuild/pause-points", json={"points": ["nope"]})
    assert r.status_code == 400


def test_pause_points_reported(client):
    client.put("/admin/rebuild/pause-points", json={"points": ["switch"]})
    info = client.get("/admin/rebuild/pause-points").json()
    assert info["armed"] == ["switch"]
    client.put("/admin/rebuild/pause-points", json={"points": []})
    assert client.get("/admin/rebuild/pause-points").json()["armed"] == []
