# SDN Link Failure Detection and Recovery (Problem 14)

## Problem Statement
Implement an SDN solution using Mininet and POX controller to detect link failures and dynamically update routing using OpenFlow flow rules.

## Topology
h1, h2 -- s1 -- s2 -- s3 -- h3, h4
Backup link: s1 -- s3

## Setup & Execution

### Requirements
- Ubuntu Linux
- Mininet: `sudo apt-get install mininet`
- POX: `git clone https://github.com/noxrepo/pox.git`
- iperf: `sudo apt-get install iperf`

### Steps
1. Copy `link_failure_recovery.py` to `pox/ext/`
2. Terminal 1 - Start controller: `cd pox && python3 pox.py log.level --DEBUG openflow.of_01 forwarding.l2_learning`
3. Terminal 2 - Start topology: `sudo python3 link_failure_topo.py`

## Test Scenarios
- **Scenario 1**: Normal operation - all hosts communicate (0% loss)
- **Scenario 2**: `link s1 s2 down` - primary link fails, traffic reroutes via backup s1-s3
- **Scenario 3**: `link s1 s2 up` - link restored, full connectivity (0% loss)

## Expected Output
- Scenario 1: 0% packet loss
- Scenario 2: partial loss during failure, recovery via backup path
- Scenario 3: 0% packet loss after restoration

## References
- Mininet: http://mininet.org
- POX Controller: https://github.com/noxrepo/pox
- OpenFlow 1.0 Spec: https://opennetworking.org
