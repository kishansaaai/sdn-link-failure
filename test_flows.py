import time
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.link import Link
from topologies.mesh_topo import MeshTopo
import subprocess

if __name__ == '__main__':
    topo = MeshTopo()
    net = Mininet(
        topo=topo,
        controller=RemoteController("c0", ip="127.0.0.1", port=6653),
        switch=lambda name, **kwargs: OVSSwitch(name, protocols='OpenFlow13', datapath='user', **kwargs),
        link=Link,
        autoSetMacs=True,
    )
    net.start()
    print("Waiting 10s for discovery...")
    time.sleep(10)
    
    print("Running pingAll...")
    net.pingAll(timeout=1)
    
    print("\n--- dump-flows s1 ---")
    subprocess.run(["sudo", "ovs-ofctl", "-O", "OpenFlow13", "dump-flows", "s1"])
    
    print("\n--- dump-groups s1 ---")
    subprocess.run(["sudo", "ovs-ofctl", "-O", "OpenFlow13", "dump-groups", "s1"])
    
    net.stop()
