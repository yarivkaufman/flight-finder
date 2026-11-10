import time

from fastapi.testclient import TestClient

from flight_finder.app import create_app
from flight_finder.config import Settings


def wait_idle(agent, timeout=10):
    end = time.time() + timeout
    while (agent.busy or agent.lock.locked()) and time.time() < end:
        time.sleep(0.05)


def test_dashboard_demo_flow(tmp_path):
    s = Settings(data_dir=tmp_path, config_path=tmp_path / "none.yaml", anthropic_configured=False,
                 demo=True)
    app = create_app(s)
    agent = app.state.agent
    with TestClient(app) as c:
        assert c.get("/healthz").json()["ok"]
        assert "No trips yet" in c.get("/").text
        r = c.post("/trips", data={"origin": "EWR", "destination": "TLV", "depart": "2027-02-10",
                                   "ret": "2027-02-20"}, follow_redirects=False)
        assert r.status_code == 303
        wait_idle(agent)
        page = c.get("/").text
        assert "EWR → TLV" in page and "Nearby dates" in page
        trip_id = agent.db.trips()[0]["id"]
        t = c.get(f"/trips/{trip_id}").text
        assert "Best options" in t and "Google Flights" in t and "Cheapest by dates" in t
        c.post(f"/trips/{trip_id}/settings", data={"name": "Winter TLV", "target_price": "$1,200",
                                                   "max_stops": "1", "flex_days": "2"})
        trip = agent.db.trip(trip_id)
        assert trip["name"] == "Winter TLV" and trip["target_price"] == 1200 and trip["max_stops"] == 1
        c.post(f"/trips/{trip_id}/check", data={"kind": "daily"})
        wait_idle(agent)
        assert len(agent.db.checks(trip_id)) == 2
        assert "Weekly" not in c.get("/runs").text
        assert agent.db.emails(trip_id)  # the "now watching" email is queued
        c.post(f"/trips/{trip_id}/delete")
        assert agent.db.trips(None) == []
