"""ring_topo.py — 4-switch ring, 4 hosts. Demonstrates single alternate path."""
from mininet.topo import Topo

class RingTopo(Topo):
    def build(self):
        # Switches in a ring: s1 - s2 - s3 - s4 - s1
        switches = [self.addSwitch(f's{i}') for i in range(1, 5)]
        for i in range(4):
            self.addLink(switches[i], switches[(i + 1) % 4], bw=100)
        # One host per switch
        for i, sw in enumerate(switches, 1):
            h = self.addHost(f'h{i}', ip=f'10.0.0.{i}/24')
            self.addLink(h, sw, bw=10)
