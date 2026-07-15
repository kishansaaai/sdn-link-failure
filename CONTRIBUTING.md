# Contributing

Thank you for considering a contribution to this project!

## Development Environment

```bash
# Clone and set up
git clone https://github.com/kishansaaai/sdn-link-failure
cd sdn-link-failure

# Install Python dependencies
pip install -r requirements.txt

# Run tests
pytest tests/ --cov=ryu_controller --cov=ha -v
```

## Running the Controller Locally (requires Ubuntu + Mininet)

```bash
# Full stack via Docker (recommended)
docker compose up

# OR bare metal
ryu-manager --observe-links ryu_controller/sdn_controller.py &
sudo python3 topologies/mesh_topo.py
```

## Code Style

This project uses [ruff](https://docs.astral.sh/ruff/) for linting:
```bash
ruff check ryu_controller/ ha/
```

And [mypy](https://mypy.readthedocs.io/) for type checking:
```bash
mypy ryu_controller/topology_graph.py ha/backup.py --ignore-missing-imports
```

## Adding a New Topology

1. Create `topologies/your_topo.py` with a `Topo` subclass named `YourTopo`.
2. Import it in `chaos_test.py`'s `build_topo()` factory function.
3. Add at least one test to `tests/test_topology_graph.py` verifying routing works on it.

## Pull Request Checklist

- [ ] All tests pass (`pytest tests/`)
- [ ] Coverage stays ≥ 80% on `ryu_controller/` and `ha/`
- [ ] Ruff passes with no errors
- [ ] mypy passes on modified files
- [ ] New behaviour is documented in the relevant module docstring

## Reporting Issues

Please include:
- OS and Python version
- Whether you're using Docker or bare-metal
- Relevant log output from the controller
