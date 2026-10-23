"""Built-in notifiers: console, Slack incoming webhook, Jira Cloud, generic webhook."""

from __future__ import annotations

from typing import Any

import httpx

from ..models import Incident
from .base import Notifier, render_text


class NotifyError(Exception):
    pass


class ConsoleNotifier(Notifier):
    def send(self, incident: Incident) -> str:
        print("\n" + "=" * 72 + "\n" + render_text(incident) + "\n" + "=" * 72 + "\n")
        return "printed"


class SlackNotifier(Notifier):
    """Slack incoming webhook (https://api.slack.com/messaging/webhooks). Config: webhook_url."""

    def send(self, incident: Incident) -> str:
        url = self.cfg.get("webhook_url")
        if not url:
            raise NotifyError("slack notifier needs webhook_url")
        r = httpx.post(url, json={"text": render_text(incident)}, timeout=10)
        if r.status_code >= 300:
            raise NotifyError(f"slack returned HTTP {r.status_code}: {r.text[:200]}")
        return "posted to Slack"


_JIRA_PRIORITY = {"critical": "Highest", "high": "High", "medium": "Medium", "low": "Low"}


class JiraNotifier(Notifier):
    """Jira Cloud REST v2 create-issue. Config: base_url, email, api_token, project_key, issue_type."""

    def send(self, incident: Incident) -> str:
        for key in ("base_url", "email", "api_token", "project_key"):
            if not self.cfg.get(key):
                raise NotifyError(f"jira notifier needs {key}")
        fields: dict[str, Any] = {
            "project": {"key": self.cfg["project_key"]},
            "summary": incident.title[:250],
            "description": render_text(incident, max_evidence=10).replace("*", ""),
            "issuetype": {"name": self.cfg.get("issue_type", "Bug")},
            "labels": ["overwatch", *[s.replace(" ", "-") for s in incident.services]],
        }
        if self.cfg.get("set_priority"):
            fields["priority"] = {"name": _JIRA_PRIORITY.get(incident.severity, "Medium")}
        r = httpx.post(
            self.cfg["base_url"].rstrip("/") + "/rest/api/2/issue",
            json={"fields": fields},
            auth=(self.cfg["email"], self.cfg["api_token"]),
            timeout=15,
        )
        if r.status_code >= 300:
            raise NotifyError(f"jira returned HTTP {r.status_code}: {r.text[:300]}")
        key = r.json().get("key", "?")
        return f"{self.cfg['base_url'].rstrip('/')}/browse/{key}"


class WebhookNotifier(Notifier):
    """POST the full incident as JSON to any URL (PagerDuty bridge, Teams relay, your own API...)."""

    def send(self, incident: Incident) -> str:
        url = self.cfg.get("url")
        if not url:
            raise NotifyError("webhook notifier needs url")
        r = httpx.post(url, json=incident.to_dict(), headers=self.cfg.get("headers", {}), timeout=10)
        if r.status_code >= 300:
            raise NotifyError(f"webhook returned HTTP {r.status_code}")
        return f"posted to {url}"


NOTIFIER_TYPES: dict[str, type[Notifier]] = {
    "console": ConsoleNotifier,
    "slack": SlackNotifier,
    "jira": JiraNotifier,
    "webhook": WebhookNotifier,
}
