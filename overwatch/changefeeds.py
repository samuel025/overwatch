"""Change feeds: learn about deploys and config changes without anyone reporting them.

A change is the best clue to most incidents ("what changed just before this?"). Instead of asking users to
push every release to /api/changes, Overwatch can poll where changes already happen:

  - type: github          releases, GitHub deployments and (optionally) successful deploy workflow runs
  - type: kubernetes      a Deployment whose image or spec changed (detected by comparing rollouts)

Each feed writes into the same change log the Investigator already reads ("reported_changes"), once per change
(de-duplicated by a stable `ref`), with the time the change really happened.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx

Record = Callable[[dict[str, Any]], bool]  # change row (with "ref") -> True if it was new


def _iso(value: str | None) -> str:
    """GitHub timestamps ('2026-10-23T10:00:00Z') in the log's own format (UTC, +00:00)."""
    when = datetime.fromisoformat(value.replace("Z", "+00:00")) if value else datetime.now(timezone.utc)
    return when.astimezone(timezone.utc).isoformat(timespec="seconds")


class ChangeFeed:
    """One polled source of changes. Subclasses implement poll(); this class runs it on a timer."""

    def __init__(self, cfg: dict[str, Any], record: Record, on_error: Callable[[str], None] = print):
        self.cfg = cfg
        self.record = record
        self.on_error = on_error
        self.interval = max(10, int(cfg.get("interval_seconds", 60)))
        self.lookback = timedelta(minutes=int(cfg.get("lookback_minutes", 120)))  # ignore changes older than this
        self._stop = threading.Event()

    def poll(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def run_once(self) -> int:
        """One pass; returns how many new changes were recorded."""
        return sum(1 for change in self.poll() if self.record(change))

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name=f"changes-{self.cfg.get('type')}").start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as e:  # a feed being down must never affect watching
                self.on_error(f"change feed {self.cfg.get('type')}: {e}")
            self._stop.wait(self.interval)


class GitHubFeed(ChangeFeed):
    """Config: repo (owner/name), token, service (default: repo name), events (releases, deployments,
    workflow_runs), environment (deployments filter), workflow (file name, for workflow_runs), branch."""

    def __init__(self, cfg: dict[str, Any], record: Record, on_error: Callable[[str], None] = print):
        super().__init__(cfg, record, on_error)
        if not cfg.get("repo"):
            raise ValueError("a github change feed needs 'repo' (owner/name)")
        self.repo = cfg["repo"]
        self.service = cfg.get("service") or self.repo.split("/")[-1]
        self.events = cfg.get("events") or ["releases", "deployments"]
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if cfg.get("token"):
            headers["Authorization"] = f"Bearer {cfg['token']}"
        self.client = httpx.Client(base_url=cfg.get("api_url", "https://api.github.com").rstrip("/"),
                                   headers=headers, timeout=15)

    def _get(self, path: str, **params: Any) -> Any:
        r = self.client.get(path, params=params)
        if r.status_code in (401, 403, 404):
            raise RuntimeError(f"GitHub {r.status_code} for {self.repo}{path.split(self.repo)[-1]}: check the token's access")
        r.raise_for_status()
        return r.json()

    def poll(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        base = f"/repos/{self.repo}"
        if "releases" in self.events:
            for r in self._get(f"{base}/releases", per_page=10):
                if r.get("draft"):
                    continue
                out.append({"service": self.service, "version": r.get("tag_name", ""), "kind": "release",
                            "author": (r.get("author") or {}).get("login", ""),
                            "description": (r.get("name") or r.get("tag_name") or "release")[:200],
                            "ts": _iso(r.get("published_at") or r.get("created_at")), "ref": f"github:{self.repo}:release:{r['id']}"})
        if "deployments" in self.events:
            params = {"per_page": 20, **({"environment": self.cfg["environment"]} if self.cfg.get("environment") else {})}
            for d in self._get(f"{base}/deployments", **params):
                out.append({"service": self.service, "version": (d.get("sha") or "")[:7], "kind": "deploy",
                            "author": (d.get("creator") or {}).get("login", ""),
                            "description": f"deployed {d.get('ref', '')} to {d.get('environment', '')}"
                                           + (f": {d['description']}" if d.get("description") else ""),
                            "ts": _iso(d.get("created_at")), "ref": f"github:{self.repo}:deployment:{d['id']}"})
        if "workflow_runs" in self.events and self.cfg.get("workflow"):
            params = {"status": "success", "per_page": 10, **({"branch": self.cfg["branch"]} if self.cfg.get("branch") else {})}
            for run in self._get(f"{base}/actions/workflows/{self.cfg['workflow']}/runs", **params).get("workflow_runs", []):
                out.append({"service": self.service, "version": (run.get("head_sha") or "")[:7], "kind": "deploy",
                            "author": (run.get("actor") or {}).get("login", ""),
                            "description": f"{self.cfg['workflow']} succeeded: {run.get('display_title', '')}"[:200],
                            "ts": _iso(run.get("updated_at")), "ref": f"github:{self.repo}:run:{run['id']}"})
        cutoff = datetime.now(timezone.utc) - self.lookback  # old history is no clue to a current incident
        out = [c for c in out if datetime.fromisoformat(c["ts"]) >= cutoff]
        return sorted(out, key=lambda c: c["ts"])

    def stop(self) -> None:
        super().stop()
        self.client.close()


class KubernetesFeed(ChangeFeed):
    """Config: source (the kubernetes source's name). A Deployment whose image changes is a deploy; one whose
    spec changes with the same image (env, replicas, resources) is a config change. The first pass only
    records the starting state."""

    def __init__(self, cfg: dict[str, Any], record: Record, source: Any, on_error: Callable[[str], None] = print):
        super().__init__(cfg, record, on_error)
        self.source = source
        self._known: dict[str, tuple[str, int]] = {}

    def poll(self) -> list[dict[str, Any]]:
        out = []
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for d in self.source._rollouts():
            name, image, gen = d["deployment"], d.get("image") or "", int(d.get("generation") or 0)
            before = self._known.get(name)
            self._known[name] = (image, gen)
            if before is None or before == (image, gen):
                continue
            old_image, old_gen = before
            tag = image.rsplit(":", 1)[-1] if ":" in image.rsplit("/", 1)[-1] else image
            if image != old_image:
                out.append({"service": name, "version": tag, "kind": "deploy", "author": "",
                            "description": f"image changed {old_image} -> {image}", "ts": now,
                            "ref": f"k8s:{name}:{gen}"})
            else:
                out.append({"service": name, "version": tag, "kind": "config", "author": "",
                            "description": f"deployment spec changed (generation {old_gen} -> {gen}); same image",
                            "ts": now, "ref": f"k8s:{name}:{gen}"})
        return out


def build_feeds(cfgs: list[dict[str, Any]], record: Record, sources: dict[str, Any],
                on_error: Callable[[str], None] = print) -> list[ChangeFeed]:
    feeds: list[ChangeFeed] = []
    for cfg in cfgs:
        kind = cfg.get("type")
        if cfg.get("enabled", True) is False:
            continue
        if kind == "github":
            feeds.append(GitHubFeed(cfg, record, on_error))
        elif kind == "kubernetes":
            name = cfg.get("source") or next((n for n, s in sources.items() if s.cfg.get("type") == "kubernetes"), None)
            if name not in sources:
                raise ValueError("a kubernetes change feed needs a kubernetes source")
            feeds.append(KubernetesFeed(cfg, record, sources[name], on_error))
        else:
            raise ValueError(f"unknown change feed type '{kind}' (known: github, kubernetes)")
    return feeds
