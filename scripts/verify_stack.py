#!/usr/bin/env python3
"""Verify both controllers, Prometheus scrape targets and Grafana provisioning."""
import base64
import json
import os
import time
from urllib.request import Request, urlopen


def get(url, auth=False):
    headers = {}
    if auth:
        credentials = ("admin:" + os.getenv("GRAFANA_ADMIN_PASSWORD", "admin")).encode()
        headers["Authorization"] = "Basic " + base64.b64encode(credentials).decode()
    with urlopen(Request(url, headers=headers), timeout=3) as response:
        return json.load(response)


def verify():
    deadline = time.monotonic() + 60
    last_error = None
    while time.monotonic() < deadline:
        try:
            health = [get(f"http://127.0.0.1:{port}/health") for port in (5000, 5001)]
            assert sorted(item["role"] for item in health) == ["backup", "primary"]
            assert all(item["ha_error"] is None for item in health)
            targets = get("http://127.0.0.1:9090/api/v1/targets")["data"]["activeTargets"]
            assert len(targets) == 2 and all(target["health"] == "up" for target in targets)
            assert get("http://127.0.0.1:3000/api/health")["database"] == "ok"
            datasource = get("http://127.0.0.1:3000/api/datasources/uid/sdn-prometheus", auth=True)
            assert datasource["url"] == "http://prometheus:9090"
            dashboard = get("http://127.0.0.1:3000/api/dashboards/uid/sdn-controller-v2", auth=True)
            assert len(dashboard["dashboard"]["panels"]) == 7
            print("Verified HA roles, both Prometheus targets, Grafana datasource and dashboard.")
            return
        except Exception as exc:
            last_error = exc
            time.sleep(1)
    raise RuntimeError(f"Stack verification did not pass: {last_error}")


if __name__ == "__main__":
    verify()
