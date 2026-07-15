"""
fat_tree_topo.py — k=4 Fat-tree topology.

k=4: 4 core, 4 aggregation, 4 edge switches, 8 hosts.
Industry-standard data centre topology for SDN research.
"""
from mininet.topo import Topo

class FatTreeTopo(Topo):
    def build(self, k: int = 4):
        num_pods     = k
        num_core     = (k // 2) ** 2
        num_agg      = k * k // 2
        num_edge     = k * k // 2
        hosts_per_edge = k // 2

        core    = [self.addSwitch(f'c{i+1}')  for i in range(num_core)]
        agg     = [self.addSwitch(f'a{i+1}')  for i in range(num_agg)]
        edge    = [self.addSwitch(f'e{i+1}')  for i in range(num_edge)]

        host_id = 1
        for pod in range(num_pods):
            agg_in_pod  = agg[pod * k // 2 : (pod + 1) * k // 2]
            edge_in_pod = edge[pod * k // 2 : (pod + 1) * k // 2]

            # Edge → Agg
            for e in edge_in_pod:
                for a in agg_in_pod:
                    self.addLink(e, a, bw=100)

            # Hosts → Edge
            for e in edge_in_pod:
                for _ in range(hosts_per_edge):
                    h = self.addHost(f'h{host_id}',
                                     ip=f'10.{pod}.{host_id}.1/24')
                    self.addLink(h, e, bw=10)
                    host_id += 1

        # Agg → Core
        stride = k // 2
        for i, a in enumerate(agg):
            pod_idx   = i // stride
            local_idx = i % stride
            for j in range(stride):
                c_idx = local_idx * stride + j
                self.addLink(a, core[c_idx], bw=1000)
