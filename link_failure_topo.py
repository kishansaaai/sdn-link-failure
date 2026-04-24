from mininet.topo import Topo
from mininet.net import Mininet
from mininet.node import RemoteController
from mininet.cli import CLI
from mininet.log import setLogLevel, info
from mininet.link import TCLink
import time

class LinkFailureTopo(Topo):
    def build(self):
        h1 = self.addHost('h1', ip='10.0.0.1/24')
        h2 = self.addHost('h2', ip='10.0.0.2/24')
        h3 = self.addHost('h3', ip='10.0.0.3/24')
        h4 = self.addHost('h4', ip='10.0.0.4/24')
        s1 = self.addSwitch('s1')
        s2 = self.addSwitch('s2')
        s3 = self.addSwitch('s3')
        self.addLink(h1, s1, bw=10, delay='5ms')
        self.addLink(h2, s1, bw=10, delay='5ms')
        self.addLink(h3, s3, bw=10, delay='5ms')
        self.addLink(h4, s3, bw=10, delay='5ms')
        self.addLink(s1, s2, bw=100, delay='2ms')
        self.addLink(s2, s3, bw=100, delay='2ms')
        self.addLink(s1, s3, bw=10, delay='20ms')

def run():
    topo = LinkFailureTopo()
    net = Mininet(topo=topo, controller=RemoteController('c0', ip='127.0.0.1', port=6633), link=TCLink, autoSetMacs=True)
    net.start()
    info('Waiting 5s for controller...\n')
    time.sleep(5)
    info('=== SCENARIO 1: Normal Operation ===\n')
    net.pingAll()
    time.sleep(2)
    info('=== Running pingall again to learn all paths ===\n')
    net.pingAll()
    time.sleep(2)
    info('=== SCENARIO 2: Simulating Link Failure s1-s2 ===\n')
    net.configLinkStatus('s1', 's2', 'down')
    time.sleep(3)
    net.pingAll()
    time.sleep(2)
    info('=== SCENARIO 3: Link Restoration ===\n')
    net.configLinkStatus('s1', 's2', 'up')
    time.sleep(3)
    net.pingAll()
    info('Entering CLI\n')
    CLI(net)
    net.stop()

if __name__ == '__main__':
    setLogLevel('info')
    run()
