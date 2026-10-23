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
