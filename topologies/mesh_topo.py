"""mesh_topo.py — 5-switch partial mesh, 6 hosts. Multiple alternative paths."""
from mininet.topo import Topo

class MeshTopo(Topo):
    def build(self):
        s = [self.addSwitch(f's{i}') for i in range(1, 6)]
        # Ring
        for i in range(5):
            self.addLink(s[i], s[(i + 1) % 5], bw=100)
        # Cross links
        self.addLink(s[0], s[2], bw=100)
        self.addLink(s[1], s[4], bw=100)
        # Hosts
        hosts = [self.addHost(f'h{i}', ip=f'10.0.0.{i}/24') for i in range(1, 7)]
        for i, sw_idx in enumerate([0, 0, 2, 3, 4, 4]):
            self.addLink(hosts[i], s[sw_idx], bw=10)
