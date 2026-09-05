# SDN link-failure recovery

A working OpenFlow 1.3 lab with weighted routing, ECMP groups, automatic link
recovery, Redis-backed controller failover, a read-only API and Prometheus/Grafana.

The controller runs on **Python 3.10**. Mininet requires Linux, root privileges
and Open vSwitch. Ubuntu 22.04 is the reference installation. Windows users
run the network inside WSL2; use the userspace datapath if the WSL kernel lacks
the Open vSwitch module.

## Start the lab

### Option A: Docker control plane + Linux Mininet

From this directory:

```bash
docker compose up --build -d --wait
sudo apt-get install -y mininet openvswitch-switch
sudo python3 -m topologies.runner --topo mesh --ports 6633,6634
```

Compose starts Redis, two controllers, Prometheus and Grafana. The second
command starts the actual data plane on the Linux host; **Compose alone does
not create switches or hosts**. Both controller addresses must be reachable
from the Mininet host. Ports are published on loopback by default.

At the Mininet prompt:

```text
pingall
link s1 s3 down
pingall
link s1 s3 up
pingall
exit
```

Allow a few seconds for discovery before the first ping. Ring and fat-tree
are available through `--topo ring` and `--topo fattree`.

| Service | Address |
|---|---|
| Primary API | http://localhost:5000/health |
| Backup API | http://localhost:5001/health |
| Topology / forwarding paths | /topology and /paths on either API |
| Recovery events / metrics | /recovery-log and /metrics on either API |
| Prometheus | http://localhost:9090 |
| Grafana | http://localhost:3000 — admin/admin for this local lab |

Set `GRAFANA_ADMIN_PASSWORD` before the first Grafana startup to choose another
password. Stop the control plane with `docker compose down`.

### Option B: native controller

```bash
sudo apt-get update
sudo apt-get install -y python3.10-venv mininet openvswitch-switch redis-server
bash setup_wsl.sh
.venv/bin/python start_ryu.py
```

In a second terminal:

```bash
sudo python3 -m topologies.runner --topo mesh
```

The default is standalone mode and needs no Redis. The supported launcher
handles Ryu's old Eventlet import and forwards Ryu command-line flags.
Do not use `--observe-links`: this application owns discovery and gates all
discovery packets on the leader lease.

For native HA, run both processes and connect Mininet to both ports:

```bash
CONTROLLER_ROLE=primary OF_PORT=6633 API_PORT=5000 .venv/bin/python start_ryu.py
CONTROLLER_ROLE=backup OF_PORT=6634 API_PORT=5001 .venv/bin/python start_ryu.py
# In another terminal:
sudo python3 -m topologies.runner --topo mesh --ports 6633,6634
```

Redis defaults to 127.0.0.1:6379. Override with `REDIS_HOST` and `REDIS_PORT`.
These two commands belong in separate terminals. In HA mode a Redis outage
withdraws controller writes; it never silently enables standalone mode.

### WSL2

Install Python 3.10 in your Ubuntu distribution before running `setup_wsl.sh`.
On distributions without a Python 3.10 package, use the Docker controller
image (built on Ubuntu 22.04) or an Ubuntu 22.04 WSL distribution.

If the OVS kernel module is missing, start its userspace daemon and use:

```bash
sudo ovs-vswitchd --pidfile --detach --log-file  # only if no daemon is running
sudo python3 -m topologies.runner --topo mesh --datapath user --unshaped
```

The userspace mode requires /dev/net/tun. `--unshaped` disables bandwidth
emulation; results from that mode are functional checks, not capacity tests.

## Verify the complete system

Unit/regression tests use real OpenFlow encoders plus an in-memory Redis
implementation executing the lease Lua scripts:

```bash
.venv/bin/python -m pytest
.venv/bin/ruff check .
.venv/bin/mypy ryu_controller/topology_graph.py ha --ignore-missing-imports
```

The real-switch integration suite starts its own two controllers and Redis
on ports 16633/16634, 15000/15001 and 16379. Run it when no other Mininet lab
is using these switch names:

```bash
sudo python3 tests/integration_lab.py --controller-python "$PWD/.venv/bin/python"
# WSL without the OVS kernel module:
sudo python3 tests/integration_lab.py --controller-python "$PWD/.venv/bin/python" --datapath user
```

It verifies all-pairs connectivity, ECMP routes, a used link failing, complete
partition and healing, killing the active controller, another link failure
after takeover, and restarting the former primary. Logs and JSON results go
to `benchmarks/live/`. GitHub Actions runs these checks and builds/starts the
Compose stack.

For continuous packet sampling against an already running standalone controller:

```bash
sudo python3 chaos_test.py --topo mesh --duration 60
# Use --api http://localhost:5001 if the backup currently owns the lease.
```

The chaos runner owns its Mininet topology and stops it even if a test fails.
It samples a flow whose installed path actually uses the failed link, verifies
packet delivery while that link stays down, and saves per-event measurements.

## What is implemented

- LLDP discovery and immediate OpenFlow port-down handling; silent links expire.
- Source learning from ARP/broadcasts and direct, loop-free replication to
  reachable access ports. Cycles do not require spanning-tree blocking.
- Weighted Dijkstra and up to four near-equal paths. SELECT groups are referenced
  by flows at every branch; all next hops decrease distance to the destination.
- Affected-route replacement, group cleanup, unreachable-state reporting,
  restoration, host moves and switch disconnects.
- Port-stat deltas calculate TX utilization and loss using packet counters.
  Costs use remaining bandwidth. Capacity defaults to 100 Mbps and is configurable
  with `LINK_CAPACITY_MBPS`; latency is a configured graph metric (default 1 ms),
  **not a claimed one-way LLDP latency measurement**.
- Redis owner-checked leases, periodic state/intent snapshots, monotonically
  increasing OpenFlow generations and master/slave role negotiation.
- Serializable API responses, bounded recovery logs, per-process metric registries,
  and a provisioned Grafana dashboard.

## Measurement and scope

`recovery_ms` is controller computation plus message enqueue time. It is not
switch acknowledgement or end-to-end packet recovery. Continuous ping results
have an explicit sampling interval and count leading/trailing outages.
The old simulated/unsupported performance table has been removed; see
[validated results](benchmarks/results.md).

This is a dedicated single-tenant L2 research lab: switches' group tables are
reconciled on controller takeover, Redis is a single coordination dependency,
and OpenFlow 1.3 updates are not atomic across switches. It is not a production
HA cluster or a guaranteed sub-100ms recovery system. Multi-tenant VLAN routing,
parallel links between the same switch pair, P4 and real-hardware timing are
outside this implementation.

Architecture and failure behavior: [ARCHITECTURE.md](ARCHITECTURE.md).
The historical POX version and its API are preserved in [legacy/](legacy/).
