"""Per-controller Prometheus registry, served by the same WSGI API."""
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram


class SDNMetrics:
    def __init__(self):
        self.registry = CollectorRegistry()
        self.link_up = Gauge("sdn_link_up", "Link state", ["src", "dst"], registry=self.registry)
        self.link_utilization = Gauge("sdn_link_utilization", "TX capacity fraction", ["src", "dst"], registry=self.registry)
        self.flow_install_total = Counter("sdn_flow_install_total", "Path FlowMods sent", registry=self.registry)
        self.recovery_time_ms = Histogram("sdn_recovery_time_ms", "Controller recompute and enqueue time; not packet recovery", buckets=[1, 5, 10, 25, 50, 100, 250, 500, 1000], registry=self.registry)
        self.active_flows = Gauge("sdn_active_flows", "Desired path rules across connected switches", registry=self.registry)
        self.controller_role = Gauge("sdn_controller_role", "1 for lease owner, 0 for standby", registry=self.registry)
        self.openflow_errors = Counter("sdn_openflow_errors_total", "OpenFlow errors received", registry=self.registry)
