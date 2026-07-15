from mininet.topo import Topo
from mininet.net import Mininet
from mininet.node import RemoteController
from mininet.cli import CLI
from mininet.log import setLogLevel, info
from mininet.link import Link
from mininet.node import OVSSwitch
import time
import re

class MeshTopo(Topo):
    def build(self):
        # 6 hosts
        h1 = self.addHost('h1', ip='10.0.0.1/24')
        h2 = self.addHost('h2', ip='10.0.0.2/24')
        h3 = self.addHost('h3', ip='10.0.0.3/24')
        h4 = self.addHost('h4', ip='10.0.0.4/24')
        h5 = self.addHost('h5', ip='10.0.0.5/24')
        h6 = self.addHost('h6', ip='10.0.0.6/24')

        # 5 switches
        s1 = self.addSwitch('s1')
        s2 = self.addSwitch('s2')
        s3 = self.addSwitch('s3')
        s4 = self.addSwitch('s4')
        s5 = self.addSwitch('s5')

        # Connect hosts to switches
        self.addLink(h1, s1, bw=10)
        self.addLink(h2, s1, bw=10)
        self.addLink(h3, s3, bw=10)
        self.addLink(h4, s4, bw=10)
        self.addLink(h5, s5, bw=10)
        self.addLink(h6, s5, bw=10)

        # Connect switches (Ring + some cross links for mesh)
        self.addLink(s1, s2, bw=100) # link 1
        self.addLink(s2, s3, bw=100) # link 2
        self.addLink(s3, s4, bw=100) # link 3
        self.addLink(s4, s5, bw=100) # link 4
        self.addLink(s5, s1, bw=100) # link 5 (ring completed)
        
        # Cross links
        self.addLink(s1, s3, bw=100)
        self.addLink(s2, s5, bw=100)

def measure_ping(net):
    loss_pattern = re.compile(r"(\d+)% dropped")
    info("Running pingall...\n")
    output = net.pingAll()
    # Mininet pingAll returns drop percentage indirectly, but prints it. 
    # Actually, net.pingAll() doesn't return a string.
    # We can measure it manually.
    drop_pct = net.pingAll() 
    return drop_pct

def run():
    topo = MeshTopo()
    net = Mininet(
        topo=topo,
        controller=RemoteController('c0', ip='127.0.0.1', port=6653),
        switch=lambda name, **kwargs: OVSSwitch(name, protocols='OpenFlow13', **kwargs),
        link=Link,
        autoSetMacs=True
    )
    net.start()
    info('Waiting 10s for controller and discovery...\n')
    time.sleep(10)
    
    info('\n=== INITIAL PING (Learning paths) ===\n')
    net.pingAll()
    time.sleep(2)
    
    info('\n=== SCENARIO 1: Normal Operation ===\n')
    loss_before = net.pingAll()
    
    info('\n=== SCENARIO 2: Link s1-s2 DOWN ===\n')
    net.configLinkStatus('s1', 's2', 'down')
    # Sleep small amount to let controller react and push flows
    time.sleep(1) 
    loss_during = net.pingAll()
    
    info('\n=== SCENARIO 3: Link s1-s2 UP ===\n')
    net.configLinkStatus('s1', 's2', 'up')
    time.sleep(1)
    loss_after = net.pingAll()
    
    # Print clean summary table
    print("\n")
    print("="*40)
    print("      FAILOVER TEST SUMMARY")
    print("="*40)
    print(f"Before Failure (Normal):  {loss_before:.1f}% loss")
    print(f"During Failure (s1-s2):   {loss_during:.1f}% loss")
    print(f"After Restore (s1-s2 up): {loss_after:.1f}% loss")
    print("="*40)
    print("\n")
    
    info('Entering CLI (You can exit anytime)\n')
    CLI(net)
    net.stop()

if __name__ == '__main__':
    setLogLevel('info')
    run()
