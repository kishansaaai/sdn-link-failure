"""
metrics_exporter.py — Prometheus metrics for the SDN controller.

Exposes an HTTP endpoint on port 8000 (scraped by Prometheus).
All metric objects are created here and injected into SDNController.
"""
from prometheus_client import Counter, Gauge, Histogram, start_http_server
import logging

log = logging.getLogger(__name__)

RECOVERY_BUCKETS = (.005, .01, .025, .05, .075, .1, .25, .5, .75, 1.0, 2.5)


class SDNMetrics:
    def __init__(self, port: int = 8000):
        self.link_up = Gauge(
            "sdn_link_up",
            "1 if link is up, 0 if down",
            ["src", "dst"],
        )
        self.flow_install_total = Counter(
            "sdn_flow_install_total",
            "Total number of flow rules installed",
        )
        self.recovery_time_ms = Histogram(
            "sdn_recovery_time_ms",
            "Failover recovery time in milliseconds",
            buckets=[5, 10, 25, 50, 100, 250, 500, 1000, 2500],
        )
        self.active_flows = Gauge(
            "sdn_active_flows",
            "Current number of active flow rules across all switches",
        )
        self.controller_role = Gauge(
            "sdn_controller_role",
            "1 = primary, 0 = backup",
        )
        try:
            start_http_server(port)
            log.info("Prometheus metrics available at http://0.0.0.0:%d/metrics", port)
        except OSError:
            log.warning("Prometheus port %d already in use — metrics not exported", port)
