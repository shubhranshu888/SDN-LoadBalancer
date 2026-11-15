import argparse
from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController


def build_topology(
    controller_ip="127.0.0.1",
    controller_port=6653,
    bw_mbps=10.0,
    delay_ms=1.0,
    srv1_bw=None,
    srv1_delay=None,
    srv1_loss=0.0,
):
    """Builds the Mininet star topology with 1 OpenFlow 1.3 switch, 3 servers, and 3 clients."""
    net = Mininet(controller=RemoteController, switch=OVSSwitch, link=TCLink)

    print("*** Adding Remote Ryu Controller")
    c0 = net.addController("c0", controller=RemoteController, ip=controller_ip, port=controller_port)

    print("*** Adding OpenFlow 1.3 Switch (s1)")
    s1 = net.addSwitch("s1", protocols="OpenFlow13")

    print("*** Adding Backend Server hosts (ports 1-3)")
    srv1 = net.addHost("srv1", ip="10.0.0.1/24", mac="00:00:00:00:00:01")
    srv2 = net.addHost("srv2", ip="10.0.0.2/24", mac="00:00:00:00:00:02")
    srv3 = net.addHost("srv3", ip="10.0.0.3/24", mac="00:00:00:00:00:03")

    print("*** Adding Client hosts (ports 4-6)")
    c1 = net.addHost("c1", ip="10.0.0.4/24", mac="00:00:00:00:00:04")
    c2 = net.addHost("c2", ip="10.0.0.5/24", mac="00:00:00:00:00:05")
    c3 = net.addHost("c3", ip="10.0.0.6/24", mac="00:00:00:00:00:06")

    default_delay = f"{delay_ms}ms"
    srv1_link_opts = {
        "bw": srv1_bw if srv1_bw is not None else bw_mbps,
        "delay": f"{srv1_delay}ms" if srv1_delay is not None else default_delay,
    }
    if srv1_loss and srv1_loss > 0:
        srv1_link_opts["loss"] = float(srv1_loss)

    print(f"*** Creating TCLinks (default bw={bw_mbps} Mbps, delay={default_delay})")
    net.addLink(srv1, s1, **srv1_link_opts)
    net.addLink(srv2, s1, bw=bw_mbps, delay=default_delay)
    net.addLink(srv3, s1, bw=bw_mbps, delay=default_delay)
    net.addLink(c1, s1, bw=bw_mbps, delay=default_delay)
    net.addLink(c2, s1, bw=bw_mbps, delay=default_delay)
    net.addLink(c3, s1, bw=bw_mbps, delay=default_delay)

    return net, c0, s1


def run_topology(args):
    setLogLevel("info")
    net, c0, s1 = build_topology(
        controller_ip=args.controller_ip,
        controller_port=args.controller_port,
        bw_mbps=args.bw,
        delay_ms=args.delay,
        srv1_bw=args.srv1_bw,
        srv1_delay=args.srv1_delay,
        srv1_loss=args.srv1_loss,
    )

    print("*** Starting network")
    net.build()
    c0.start()
    s1.start([c0])

    print("\n" + "=" * 65)
    print(" SDN Load Balancer Mininet Topology Ready")
    print("=" * 65)
    print(" Virtual IP (VIP)  : 10.0.0.100:8080")
    print(" Telemetry IP      : 10.0.0.254:5555 (UDP in-band to Ryu)")
    print(" Backend Servers   : srv1=10.0.0.1 (p1), srv2=10.0.0.2 (p2), srv3=10.0.0.3 (p3)")
    print(" Clients           : c1=10.0.0.4   (p4), c2=10.0.0.5   (p5), c3=10.0.0.6   (p6)")
    print("-" * 65)
    print(" Quick Start inside mininet>:")
    print("   srv1 python3 server/tcp_server.py srv1 10.0.0.1 &")
    print("   srv2 python3 server/tcp_server.py srv2 10.0.0.2 &")
    print("   srv3 python3 server/tcp_server.py srv3 10.0.0.3 &")
    print("   c1 python3 client/tcp_client.py 10.0.0.100 15 --concurrency 5")
    print("=" * 65 + "\n")

    CLI(net)

    print("*** Stopping network")
    net.stop()


def parse_args():
    parser = argparse.ArgumentParser(description="Mininet Topology for SDN Load Balancer")
    parser.add_argument("--controller-ip", default="127.0.0.1", help="Ryu controller IP (default: 127.0.0.1)")
    parser.add_argument("--controller-port", type=int, default=6653, help="Ryu controller OpenFlow port (default: 6653)")
    parser.add_argument("--bw", type=float, default=10.0, help="Link bandwidth in Mbps (default: 10.0)")
    parser.add_argument("--delay", type=float, default=1.0, help="Base link delay in ms (default: 1.0)")
    parser.add_argument("--srv1-bw", type=float, default=None, help="Optional bottleneck bw (Mbps) on srv1 link")
    parser.add_argument("--srv1-delay", type=float, default=None, help="Optional extra delay (ms) on srv1 link")
    parser.add_argument("--srv1-loss", type=float, default=0.0, help="Optional packet loss %% on srv1 link")
    return parser.parse_args()


if __name__ == "__main__":
    run_topology(parse_args())
