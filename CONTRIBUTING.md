# Contributing

Use Ubuntu 22.04 and Python 3.10 for the supported controller runtime.
Run `bash setup_wsl.sh` to create an isolated environment without deleting an
existing environment or modifying system Python.

Before submitting changes:

```bash
.venv/bin/ruff check .
.venv/bin/mypy ryu_controller/topology_graph.py ha --ignore-missing-imports
.venv/bin/python -m pytest --cov=ryu_controller --cov=ha
sudo python3 tests/integration_lab.py --controller-python "$PWD/.venv/bin/python"
docker compose up --build -d --wait
docker compose down
```

Unit coverage excludes live-switch event paths; review the integration results
alongside it. Add regression tests for changed routing, role, protocol and
measurement behavior. Keep the distinction between message timing and actual
packet delivery in documentation and benchmark output.

New topologies belong in topologies/ and must use unique OpenFlow datapath IDs
and mutually reachable host IPs. Register their factory in topologies/runner.py.
The integration harness identifies switches by object/DPID, not by name prefixes.

The legacy/ directory preserves the original POX experiment. New controller
work belongs in ryu_controller/ and ha/.

Report failures with OS/Python/OVS versions, topology, datapath type, relevant
controller logs and benchmark JSON. Never include credentials or packet captures
containing unrelated private traffic.
