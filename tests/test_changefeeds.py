"""Change feeds: GitHub and Kubernetes changes land in the change log once, with their real time."""

from datetime import datetime, timedelta, timezone

import httpx

from overwatch.changefeeds import GitHubFeed, KubernetesFeed, build_feeds
from overwatch.store import IncidentStore


def _ts(minutes_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def github_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/releases"):
        return httpx.Response(200, json=[
            {"id": 1, "tag_name": "v1.4.0", "name": "Faster checkout", "published_at": _ts(10), "author": {"login": "ana"}},
            {"id": 2, "tag_name": "v0.1", "name": "ancient", "published_at": _ts(60 * 24 * 30), "author": {"login": "bo"}},
            {"id": 3, "tag_name": "v1.5-rc", "draft": True, "published_at": _ts(5)}])
    if path.endswith("/deployments"):
        return httpx.Response(200, json=[{"id": 9, "sha": "abcdef123456", "ref": "main", "environment": "production",
                                          "created_at": _ts(3), "creator": {"login": "ci"}, "description": "hotfix"}])
    if "/actions/workflows/" in path:
        return httpx.Response(200, json={"workflow_runs": [{"id": 77, "head_sha": "1234567890", "display_title": "Merge #12",
                                                             "updated_at": _ts(2), "actor": {"login": "ana"}}]})
    return httpx.Response(404)


def test_github_feed_records_recent_changes_once(tmp_path):
    store = IncidentStore(tmp_path / "ow.db")
    feed = GitHubFeed({"repo": "acme/payments", "token": "t", "events": ["releases", "deployments", "workflow_runs"],
                       "workflow": "deploy.yml"},
                      lambda c: bool(store.record_change(c["service"], c["version"], c["kind"], c["author"],
                                                         c["description"], c["ts"], c["ref"])))
    feed.client = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(github_handler))
    assert feed.run_once() == 3  # the draft and the 30-day-old release are skipped
    assert feed.run_once() == 0  # nothing new: de-duplicated by ref
    rows = store._db.execute("SELECT service, version, kind, author FROM changes ORDER BY ts").fetchall()
    assert ("payments", "v1.4.0", "release", "ana") in rows and ("payments", "abcdef1", "deploy", "ci") in rows
    assert ("payments", "1234567", "deploy", "ana") in rows


def test_github_errors_are_clear():
    feed = GitHubFeed({"repo": "acme/x"}, lambda c: True)
    feed.client = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    try:
        feed.poll()
    except RuntimeError as e:
        assert "check the token" in str(e)
    else:
        raise AssertionError("expected a RuntimeError")


class FakeK8s:
    def __init__(self):
        self.rows = [{"deployment": "orders", "image": "reg/orders:v1", "generation": 1},
                     {"deployment": "gateway", "image": "reg/gateway:v7", "generation": 3}]
        self.cfg = {"type": "kubernetes"}

    def _rollouts(self):
        return self.rows


def test_kubernetes_feed_reports_image_and_spec_changes_after_the_baseline():
    k8s, seen = FakeK8s(), []
    feed = KubernetesFeed({"type": "kubernetes"}, lambda c: seen.append(c) or True, k8s)
    assert feed.run_once() == 0  # first pass is only the starting state
    k8s.rows[0].update(image="reg/orders:v2", generation=2)  # new image
    k8s.rows[1].update(generation=4)  # same image, spec changed
    assert feed.run_once() == 2
    deploy = next(c for c in seen if c["service"] == "orders")
    config = next(c for c in seen if c["service"] == "gateway")
    assert (deploy["kind"], deploy["version"], deploy["ref"]) == ("deploy", "v2", "k8s:orders:2")
    assert "v1 -> reg/orders:v2" in deploy["description"] or "reg/orders:v1 -> reg/orders:v2" in deploy["description"]
    assert config["kind"] == "config" and "same image" in config["description"]
    assert feed.run_once() == 0


def test_store_dedupes_by_ref_and_migrates_old_state_files(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE changes (ts TEXT NOT NULL, service TEXT NOT NULL, version TEXT, kind TEXT NOT NULL DEFAULT 'deploy', "
               "author TEXT, description TEXT)")
    db.execute("CREATE TABLE incidents (id TEXT PRIMARY KEY, opened_at TEXT NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL)")
    db.commit()
    db.close()
    store = IncidentStore(path)  # adds the ref column to an existing file
    assert store.record_change("a", ref="x:1") is not None and store.record_change("a", ref="x:1") is None
    assert store.record_change("a") is not None and store.record_change("a") is not None  # no ref: always stored


def test_build_feeds_validates():
    import pytest
    with pytest.raises(ValueError, match="unknown change feed"):
        build_feeds([{"type": "jenkins"}], lambda c: True, {})
    with pytest.raises(ValueError, match="needs a kubernetes source"):
        build_feeds([{"type": "kubernetes"}], lambda c: True, {})
    with pytest.raises(ValueError, match="needs 'repo'"):
        build_feeds([{"type": "github"}], lambda c: True, {})
