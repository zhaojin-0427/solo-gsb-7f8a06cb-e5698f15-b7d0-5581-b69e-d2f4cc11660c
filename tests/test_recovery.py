"""Crash recovery: an interrupted rebuild must be resumable (or safely
cleaned up) and must never switch in a half-built candidate."""
from app.service import SearchService
from app.tokenizer import TokenizerConfig
from conftest import wait_for

NEW_CONFIG = {"case_fold": True, "stopwords": []}


def _status(svc):
    return svc.rebuild_status()


def test_restart_resumes_interrupted_rebuild(tmp_path):
    db = str(tmp_path / "crash.db")
    svc1 = SearchService(db)
    try:
        svc1.put_document("d1", "alpha beta")
        svc1.arm_pause_points(["catchup"])
        svc1.start_rebuild(TokenizerConfig.from_dict(NEW_CONFIG))
        wait_for(lambda: _status(svc1)["paused_at"] == "catchup", msg="not paused")
        # writes acknowledged while the first process is "crashed" mid-rebuild
        svc1.put_document("d2", "gamma delta")
        svc1.delete_document("d1")
        gen = _status(svc1)["gen"]
        # simulate a crash: no graceful shutdown, worker stays parked
    finally:
        svc1.close()  # stops the parked worker; persisted state is untouched

    # "process" restarts on the same database file
    svc2 = SearchService(db)
    try:
        st = _status(svc2)
        assert st["state"] != "IDLE" and st["gen"] == gen  # rebuild resumed
        assert svc2.tokenizer_info()["gen"] == 1           # no half switch
        wait_for(lambda: _status(svc2)["paused_at"] == "catchup",
                 msg="resumed worker did not reach the catchup pause point")
        svc2.resume()
        wait_for(lambda: _status(svc2)["state"] == "IDLE", msg="rebuild stuck")
        # every write acknowledged before the switch is visible, deletes stick
        assert svc2.tokenizer_info()["gen"] == gen
        assert [r["doc_id"] for r in svc2.search("gamma")["results"]] == ["d2"]
        assert svc2.search("alpha")["results"] == []
        # the interrupted candidate was completed exactly once
        gens = {i["gen"]: i["status"] for i in svc2.list_indexes()}
        assert gens[gen] == "active" and gens[1] in ("dropped", "retired")
    finally:
        svc2.close()


def test_restart_with_idle_state_truncates_log_and_starts_clean(tmp_path):
    db = str(tmp_path / "idle.db")
    svc1 = SearchService(db)
    svc1.put_document("d1", "hello world")
    svc1.close()

    svc2 = SearchService(db)
    try:
        st = svc2.rebuild_status()
        assert st["state"] == "IDLE"
        assert [r["doc_id"] for r in svc2.search("hello")["results"]] == ["d1"]
    finally:
        svc2.close()


def test_cancel_then_rebuild_starts_fresh(tmp_path):
    db = str(tmp_path / "cancel.db")
    svc = SearchService(db)
    try:
        svc.put_document("d1", "alpha")
        svc.arm_pause_points(["backfill"])
        first = svc.start_rebuild()
        wait_for(lambda: svc.rebuild_status()["paused_at"] == "backfill")
        assert svc.cancel_rebuild() is True
        assert svc.rebuild_status()["state"] == "IDLE"
        assert svc.tokenizer_info()["gen"] == 1  # never switched
        # a later rebuild must win cleanly and produce a complete index
        svc.arm_pause_points([])
        second = svc.start_rebuild()
        assert second["gen"] == first["gen"] + 1
        wait_for(lambda: svc.rebuild_status()["state"] == "IDLE")
        assert [r["doc_id"] for r in svc.search("alpha")["results"]] == ["d1"]
    finally:
        svc.close()
