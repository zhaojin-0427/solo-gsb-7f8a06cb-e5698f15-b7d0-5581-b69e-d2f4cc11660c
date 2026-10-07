"""Drill boundaries: backfill / catch-up / switch pauses + crash recovery."""
import os
import signal
import subprocess
import sys
import threading
import time

import httpx
import pytest

from app.manager import Manager
from app.tokenizer import Rules
from tests.conftest import wait_phase


@pytest.mark.parametrize("boundary", ["backfill", "catchup", "switch"])
def test_pause_boundaries_writes_visible_throughout(manager, boundary):
    for i in range(10):
        manager.upsert(f"d{i}", f"seed word {i}")
    manager.start_rebuild(Rules(stopwords=frozenset({"seed"})),
                          pause_at=boundary)
    wait_phase(manager, f"paused:{boundary}")

    # current index stays complete + writable at the boundary
    assert manager.search(["seed"], "term", 100)["count"] == 10
    manager.upsert("added", "seed word brandnew")
    assert manager.search(["brandnew"], "term", 100)["count"] == 1
    manager.delete("d0")
    assert manager.search(["seed"], "term", 100)["count"] == 10
    assert all(h["doc_id"] != "d0"
               for h in manager.search(["seed"], "term", 100)["hits"])

    # status introspection
    st = manager.rebuild_status()
    assert st["paused"] == boundary

    manager.resume_rebuild()
    assert manager.wait_rebuild(timeout=30)
    assert st["paused"] is None or manager.rebuild_status()["phase"] == "done"

    # after switch: stopword rule active, all boundary writes represented
    hits = {h["doc_id"] for h in
            manager.search(["brandnew"], "term", 100)["hits"]}
    assert hits == {"added"}
    assert manager.search(["seed"], "term", 100)["count"] == 0
    d0 = manager.get_document("d0")
    assert d0 is None


def test_cancel_at_each_boundary(manager):
    for boundary in ["backfill", "catchup", "switch"]:
        old_gen = manager.current()[0]
        manager.upsert(f"x-{boundary}", f"canceltext {boundary}")
        manager.start_rebuild(Rules(stopwords=frozenset({"canceltext"})),
                              pause_at=boundary)
        wait_phase(manager, f"paused:{boundary}")
        manager.cancel_rebuild()
        assert manager.wait_rebuild(timeout=30)
        assert manager.current()[0] == old_gen
        assert manager.search(["canceltext"], "term", 100)["count"] >= 1
        # candidate generation rows gone
        from app import db
        conn = db.connect(manager.path, readonly=True)
        building = [r["gen"] for r in
                    conn.execute("SELECT gen FROM gen_meta")]
        conn.close()
        # only the current gen remains
        assert building == [old_gen]


def test_cancel_resume_race_at_switch_never_corrupts_active(manager):
    """Cancel arriving right as the switch barrier releases must never
    delete the generation that becomes current."""
    from app import db
    for round_no in range(20):
        manager.upsert(f"r{round_no}", f"race word number {round_no}")
        manager.start_rebuild(Rules(stopwords=frozenset({"race"})),
                              pause_at="switch")
        wait_phase(manager, "paused:switch")
        t1 = threading.Thread(target=manager.resume_rebuild)
        t2 = threading.Thread(target=manager.cancel_rebuild)
        t1.start(); t2.start()
        t1.join(); t2.join()
        assert manager.wait_rebuild(timeout=30)
        # the active generation must always be a complete, usable index
        body = manager.search(["word"], "term", 100)
        assert len(body["hits"]) == round_no + 1, (round_no, body["count"])
        conn = db.connect(manager.path, readonly=True)
        cur = [dict(g) for g in conn.execute(
            "SELECT gen, state FROM gen_meta WHERE state='current'")]
        conn.close()
        assert len(cur) == 1, (round_no, cur)


def test_restart_discards_paused_building_generation(db_path):
    """Simulate a crash while a rebuild is paused: reopen the same DB."""
    m = Manager(db_path)
    for i in range(5):
        m.upsert(f"d{i}", f"restart word {i}")
    old_gen = m.current()[0]
    m.start_rebuild(Rules(stopwords=frozenset({"restart"})),
                    pause_at="catchup")
    wait_phase(m, "paused:catchup")
    # "process dies": drop the manager without resuming
    del m

    m2 = Manager(db_path)  # restart: safe cleanup
    assert m2.current()[0] == old_gen
    state = m2.meta_state()
    assert [g["gen"] for g in state["generations"]] == [old_gen]
    # current index intact and searchable
    assert m2.search(["word"], "term", 100)["count"] == 5
    assert m2.search(["restart"], "term", 100)["count"] == 5
    # a fresh rebuild can run and complete
    m2.start_rebuild(Rules(stopwords=frozenset({"restart"})))
    assert m2.wait_rebuild(timeout=30)
    assert m2.search(["restart"], "term", 100)["count"] == 0
    assert m2.search(["word"], "term", 100)["count"] == 5
    assert m2.current()[0] != old_gen


def test_restart_after_switch_does_not_resurrect_retired(db_path):
    m = Manager(db_path)
    m.upsert("a", "switch keepme")
    m.upsert("b", "switch deleteme")
    m.delete("b")
    m.start_rebuild(Rules())
    assert m.wait_rebuild(timeout=30)
    new_gen = m.current()[0]
    del m

    m2 = Manager(db_path)
    assert m2.current()[0] == new_gen
    assert {h["doc_id"] for h in
            m2.search(["switch"], "term", 100)["hits"]} == {"a"}
    assert m2.get_document("b") is None


def _serve(db_path, port):
    env = dict(os.environ)
    env["FTS_DB_PATH"] = db_path
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level",
         "warning"],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True)


def _wait_http(base, timeout=15.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            r = httpx.get(base + "/healthz", timeout=1.0)
            if r.status_code == 200:
                return
        except Exception as e:
            last = e
        time.sleep(0.2)
    raise RuntimeError(f"server did not come up: {last}")


def test_real_process_kill_mid_rebuild(tmp_path):
    """End-to-end: kill the server during a paused rebuild, restart it."""
    db_path = str(tmp_path / "real.db")
    port = 8765
    base = f"http://127.0.0.1:{port}"
    proc = _serve(db_path, port)
    try:
        _wait_http(base)
        for i in range(8):
            httpx.put(f"{base}/documents/doc{i}",
                      json={"text": f"live word number {i}"}, timeout=5)
        r = httpx.post(f"{base}/rebuild",
                       json={"rules": {"stopwords": ["live"]},
                             "pause_at": "catchup"}, timeout=5)
        assert r.status_code == 200
        # wait for pause
        deadline = time.time() + 10
        while time.time() < deadline:
            st = httpx.get(f"{base}/rebuild", timeout=5).json()
            if st.get("paused") == "catchup":
                break
            time.sleep(0.1)
        assert st["paused"] == "catchup"
        # writes still work during the pause
        httpx.put(f"{base}/documents/extra",
                  json={"text": "live word duringpause"}, timeout=5)
        assert httpx.get(f"{base}/search",
                         params={"q": "duringpause"}, timeout=5).json()[
            "count"] == 1

        # KILL the process mid-rebuild
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGKILL)
            proc.wait(timeout=10)

    # restart on the same data directory
    proc = _serve(db_path, port)
    try:
        _wait_http(base)
        meta = httpx.get(f"{base}/meta", timeout=5).json()
        # half-built gen discarded; single current gen remains
        assert len(meta["generations"]) == 1
        assert meta["generations"][0]["state"] == "current"
        assert meta["active"]["rules"]["stopwords"] == []
        # all committed writes (incl. the one while paused!) are searchable
        body = httpx.get(f"{base}/search", params={"q": "live"},
                         timeout=5).json()
        assert body["count"] == 9
        body = httpx.get(f"{base}/search", params={"q": "duringpause"},
                         timeout=5).json()
        assert body["count"] == 1

        # run a successful rebuild after restart to switch rules
        httpx.post(f"{base}/rebuild",
                  json={"rules": {"stopwords": ["live"]}}, timeout=5)
        deadline = time.time() + 20
        while time.time() < deadline:
            st = httpx.get(f"{base}/rebuild", timeout=5).json()
            if not st.get("running", False):
                break
            time.sleep(0.2)
        assert st["phase"] == "done"
        # 'live' is now a stopword: query is rejected, no stale matches
        r = httpx.get(f"{base}/search", params={"q": "live"}, timeout=5)
        assert r.status_code == 400
        assert httpx.get(f"{base}/search", params={"q": "word"},
                         timeout=5).json()["count"] == 9
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.send_signal(signal.SIGKILL)
            proc.wait(timeout=10)
