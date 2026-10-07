import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def db_path(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture()
def manager(db_path):
    from app.manager import Manager
    m = Manager(db_path)
    yield m
    # wait for any background rebuild so threads do not leak
    job = m._job
    if job is not None:
        m.wait_rebuild(timeout=30)


@pytest.fixture()
def client(db_path):
    from app.main import create_app
    app = create_app(db_path)
    with TestClient(app) as c:
        c.manager = app.state.manager
        yield c


def wait_phase(manager, substr, timeout=10.0):
    """Wait until the rebuild job phase contains ``substr``."""
    job = manager._job
    assert job is not None
    import time
    deadline = time.time() + timeout
    with job.cond:
        while substr not in job.phase and not job.finished:
            job.cond.wait(timeout=0.05)
            if time.time() > deadline:
                raise AssertionError(
                    f"phase never reached {substr!r}: {job.phase}")
    return job


def wait_finished(manager, timeout=30.0):
    assert manager.wait_rebuild(timeout=timeout)
