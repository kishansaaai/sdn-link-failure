# Architecture

## Packet and topology path

The active controller sends its own LLDP probes through physical ports every
second. Received probes identify both switches and their port numbers. A
port-down event removes the link immediately; missing probes expire after four
seconds. Standby controllers send no probes or flow changes.

A source MAC is learned on an access port before broadcast handling. Broadcast
and unknown-unicast packets are copied directly from the controller to reachable
access ports. They are never flooded through switch-to-switch links, avoiding
broadcast cycles without STP. Known unicast traffic receives complete paths.

## Routing and flow lifecycle

The routing cost combines latency, inverse available bandwidth, and packet loss.
The graph validates finite, nonnegative metrics. Default link latency is 1 ms;
port stats supply utilization/loss, not physical one-way latency. Traffic-engineering
recomputation is triggered by a utilization change of at least 15 percentage points.

ECMP computes a destination-distance DAG without mutating the graph. Up to four
near-equal paths are returned. Each branch switch uses a SELECT group referenced
by its source/destination flow. Shared downstream branches cannot form cycles
because every next hop strictly decreases distance to the destination.

The controller keeps the entire path set for each source/destination intent.
Failures replace only affected routes; unreachable destinations lose old
rules and groups but retain their intent for restoration. Flow rules are
permanent until reconciled, so controller intent and idle timeouts cannot drift.
Updates are sent downstream first, then obsolete rules/groups are removed.
This is not a cross-switch transaction: brief packet loss during an update
is possible and must be measured in the data plane.

The application uses an SDN cookie prefix for flow deletion. On handshake or
takeover it reconciles its flows and clears the dedicated lab switch's group
table, then rebuilds intents. It therefore requires exclusive group-table ownership.

## Controller HA

Both configured HA processes use the same election state machine. The first
successful Redis lease acquisition becomes active; the primary/backup names
are startup preferences established by Compose ordering, not permanent identities.

Acquisition, renewal, publication and release use atomic Lua scripts checking
the lease owner. A lease lasts five seconds and renews every second. Successful
acquisition increments a persistent generation counter, used in OpenFlow MASTER
requests. Standbys query each switch's generation before requesting SLAVE and
retry if another controller promotes during that exchange.

A controller stops issuing application writes when it loses Redis access,
ownership, or its conservative local lease deadline. The backup must acquire
the lease before loading the last topology/intent snapshot and requesting MASTER.
Only MASTER role replies enable programming on each switch. An old primary
rejoining remains standby while the current lease is valid.

Snapshots include graph, hosts, intents and the bounded recovery log. They may
be one interval stale; LLDP and port events reconcile them. Redis AOF data keeps
the generation counter across normal restarts. Do not erase Redis state while
switches retain controller generations; that requires a coordinated lab reset.
Queued OpenFlow messages and a Redis/switch network partition cannot provide
the guarantees of a consensus-based production controller cluster.

## Interfaces and observability

The Ryu WSGI listener serves /health, /topology, /paths, /recovery-log and
/metrics. /health is process health and includes role, connected/ready switch
counts, generation and the most recent HA error; an empty topology can still
be a healthy process. A standby's topology may be empty until takeover.

Prometheus metrics include link state/utilization, installed FlowMods, desired
path-rule count, controller role, unexpected OpenFlow errors and controller
enqueue-time distributions. Metric registries are isolated per app instance.
Grafana reads the two API listeners through Docker service DNS.

## Validation

The unit suite verifies graph invariants, routing and packet handling with real
Ryu wire encoders, API serialization, counter reset behavior, lease fencing and
measurement parsing. The integration suite uses Mininet and actual Open vSwitch
processes for ring, mesh and canonical k=4 fat-tree topologies.

The chaos runner measures continuous ping replies and verifies connectivity
while the link is still down. The packet-capture utility checks actual port-down
events followed by add/modify commands on the same OpenFlow connection. It
rejects captures without evidence and does not mistake deletes for installs.
Neither tool equates controller messages with guaranteed data-plane recovery.
