"""What the model is told when the team has not described a service: never the TODO placeholder."""

from overwatch import knowledge
from overwatch.agents.investigator import Investigator
from overwatch.config import CheckConfig
from overwatch.models import Anomaly, Incident
from tests.test_core import FakeSource, ScriptedLLM, make_config

TOPOLOGY = {"api": ["drmq", "postgres", "redis"], "prometheus": ["api"], "postgres": [], "drmq": [], "redis": []}
SERVICES = {
    "api": {"what": "TODO: describe what this service does", "runs_on": "1 Docker container", "image": "shop-api"},
    "redis": {"what": "Read-through cache for short links; the api falls back to Postgres", "normal": "hit rate above 80%"},
    "postgres": {"what": "PostgreSQL database server"},
    "drmq": {"what": "TODO: describe what this service does"},
}
CHECKS = [CheckConfig("api_p95", "prom", "x", service="api", description="api p95 latency (ms)"),
          CheckConfig("api_up", "prom", "x", service="api", description="api answers its health check")]


def test_placeholders_are_recognised():
    assert knowledge.is_placeholder("TODO: describe what this service does")
    assert knowledge.is_placeholder("") and knowledge.is_placeholder(None) and knowledge.is_placeholder("  ")
    assert not knowledge.is_placeholder("Go REST API that redirects short codes")


def test_an_unfilled_system_description_becomes_an_honest_statement():
    text = knowledge.system_description("6 services: api, drmq. TODO: one or two sentences on what the system does and how requests flow.")
    assert text.startswith("6 services: api, drmq.") and "TODO" not in text
    assert "has not said what the system does" in text
    assert knowledge.system_description("") .startswith("(The team has not said")


def test_an_undescribed_service_is_stated_as_such_with_facts_overwatch_found():
    lines = knowledge.describe_services(SERVICES, TOPOLOGY, CHECKS)
    api = next(l for l in lines if l.startswith("- api:"))
    assert "TODO" not in api and "has not described this service; do not assume what it does" in api
    assert "calls drmq, postgres, redis" in api and "called by prometheus" in api and "1 Docker container" in api
    assert "image shop-api" in api and "api p95 latency (ms)" in api  # what is watched, computed from the checks
    drmq = next(l for l in lines if l.startswith("- drmq:"))
    assert "called by api" in drmq  # even a service with nothing else gets its place in the system


def test_a_service_the_team_described_keeps_its_own_words():
    lines = knowledge.describe_services(SERVICES, TOPOLOGY, CHECKS)
    redis = next(l for l in lines if l.startswith("- redis:"))
    assert redis == ("- redis: what: Read-through cache for short links; the api falls back to Postgres; "
                     "normal: hit rate above 80%")
    assert "Overwatch found" not in redis  # no need: the team already said


def test_the_investigator_never_sees_the_todo_text():
    cfg = make_config()
    cfg.services, cfg.topology, cfg.checks = SERVICES, TOPOLOGY, CHECKS
    cfg.system_description = "4 services. TODO: one or two sentences on what the system does and how requests flow."
    inv = Investigator(cfg, ScriptedLLM([]), {"db": FakeSource({})})
    incident = Incident(anomalies=[Anomaly("api_p95", "api", 900.0, 200.0, ">", "p95 high")])
    context = "\n\n".join(inv._context(incident, []))
    assert "TODO" not in context
    assert "has not described this service" in context and "has not said what the system does" in context
    assert "TODO" not in inv.known_text()
    assert "calls drmq" in inv.known_text()  # the facts are also what grounding treats as known
