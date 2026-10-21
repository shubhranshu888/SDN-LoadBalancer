import threading
import json
import socket
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ether_types, arp, ipv4, tcp

class AdaptiveLoadBalancer(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(AdaptiveLoadBalancer, self).__init__(*args, **kwargs)
        self.mac_to_port = {}
        self.virtual_ip = '10.0.0.100'
        self.virtual_mac = '00:00:00:00:00:99'
        
        # Backend servers tracking load and health
        self.servers = {
            '10.0.0.1': {'mac': '00:00:00:00:00:01', 'port': 1, 'cpu': 0.0, 'mem': 0.0, 'status': 'ACTIVE', 'last_seen': time.time()},
            '10.0.0.2': {'mac': '00:00:00:00:00:02', 'port': 2, 'cpu': 0.0, 'mem': 0.0, 'status': 'ACTIVE', 'last_seen': time.time()},
            '10.0.0.3': {'mac': '00:00:00:00:00:03', 'port': 3, 'cpu': 0.0, 'mem': 0.0, 'status': 'ACTIVE', 'last_seen': time.time()}
        }
        
        # Start background UDP listener for server telemetry
        self.telemetry_thread = threading.Thread(target=self.udp_telemetry_listener, daemon=True)
        self.telemetry_thread.start()

    def udp_telemetry_listener(self):
        """Listens for telemetry updates sent by tcp_server.py scripts."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(('0.0.0.0', 5555))
        print("[*] SDN Controller UDP Telemetry Listener started on port 5555")
        
        while True:
            try:
                data, addr = sock.recvfrom(1024)
                packet_data = json.loads(data.decode('utf-8'))
                srv_ip = packet_data['ip']
                
                if srv_ip in self.servers:
                    self.servers[srv_ip]['cpu'] = packet_data['cpu']
                    self.servers[srv_ip]['mem'] = packet_data['mem']
                    self.servers[srv_ip]['status'] = 'ACTIVE'
                    self.servers[srv_ip]['last_seen'] = time.time()
                    # Uncomment to debug stats live:
                    # print(f"[Telemetry] Server {srv_ip} -> CPU: {packet_data['cpu']}% | MEM: {packet_data['mem']}%")
            except Exception as e:
                print(f"[Telemetry Error] {e}")

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 0, match, actions)

    def add_flow(self, datapath, priority, match, actions):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(datapath=datapath, priority=priority, match=match, instructions=inst)
        datapath.send_msg(mod)

    def select_best_server(self):
        """Dynamic Load Balancing Algorithm: Selects active server with the lowest CPU load."""
        current_time = time.time()
        best_server_ip = None
        min_cpu = float('inf')

        for ip, info in self.servers.items():
            # Check for server failures (if no telemetry received in last 5 seconds)
            if current_time - info['last_seen'] > 5.0:
                info['status'] = 'DEAD'
                continue

            if info['status'] == 'ACTIVE' and info['cpu'] < min_cpu:
                min_cpu = info['cpu']
                best_server_ip = ip

        # Fallback if all look dead or overloaded
        if not best_server_ip:
            active_servers = [ip for ip, info in self.servers.items() if info['status'] == 'ACTIVE']
            if active_servers:
                best_server_ip = active_servers[0]
            else:
                best_server_ip = list(self.servers.keys())[0] # ultimate fallback

        return best_server_ip, self.servers[best_server_ip]

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        in_port = msg.match['in_port']

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        
        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        arp_pkt = pkt.get_protocol(arp.arp)
        ipv4_pkt = pkt.get_protocol(ipv4.ipv4)

        # 1. Handle ARP requests for Virtual IP
        if arp_pkt and arp_pkt.dst_ip == self.virtual_ip:
            self.reply_arp(datapath, eth.src, arp_pkt.src_mac, arp_pkt.src_ip, in_port)
            return

        dpid = datapath.id
        self.mac_to_port.setdefault(dpid, {})
        self.mac_to_port[dpid][eth.src] = in_port

        # 2. Intercept TCP traffic headed to Virtual IP and pick the best server adaptively
        if ipv4_pkt and ipv4_pkt.dst == self.virtual_ip and pkt.get_protocol(tcp.tcp):
            self.handle_tcp_forward(msg, datapath, in_port, pkt, eth, ipv4_pkt)
            return
            
        # 3. Intercept Server responses and rewrite headers
        if ipv4_pkt and ipv4_pkt.src in self.servers:
            self.handle_tcp_reverse(msg, datapath, in_port, pkt, eth, ipv4_pkt)
            return

        # Normal forwarding
        dst = eth.dst
        out_port = self.mac_to_port[dpid].get(dst, ofproto.OFPP_FLOOD)
        actions = [parser.OFPActionOutput(out_port)]
        if out_port != ofproto.OFPP_FLOOD:
            match = parser.OFPMatch(in_port=in_port, eth_dst=dst)
            self.add_flow(datapath, 1, match, actions)
            
        data = msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None
        out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                    in_port=in_port, actions=actions, data=data)
        datapath.send_msg(out)

    def reply_arp(self, datapath, eth_dst, arp_dst_mac, arp_dst_ip, out_port):
        parser = datapath.ofproto_parser
        pkt = packet.Packet()
        pkt.add_protocol(ethernet.ethernet(ethertype=ether_types.ETH_TYPE_ARP, dst=eth_dst, src=self.virtual_mac))
        pkt.add_protocol(arp.arp(opcode=arp.ARP_REPLY, src_mac=self.virtual_mac, src_ip=self.virtual_ip,
                                   dst_mac=arp_dst_mac, dst_ip=arp_dst_ip))
        pkt.serialize()
        actions = [parser.OFPActionOutput(out_port)]
        out = parser.OFPPacketOut(datapath=datapath, buffer_id=datapath.ofproto.OFP_NO_BUFFER,
                                    in_port=datapath.ofproto.OFPP_PROC_OT_CONTROLLER if hasattr(datapath.ofproto, 'OFPP_PROC_OT_CONTROLLER') else datapath.ofproto.OFPP_CONTROLLER, 
                                    actions=actions, data=pkt.data)
        datapath.send_msg(out)

    def handle_tcp_forward(self, msg, datapath, in_port, pkt, eth, ipv4_pkt):
        parser = datapath.ofproto_parser
        
        # Dynamically select server based on lowest CPU load telemetry
        server_ip, server = self.select_best_server()
        print(f"[Adaptive Routing] Client {ipv4_pkt.src} routed to Server {server_ip} (CPU: {server['cpu']}%)")
        
        actions = [
            parser.OFPActionSetField(eth_dst=server['mac']),
            parser.OFPActionSetField(ipv4_dst=server_ip),
            parser.OFPActionOutput(server['port'])
        ]
        
        tcp_pkt = pkt.get_protocol(tcp.tcp)
        match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP, ip_proto=ipv4_pkt.proto,
                                ipv4_src=ipv4_pkt.src, ipv4_dst=self.virtual_ip,
                                tcp_src=tcp_pkt.src_port, tcp_dst=tcp_pkt.dst_port)
        self.add_flow(datapath, 10, match, actions)
        
        out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                    in_port=in_port, actions=actions, data=msg.data)
        datapath.send_msg(out)

    import time # Ensure time is imported in file scope if needed, handled above via global imports

    def handle_tcp_reverse(self, msg, datapath, in_port, pkt, eth, ipv4_pkt):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        dpid = datapath.id
        out_port = self.mac_to_port[dpid].get(eth.dst, ofproto.OFPP_FLOOD)
        
        actions = [
            parser.OFPActionSetField(eth_src=self.virtual_mac),
            parser.OFPActionSetField(ipv4_src=self.virtual_ip),
            parser.OFPActionOutput(out_port)
        ]
        
        tcp_pkt = pkt.get_protocol(tcp.tcp)
        match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP, ip_proto=ipv4_pkt.proto,
                                ipv4_src=ipv4_pkt.src, ipv4_dst=ipv4_pkt.dst,
                                tcp_src=tcp_pkt.src_port, tcp_dst=tcp_pkt.dst_port)
        self.add_flow(datapath, 10, match, actions)
        
        out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                    in_port=in_port, actions=actions, data=msg.data)
        datapath.send_msg(out)