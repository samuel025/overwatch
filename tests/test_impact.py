from collections import deque

from overwatch.impact import incident_start, spread
from overwatch.models import Anomaly

TOPOLOGY = {"gateway": ["orders", "payments"], "orders": ["postgres"], "payments": ["postgres", "psp.example.com"]}


def test_spread_marks_callers_of_the_origin_at_risk():
    br = spread(TOPOLOGY, "postgres", failing={"orders"})
    assert br["affected"] == ["orders", "postgres"]
    assert br["at_risk"] == ["gateway", "payments"]  # they call postgres too, not failing yet
    assert {"from": "orders", "to": "postgres"} in br["edges"]
    assert {"from": "gateway", "to": "orders"} in br["edges"]


def test_spread_of_external_dependency():
    br = spread(TOPOLOGY, "psp.example.com", failing={"payments", "gateway"})
    assert br["at_risk"] == []
    assert {"from": "payments", "to": "psp.example.com"} in br["edges"]
    assert {"from": "orders", "to": "postgres"} not in br["edges"]  # orders is not on the failure path


def test_spread_without_origin_is_just_the_failing_services():
    assert spread(TOPOLOGY, None, failing={"gateway"})["affected"] == ["gateway"]


def test_incident_start_walks_back_the_unbroken_bad_run():
    history = {"lat": deque([(100, 10.0), (110, 12.0), (120, 400.0), (130, 900.0), (140, 950.0)])}
    a = Anomaly("lat", "orders", 950.0, 250.0, ">", "p95")
    assert incident_start([a], history, detected_at=145) == 120
    silent = Anomaly("lat2", "gw", None, 0, "no_data", "")
    assert incident_start([silent], {"lat2": deque([(1, 5.0), (2, None), (3, None)])}, detected_at=4) == 2


def test_the_origin_is_the_failing_service_that_depends_on_no_other_failing_one():
    from overwatch.impact import deepest_failing

    topology = {"api": ["postgres", "redis"], "prometheus": ["api"], "postgres": [], "redis": []}
    assert deepest_failing(topology, {"postgres"}) == {"postgres"}
    assert deepest_failing(topology, {"api", "postgres"}) == {"postgres"}  # api fails because postgres does
    assert deepest_failing(topology, {"postgres", "redis"}) == {"postgres", "redis"}  # two independent roots


def test_everything_that_calls_a_failing_service_is_at_risk_even_without_a_named_origin():
    from overwatch.impact import spread

    topology = {"api": ["postgres", "redis"], "prometheus": ["api"], "postgres": [], "redis": []}
    s = spread(topology, None, {"postgres"})
    assert s["at_risk"] == ["api", "prometheus"] and s["affected"] == ["postgres"]
    assert {"from": "api", "to": "postgres"} in s["edges"] and {"from": "prometheus", "to": "api"} in s["edges"]
    assert {"from": "api", "to": "redis"} not in s["edges"]  # redis is fine: that edge carries nothing
    both = spread(topology, None, {"postgres", "redis"})
    assert both["at_risk"] == ["api", "prometheus"]
