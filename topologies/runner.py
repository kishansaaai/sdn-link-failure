"""Shared Mininet network factory and interactive CLI."""
import argparse
import os
from functools import partial


def build_topo(name):
    if name == "ring":
        from topologies.ring_topo import RingTopo
        return RingTopo()
    if name == "mesh":
        from topologies.mesh_topo import MeshTopo
        return MeshTopo()
    if name == "fattree":
        from topologies.fat_tree_topo import FatTreeTopo
        return FatTreeTopo()
    raise ValueError(f"Unknown topology: {name}")


def create_network(name, controller_ip="127.0.0.1", ports=(6633,),
                   datapath="kernel", shaped=True):
    from mininet.net import Mininet
    from mininet.node import OVSSwitch, RemoteController
    from mininet.link import TCLink, Link

    net = Mininet(topo=build_topo(name), controller=None,
                  switch=partial(OVSSwitch, protocols="OpenFlow13", datapath=datapath,
                                 failMode="secure"),
                  link=TCLink if shaped else Link, autoSetMacs=True, waitConnected=True)
    for index, port in enumerate(ports):
        net.addController(f"controller{index}", controller=RemoteController,
                          ip=controller_ip, port=int(port))
    return net


def main():
    parser = argparse.ArgumentParser(description="Interactive SDN lab")
    parser.add_argument("--topo", choices=["ring", "mesh", "fattree"], default="mesh")
    parser.add_argument("--controller-ip", default="127.0.0.1")
    parser.add_argument("--ports", default="6633", help="Comma-separated OpenFlow ports")
    parser.add_argument("--datapath", choices=["kernel", "user"], default="kernel")
    parser.add_argument("--unshaped", action="store_true", help="Skip TC bandwidth limits")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Mininet requires root")
    from mininet.cli import CLI
    net = create_network(args.topo, args.controller_ip, args.ports.split(","),
                         args.datapath, not args.unshaped)
    try:
        net.start()
        CLI(net)
    finally:
        net.stop()


if __name__ == "__main__":
    main()
