import time
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.lib.packet import arp, ethernet, ether_types, icmp, ipv4, packet, tcp, udp
from ryu.ofproto import ofproto_v1_3


class StaticLoadBalancer(app_manager.RyuApp):
    """D1 Baseline: Static Round-Robin SDN TCP Load Balancer (OpenFlow 1.3)."""

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(StaticLoadBalancer, self).__init__(*args, **kwargs)
        self.mac_to_port = {}
        self.virtual_ip = "10.0.0.100"
        self.virtual_mac = "00:00:00:00:00:99"
        self.virtual_port = 8080
        self.telemetry_ip = "10.0.0.254"
        self.telemetry_mac = "00:00:00:00:00:fe"
        self.telemetry_port = 5555

        self.servers = [
            {"id": "srv1", "ip": "10.0.0.1", "mac": "00:00:00:00:00:01", "port": 1},
            {"id": "srv2", "ip": "10.0.0.2", "mac": "00:00:00:00:00:02", "port": 2},
            {"id": "srv3", "ip": "10.0.0.3", "mac": "00:00:00:00:00:03", "port": 3},
        ]
        self.server_ips = {s["ip"] for s in self.servers}
        self.server_index = 0
        # Tracks active VIP-forwarded TCP flows: (client_ip, client_port) -> (server_ip, timestamp)
        self.active_vip_flows = {}

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        # Table-miss rule: send unmatched packets to controller
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 0, match, actions)

        # Quietly drop server telemetry UDP packets in static mode so they are not flooded
        tele_match = parser.OFPMatch(
            eth_type=ether_types.ETH_TYPE_IP,
            ip_proto=17,
            ipv4_dst=self.telemetry_ip,
            udp_dst=self.telemetry_port,
        )
        self.add_flow(datapath, 100, tele_match, [])
        self.logger.info("[StaticLB] Switch %s connected; OpenFlow 1.3 base rules installed.", datapath.id)

    def add_flow(self, datapath, priority, match, actions, idle_timeout=0, hard_timeout=0):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)] if actions else []
        mod = parser.OFPFlowMod(
            datapath=datapath,
            priority=priority,
            idle_timeout=idle_timeout,
            hard_timeout=hard_timeout,
            match=match,
            instructions=inst,
        )
        datapath.send_msg(mod)

    def _send_packet_out(self, datapath, msg, in_port, actions):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        data = msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None
        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=msg.buffer_id,
            in_port=in_port,
            actions=actions,
            data=data,
        )
        datapath.send_msg(out)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        in_port = msg.match["in_port"]

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None or eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        dpid = datapath.id
        self.mac_to_port.setdefault(dpid, {})
        self.mac_to_port[dpid][eth.src] = in_port

        arp_pkt = pkt.get_protocol(arp.arp)
        if arp_pkt and arp_pkt.opcode == arp.ARP_REQUEST:
            if arp_pkt.dst_ip == self.virtual_ip:
                self.reply_arp(datapath, self.virtual_mac, self.virtual_ip,
                               eth.src, arp_pkt.src_mac, arp_pkt.src_ip, in_port)
                return
            if arp_pkt.dst_ip == self.telemetry_ip:
                self.reply_arp(datapath, self.telemetry_mac, self.telemetry_ip,
                               eth.src, arp_pkt.src_mac, arp_pkt.src_ip, in_port)
                return

        ipv4_pkt = pkt.get_protocol(ipv4.ipv4)
        tcp_pkt = pkt.get_protocol(tcp.tcp)
        udp_pkt = pkt.get_protocol(udp.udp)
        icmp_pkt = pkt.get_protocol(icmp.icmp)

        # Ignore telemetry UDP traffic in static mode
        if ipv4_pkt and udp_pkt and ipv4_pkt.dst == self.telemetry_ip and udp_pkt.dst_port == self.telemetry_port:
            return

        # Respond to ICMP echo requests targeting the Virtual IP
        if ipv4_pkt and icmp_pkt and ipv4_pkt.dst == self.virtual_ip and icmp_pkt.type == icmp.ICMP_ECHO_REQUEST:
            self.reply_icmp(datapath, eth, ipv4_pkt, icmp_pkt, in_port)
            return

        # Forward TCP traffic destined to VIP:8080 using Round-Robin
        if ipv4_pkt and tcp_pkt and ipv4_pkt.dst == self.virtual_ip and tcp_pkt.dst_port == self.virtual_port:
            self.handle_tcp_forward(msg, datapath, in_port, eth, ipv4_pkt, tcp_pkt)
            return

        # Reverse NAT only for active VIP TCP flows returning from backend servers
        if ipv4_pkt and tcp_pkt and ipv4_pkt.src in self.server_ips and tcp_pkt.src_port == self.virtual_port:
            flow_key = (ipv4_pkt.dst, tcp_pkt.dst_port)
            flow_info = self.active_vip_flows.get(flow_key)
            if flow_info and flow_info[0] == ipv4_pkt.src:
                self.handle_tcp_reverse(msg, datapath, in_port, eth, ipv4_pkt, tcp_pkt)
                return

        # Standard L2 learning switch forwarding
        dst = eth.dst
        out_port = self.mac_to_port[dpid].get(dst, ofproto.OFPP_FLOOD)
        actions = [parser.OFPActionOutput(out_port)]
        if out_port != ofproto.OFPP_FLOOD:
            match = parser.OFPMatch(in_port=in_port, eth_dst=dst)
            self.add_flow(datapath, 1, match, actions, idle_timeout=30)

        self._send_packet_out(datapath, msg, in_port, actions)

    def reply_arp(self, datapath, src_mac, src_ip, eth_dst, arp_dst_mac, arp_dst_ip, out_port):
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto
        pkt = packet.Packet()
        pkt.add_protocol(ethernet.ethernet(ethertype=ether_types.ETH_TYPE_ARP, dst=eth_dst, src=src_mac))
        pkt.add_protocol(arp.arp(
            opcode=arp.ARP_REPLY,
            src_mac=src_mac,
            src_ip=src_ip,
            dst_mac=arp_dst_mac,
            dst_ip=arp_dst_ip,
        ))
        pkt.serialize()
        actions = [parser.OFPActionOutput(out_port)]
        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=ofproto.OFP_NO_BUFFER,
            in_port=ofproto.OFPP_CONTROLLER,
            actions=actions,
            data=pkt.data,
        )
        datapath.send_msg(out)

    def reply_icmp(self, datapath, eth, ipv4_pkt, icmp_pkt, out_port):
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto
        pkt = packet.Packet()
        pkt.add_protocol(ethernet.ethernet(ethertype=ether_types.ETH_TYPE_IP, dst=eth.src, src=self.virtual_mac))
        pkt.add_protocol(ipv4.ipv4(dst=ipv4_pkt.src, src=self.virtual_ip, proto=ipv4_pkt.proto))
        pkt.add_protocol(icmp.icmp(
            type_=icmp.ICMP_ECHO_REPLY,
            code=0,
            csum=0,
            data=icmp_pkt.data,
        ))
        pkt.serialize()
        actions = [parser.OFPActionOutput(out_port)]
        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=ofproto.OFP_NO_BUFFER,
            in_port=ofproto.OFPP_CONTROLLER,
            actions=actions,
            data=pkt.data,
        )
        datapath.send_msg(out)

    def _prune_flows(self):
        now = time.time()
        stale = [k for k, v in self.active_vip_flows.items() if now - v[1] > 120.0]
        for k in stale:
            del self.active_vip_flows[k]

    def handle_tcp_forward(self, msg, datapath, in_port, eth, ipv4_pkt, tcp_pkt):
        parser = datapath.ofproto_parser
        flow_key = (ipv4_pkt.src, tcp_pkt.src_port)

        is_new_syn = bool(tcp_pkt.bits & tcp.TCP_SYN) and not bool(tcp_pkt.bits & tcp.TCP_ACK)
        existing = None if is_new_syn else self.active_vip_flows.get(flow_key)
        if existing:
            server = next((s for s in self.servers if s["ip"] == existing[0]), None)
        else:
            server = None

        if server is None:
            server = self.servers[self.server_index]
            self.server_index = (self.server_index + 1) % len(self.servers)
            self._prune_flows()
            self.active_vip_flows[flow_key] = (server["ip"], time.time())
            print(
                f"[Static RR] Client {ipv4_pkt.src}:{tcp_pkt.src_port} -> "
                f"{server['id']} ({server['ip']})",
                flush=True,
            )

        actions = [
            parser.OFPActionSetField(eth_dst=server["mac"]),
            parser.OFPActionSetField(ipv4_dst=server["ip"]),
            parser.OFPActionOutput(server["port"]),
        ]
        match = parser.OFPMatch(
            eth_type=ether_types.ETH_TYPE_IP,
            ip_proto=ipv4_pkt.proto,
            ipv4_src=ipv4_pkt.src,
            ipv4_dst=self.virtual_ip,
            tcp_src=tcp_pkt.src_port,
            tcp_dst=tcp_pkt.dst_port,
        )
        self.add_flow(datapath, 10, match, actions, idle_timeout=15, hard_timeout=60)
        self._send_packet_out(datapath, msg, in_port, actions)

    def handle_tcp_reverse(self, msg, datapath, in_port, eth, ipv4_pkt, tcp_pkt):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        dpid = datapath.id
        out_port = self.mac_to_port[dpid].get(eth.dst, ofproto.OFPP_FLOOD)

        actions = [
            parser.OFPActionSetField(eth_src=self.virtual_mac),
            parser.OFPActionSetField(ipv4_src=self.virtual_ip),
            parser.OFPActionOutput(out_port),
        ]

        if out_port != ofproto.OFPP_FLOOD:
            match = parser.OFPMatch(
                eth_type=ether_types.ETH_TYPE_IP,
                ip_proto=ipv4_pkt.proto,
                ipv4_src=ipv4_pkt.src,
                ipv4_dst=ipv4_pkt.dst,
                tcp_src=tcp_pkt.src_port,
                tcp_dst=tcp_pkt.dst_port,
            )
            self.add_flow(datapath, 10, match, actions, idle_timeout=15, hard_timeout=60)

        self._send_packet_out(datapath, msg, in_port, actions)
