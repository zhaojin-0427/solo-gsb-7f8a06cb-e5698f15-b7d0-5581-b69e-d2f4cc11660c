import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.service import SearchService


def wait_for(cond, timeout=10.0, interval=0.05, msg="condition not met"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(interval)
    raise AssertionError(msg)


@pytest.fixture
def svc(tmp_path):
    s = SearchService(str(tmp_path / "test.db"))
    yield s
    s.close()


@pytest.fixture
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def search(client, q, **kw):
    r = client.get("/search", params={"q": q, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def doc_ids(payload):
    return [r["doc_id"] for r in payload["results"]]


def wait_idle(client, timeout=10.0):
    wait_for(lambda: client.get("/index/rebuild").json()["state"] == "IDLE",
             timeout=timeout, msg="rebuild did not finish")


def wait_paused(client, point, timeout=10.0):
    wait_for(lambda: client.get("/index/rebuild").json()["paused_at"] == point,
             timeout=timeout, msg=f"worker did not pause at {point}")
