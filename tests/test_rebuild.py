import threading
import time

from app import db
from app.tokenizer import Rules
from tests.conftest import wait_phase


def test_rebuild_switches_rules(manager):
    for i in range(20):
        manager.upsert(f"d{i}", f"Document number {i} the end")
    old_gen = manager.current()[0]

    rules = Rules(case_fold=False, stopwords=frozenset({"the"}))
    manager.start_rebuild(rules)
    assert manager.wait_rebuild(timeout=30)
    gen, wm, active_rules = manager.current()
    assert gen != old_gen
    assert active_rules is rules or active_rules.case_fold is False
    assert active_rules.stopwords == frozenset({"the"})

    # case folding no longer applies: uppercase query misses
    assert manager.search(["document"], "term", 100)["count"] == 0
    assert manager.search(["Document"], "term", 100)["count"] == 20
    # stopword gone
    assert manager.search(["the"], "term", 100)["count"] == 0
    # state is consistent
    state = manager.meta_state()
    assert state["active"]["gen"] == gen
    assert all(g["state"] != "retired" or g["gen"] == old_gen
               for g in state["generations"])
    # retired token rows reclaimed
    conn = db.connect(manager.path, readonly=True)
    n = conn.execute("SELECT COUNT(*) c FROM tokens WHERE gen=?",
                     (old_gen,)).fetchone()["c"]
    conn.close()
    assert n == 0


def test_concurrent_rebuild_only_one_winner(manager):
    manager.upsert("d1", "hello world")
    manager.start_rebuild(Rules(stopwords=frozenset({"a"})))
    from app.manager import RebuildRunning
    try:
        manager.start_rebuild(Rules(stopwords=frozenset({"b"})))
        assert False, "expected RebuildRunning"
    except RebuildRunning as e:
        assert e.status is not None
    assert manager.wait_rebuild(timeout=30)
    # slot released after finish -> another rebuild works
    manager.start_rebuild(Rules())
    assert manager.wait_rebuild(timeout=30)


def test_cancel_discards_candidate(manager):
    for i in range(10):
        manager.upsert(f"d{i}", f"word {i}")
    gen_before = manager.current()[0]
    manager.start_rebuild(Rules(stopwords=frozenset({"word"})))
    st = manager.cancel_rebuild()
    assert st["cancelled"] is True
    assert manager.wait_rebuild(timeout=30)
    # still on the old generation/rules
    assert manager.current()[0] == gen_before
    assert manager.search(["word"], "term", 100)["count"] == 10
    conn = db.connect(
        manager.path, readonly=True)
    gens = [r["gen"] for r in conn.execute(
        "SELECT gen FROM gen_meta WHERE state='building'")]
    conn.close()
    assert gens == []


def test_writes_during_rebuild_are_searchable_after_switch(manager):
    for i in range(30):
        manager.upsert(f"d{i}", f"alpha {i}")
    manager.start_rebuild(Rules(stopwords=frozenset({"the"})),
                          pause_at="catchup")
    job = manager._job
    wait_phase(manager, "paused:catchup")

    # concurrent writes while caught up + paused: upsert, update, delete
    manager.upsert("d30", "alpha fresh thirty")
    manager.upsert("d0", "alpha REPLACED content")
    manager.delete("d1")
    manager.upsert("d31", "alpha thirty one")
    manager.delete("d31")

    job.resume()
    assert manager.wait_rebuild(timeout=30)

    hits = {h["doc_id"]: h for h in
            manager.search(["alpha"], "term", 100)["hits"]}
    assert "d30" in hits
    assert "d1" not in hits          # delete not resurrected
    assert "d31" not in hits         # created+deleted during rebuild
    d0 = manager.get_document("d0")
    assert d0["text"] == "alpha REPLACED content"
    # new index points at the right version (version 2)
    assert hits["d0"]["doc_version"] == 2
    assert "REPLACED" in manager.get_document(
        "d0", version=hits["d0"]["doc_version"])["text"]


def test_delete_during_backfill_does_not_resurrect(manager):
    for i in range(20):
        manager.upsert(f"d{i}", f"backfill text {i}")
    manager.start_rebuild(Rules(), pause_at="backfill")
    job = manager._job
    wait_phase(manager, "paused:backfill")

    # delete a doc whose version is in the backfill snapshot
    manager.delete("d5")
    manager.upsert("dnew", "backfill text brand new")

    job.resume()
    assert manager.wait_rebuild(timeout=30)
    hits = {h["doc_id"] for h in
            manager.search(["backfill"], "term", 100)["hits"]}
    assert "d5" not in hits
    assert "dnew" in hits


def test_old_gen_reclaimed_only_after_reads_drain(manager):
    manager.upsert("d1", "retireme word")
    manager.start_rebuild(Rules(stopwords=frozenset({"the"})),
                          pause_at="switch")
    wait_phase(manager, "paused:switch")
    old_gen = manager.current()[0]

    # hold a read lease on the old gen across the switch
    lease_acquired = threading.Event()
    release = threading.Event()

    def reader():
        with manager._leases.lease(old_gen):
            lease_acquired.set()
            release.wait(timeout=15)

    t = threading.Thread(target=reader)
    t.start()
    assert lease_acquired.wait(timeout=5)

    job = manager._job
    job.resume()
    # switch completes but reclaim blocks on the outstanding lease:
    # wait for the job to park in 'retiring'
    deadline = time.time() + 10
    while time.time() < deadline:
        st = manager.rebuild_status()
        if st and st["phase"] == "retiring":
            break
        time.sleep(0.05)
    assert st["phase"] == "retiring"
    assert manager.current()[0] != old_gen  # new gen serves queries already
    conn = db.connect(
        manager.path, readonly=True)
    n = conn.execute("SELECT COUNT(*) c FROM tokens WHERE gen=?",
                     (old_gen,)).fetchone()["c"]
    conn.close()
    assert n > 0  # reader still active -> old index not reclaimed

    release.set()
    t.join(timeout=5)
    assert manager.wait_rebuild(timeout=30)
    conn = db.connect(
        manager.path, readonly=True)
    n = conn.execute("SELECT COUNT(*) c FROM tokens WHERE gen=?",
                     (old_gen,)).fetchone()["c"]
    conn.close()
    assert n == 0


def test_hammer_writes_reads_while_rebuild_runs(manager):
    """Sustained concurrent writes/reads overlapping a full rebuild.

    Uses the Manager directly (not TestClient): the Starlette in-process
    test portal has a tiny blocking-thread pool that self-starves when all
    request threads park on the write lock, which is a test-harness
    artifact, not application behavior.
    """
    import random
    from app.tokenizer import Rules
    manager.upsert("h0", "hammer alpha start")
    errors = []
    stop = threading.Event()

    def writer(wid):
        i = 0
        while not stop.is_set():
            i += 1
            doc = f"h{random.randrange(8)}"
            try:
                if i % 13 == 0:
                    manager.delete(doc)
                else:
                    manager.upsert(doc, f"hammer alpha w{wid} rev{i}")
            except Exception as exc:
                errors.append(f"writer: {exc!r}")

    def reader():
        while not stop.is_set():
            try:
                body = manager.search(["alpha"], "term", 100)
                for hit in body["hits"]:
                    d = manager.get_document(hit["doc_id"],
                                             version=hit["doc_version"])
                    if d is None:
                        continue  # deleted between search and fetch
                    text = d["text"]
                    for s, e in hit["spans"]:
                        if text[s:e].lower() != "alpha":
                            errors.append(f"bad span in {hit}")
            except Exception as exc:
                errors.append(f"reader: {exc!r}")
            time.sleep(0.001)

    manager.start_rebuild(Rules(case_fold=True, stopwords=frozenset({"hammer"})))
    threads = [threading.Thread(target=writer, args=(i,)) for i in range(6)]
    threads += [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()
    assert manager.wait_rebuild(timeout=60)
    time.sleep(2)
    stop.set()
    for t in threads:
        t.join(timeout=10)
    assert not errors, errors[:5]
    # post-switch: stopword 'hammer' rejected at API level; search layer
    # reflects the new rules
    gen, _wm, rules = manager.current()
    assert rules.stopwords == frozenset({"hammer"})
    assert manager.search(["alpha"], "term", 100)["count"] >= 1


def test_query_result_matches_its_doc_version(manager):
    manager.upsert("d1", "versionone alpha")
    manager.start_rebuild(Rules(), pause_at="catchup")
    wait_phase(manager, "paused:catchup")
    # rapid updates during catch-up
    for v in range(2, 8):
        manager.upsert("d1", f"version{v} alpha")
    job = manager._job
    job.resume()
    assert manager.wait_rebuild(timeout=30)
    body = manager.search(["alpha"], "term", 100)
    hit = body["hits"][0]
    doc = manager.get_document("d1", version=hit["doc_version"])
    s, e = hit["spans"][0]
    assert doc["text"][s:e].lower() == "alpha"
    assert doc["text"].startswith(f"version{hit['doc_version']}")


def test_version_consistency_under_concurrent_writes(client):
    """A query's spans must always slice the hit's doc version."""
    stop = threading.Event()
    errors = []

    def writer():
        i = 0
        while not stop.is_set():
            i += 1
            client.put("/documents/w1", json={"text": f"rev{i} alpha word"})
            if i % 7 == 0:
                client.delete("/documents/w1")

    def checker():
        while not stop.is_set():
            body = client.get("/search", params={"q": "alpha"}).json()
            for hit in body.get("hits", []):
                d = client.get(f"/documents/{hit['doc_id']}",
                               params={"version": hit["doc_version"]})
                if d.status_code == 404:
                    errors.append(f"dangling version {hit}")
                    continue
                text = d.json()["text"]
                for s, e in hit.get("spans", []):
                    if text[s:e].lower() != "alpha":
                        errors.append(f"span mismatch {text!r} {s}:{e}")

    client.put("/documents/w1", json={"text": "rev0 alpha word"})
    client.post("/rebuild", json={
        "rules": {"case_fold": True, "stopwords": ["word"]}})
    tw = threading.Thread(target=writer)
    tc = threading.Thread(target=checker)
    tw.start(); tc.start()
    time.sleep(3)
    client.manager.wait_rebuild(timeout=30)
    stop.set()
    tw.join(timeout=5); tc.join(timeout=5)
    assert not errors, errors[:5]
