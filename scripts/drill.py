#!/usr/bin/env python3
"""Rebuild drill driver.

Pauses a background rebuild at one of the three boundaries
(backfill / catchup / switch), exercises concurrent writes, deletes and
searches at that moment, optionally performs a *process restart* while
the rebuild is paused, then resumes and verifies the atomic switch.

Usage:
    python scripts/drill.py --base-url http://localhost:8000 setup
    python scripts/drill.py pause --at catchup --stopwords the,a --restart-cmd "docker compose restart fts"
    python scripts/drill.py verify

"pause" both seeds the corpus and starts the paused rebuild; after a
process restart the paused (half-built) generation is discarded on
startup, so "verify" checks that no stale rules leaked, starts a fresh
rebuild and waits for it to complete.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from typing import Any, Dict

import httpx

CORPUS = [
    ("doc-001", "the quick brown fox jumps over the lazy dog"),
    ("doc-002", "a second document about foxes and quick decisions"),
    ("doc-003", "Unicode 支持：日本語のテストと café au lait 12345"),
    ("doc-004", "the theory of everything: quick fox anthology"),
]


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def wait_health(client: httpx.Client, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if client.get("/healthz").status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.3)
    die("service is not healthy")


def show(title: str, obj: Any) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def cmd_setup(args, client: httpx.Client) -> None:
    # make setup idempotent across repeated runs on the same database
    if client.get("/rebuild").json().get("running"):
        client.post("/rebuild/cancel")
        _wait_done(client)
    for doc_id, text in CORPUS:
        r = client.put(f"/documents/{doc_id}", json={"text": text})
        r.raise_for_status()
    print(f"seeded {len(CORPUS)} documents")
    show("current state", client.get("/meta").json())


def cmd_pause(args, client: httpx.Client) -> None:
    new_stopwords = args.stopwords.split(",") if args.stopwords else []
    rules_before = client.get("/meta").json()["active"]["rules"]
    r = client.post("/rebuild", json={
        "rules": {"case_fold": True, "stopwords": new_stopwords},
        "pause_at": args.at})
    if r.status_code == 409:
        die("a rebuild is already running:\n"
            + json.dumps(r.json(), ensure_ascii=False, indent=2))
    r.raise_for_status()

    # wait until the job parks at the requested boundary
    deadline = time.time() + 30
    st = {}
    while time.time() < deadline:
        st = client.get("/rebuild").json()
        if st.get("paused") == args.at:
            break
        time.sleep(0.1)
    if st.get("paused") != args.at:
        die(f"rebuild did not pause at {args.at}: {st}")
    show(f"rebuild PAUSED at boundary: {args.at}", st)

    # concurrent writes while paused: insert, update, delete
    r = client.put("/documents/drill-new",
                   json={"text": "the drill zzyzx inserted this document"})
    r.raise_for_status()
    print("upsert drill-new ->", r.json())
    r = client.put("/documents/doc-002",
                   json={"text": "a second document rewritten during drill"})
    r.raise_for_status()
    print("update doc-002 ->", r.json())
    r = client.delete("/documents/doc-004")
    r.raise_for_status()
    print("delete doc-004 ->", r.json())

    # searches against the complete CURRENT index must keep working
    hits = client.get("/search", params={"q": "zzyzx"}).json()
    assert hits["count"] == 1 and hits["hits"][0]["doc_id"] == "drill-new", hits
    # a word that is not being stopworded must still match
    assert client.get("/search", params={"q": "quick"}).status_code == 200
    assert client.get("/search", params={"q": ""}).status_code == 400
    print("searches while paused: OK (current index fully served)")

    if args.restart_cmd:
        print(f"\nrestarting process: {args.restart_cmd}")
        subprocess.run(args.restart_cmd, shell=True, check=True)
        wait_health(client)
        meta = client.get("/meta").json()
        show("after restart: half-built gen must be gone", meta)
        gens = meta["generations"]
        assert len(gens) == 1 and gens[0]["state"] == "current", gens
        rules_after = meta["active"]["rules"]
        # the candidate rules must NOT have taken effect without a switch;
        # the rules in force are exactly the pre-rebuild ones
        assert rules_after == rules_before, (rules_before, rules_after)
        # the paused write set survives and is searchable on the old index
        assert client.get("/search",
                          params={"q": "zzyzx"}).json()["hits"][0][
            "doc_id"] == "drill-new"
        assert client.get("/documents/doc-004").status_code == 404
        print("restart checks: OK")
        print("\nnow run:  python scripts/drill.py verify --restarted")
        return

    print(f"\npaused at {args.at}. inspect with GET /rebuild, then run:")
    print("  python scripts/drill.py resume")
    print("  python scripts/drill.py verify")


def cmd_resume(args, client: httpx.Client) -> None:
    r = client.post("/rebuild/resume")
    if r.status_code == 404:
        print("no paused rebuild (already discarded by restart?)")
        return
    r.raise_for_status()
    print("resumed ->", r.json().get("phase"))


def _wait_done(client: httpx.Client, timeout: float = 60.0) -> Dict[str, Any]:
    deadline = time.time() + timeout
    st: Dict[str, Any] = {}
    while time.time() < deadline:
        st = client.get("/rebuild").json()
        if not st.get("running", False):
            return st
        time.sleep(0.2)
    die(f"rebuild did not finish: {st}")


def cmd_verify(args, client: httpx.Client) -> None:
    st = client.get("/rebuild").json()
    if st.get("running") and not args.restarted:
        die("rebuild still running; resume it first or wait: "
            + json.dumps(st, ensure_ascii=False))

    if args.restarted or not st:
        # the paused candidate was discarded on restart; run a clean rebuild
        print("\nstarting a fresh rebuild after restart cleanup ...")
        r = client.post("/rebuild", json={
            "rules": {"case_fold": True, "stopwords": ["the", "a"]}})
        r.raise_for_status()
        st = _wait_done(client)

    show("finished rebuild", st)
    assert st.get("phase") == "done", st

    meta = client.get("/meta").json()
    show("final state", meta)
    assert len(meta["generations"]) == 1, "old gen must be reclaimed"

    # stopword rules are live: 'the' query is rejected
    r = client.get("/search", params={"q": "the"})
    assert r.status_code == 400, r.text
    print("stopword query rejected: OK")

    # writes made while paused are represented on the new index
    zzyzx = client.get("/search", params={"q": "zzyzx"}).json()
    assert zzyzx["hits"][0]["doc_id"] == "drill-new", zzyzx
    print("write during rebuild is searchable on the new gen: OK")

    # update landed: old content gone, new content present
    assert client.get("/search",
                      params={"q": "anthology"}).json()["count"] == 0
    rewritten = client.get("/search", params={"q": "rewritten"}).json()
    assert rewritten["hits"][0]["doc_id"] == "doc-002"
    print("update during rebuild indexed: OK")

    # delete never resurrected
    assert client.get("/documents/doc-004").status_code == 404
    assert client.get("/search",
                      params={"q": "anthology"}).json()["count"] == 0
    print("delete during rebuild stays deleted: OK")

    # spans are code-point ranges slicing the right document version
    hit = zzyzx["hits"][0]
    doc = client.get(f"/documents/{hit['doc_id']}",
                     params={"version": hit["doc_version"]}).json()
    for s, e in hit["spans"]:
        assert doc["text"][s:e] == "zzyzx", (s, e, doc)
    print("hit spans match the hit's document version: OK")

    # phrase adjacency incl. Unicode and stopword gap
    ph = client.get("/search", params={"q": '"quick brown"'}).json()
    assert {h["doc_id"] for h in ph["hits"]} == {"doc-001"}, ph
    assert client.get("/search",
                      params={"q": '"fox quick"'}).json()["count"] == 0
    uni = client.get("/search", params={"q": "café"}).json()
    assert uni["count"] == 1
    print("phrase + unicode: OK")
    print("\nALL DRILL CHECKS PASSED")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", default="http://localhost:8000")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup")
    p = sub.add_parser("pause")
    p.add_argument("--at", choices=["backfill", "catchup", "switch"],
                   default="catchup")
    p.add_argument("--stopwords", default="the,a")
    p.add_argument("--restart-cmd", default=None,
                   help="shell command to restart the service, e.g. "
                        '"docker compose restart fts"')
    sub.add_parser("resume")
    pv = sub.add_parser("verify")
    pv.add_argument("--restarted", action="store_true",
                    help="set after a restart-at-pause drill")
    args = ap.parse_args()

    with httpx.Client(base_url=args.base_url, timeout=10.0) as client:
        wait_health(client)
        {"setup": cmd_setup, "pause": cmd_pause,
         "resume": cmd_resume, "verify": cmd_verify}[args.cmd](args, client)


if __name__ == "__main__":
    main()
