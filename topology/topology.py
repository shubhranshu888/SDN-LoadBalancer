from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.cli import CLI
from mininet.log import setLogLevel

def run_topology():
    setLogLevel('info')
    
    # Initialize Mininet with a Remote Controller (Ryu)
    net = Mininet(controller=RemoteController, switch=OVSSwitch)
    
    print("*** Adding controller")
    c0 = net.addController('c0', controller=RemoteController, ip='127.0.0.1', port=6653)
    
    print("*** Adding switch")
    s1 = net.addSwitch('s1', protocols='OpenFlow13')
    
    print("*** Adding Backend Server hosts")
    srv1 = net.addHost('srv1', ip='10.0.0.1', mac='00:00:00:00:00:01')
    srv2 = net.addHost('srv2', ip='10.0.0.2', mac='00:00:00:00:00:02')
    srv3 = net.addHost('srv3', ip='10.0.0.3', mac='00:00:00:00:00:03')
    
    print("*** Adding Client hosts")
    c1 = net.addHost('c1', ip='10.0.0.4', mac='00:00:00:00:00:04')
    c2 = net.addHost('c2', ip='10.0.0.5', mac='00:00:00:00:00:05')
    c3 = net.addHost('c3', ip='10.0.0.6', mac='00:00:00:00:00:06')
    
    print("*** Creating links")
    net.addLink(srv1, s1)
    net.addLink(srv2, s1)
    net.addLink(srv3, s1)
    net.addLink(c1, s1)
    net.addLink(c2, s1)
    net.addLink(c3, s1)
    
    print("*** Starting network")
    net.build()
    c0.start()
    s1.start([c0])
    
    print("*** Running CLI")
    CLI(net)
    
    print("*** Stopping network")
    net.stop()

if __name__ == '__main__':
    run_topology()
