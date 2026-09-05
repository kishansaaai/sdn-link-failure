"""Canonical k-ary fat-tree: k=4 has 20 switches and 16 hosts."""
from mininet.topo import Topo


class FatTreeTopo(Topo):
    def build(self, k=4):
        if not isinstance(k, int) or k < 2 or k % 2:
            raise ValueError("k must be an even integer >= 2")
        next_id = 1

        def switch(name):
            nonlocal next_id
            result = self.addSwitch(name, dpid=f"{next_id:016x}", protocols="OpenFlow13")
            next_id += 1
            return result

        core = [switch(f"c{i + 1}") for i in range((k // 2) ** 2)]
        agg = [switch(f"a{i + 1}") for i in range(k * k // 2)]
        edge = [switch(f"e{i + 1}") for i in range(k * k // 2)]
        host_id = 1
        stride = k // 2
        for pod in range(k):
            pod_agg = agg[pod * stride:(pod + 1) * stride]
            pod_edge = edge[pod * stride:(pod + 1) * stride]
            for edge_switch in pod_edge:
                for agg_switch in pod_agg:
                    self.addLink(edge_switch, agg_switch, bw=100)
                for _ in range(stride):
                    host = self.addHost(f"h{host_id}", ip=f"10.0.0.{host_id}/24")
                    self.addLink(host, edge_switch, bw=10)
                    host_id += 1
            for local, agg_switch in enumerate(pod_agg):
                for j in range(stride):
                    self.addLink(agg_switch, core[local * stride + j], bw=100)
