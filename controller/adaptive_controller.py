import json
import os
import socket
import tempfile
import threading
import time

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.lib import hub
from ryu.lib.packet import arp, ethernet, ether_types, icmp, ipv4, packet, tcp, udp
from ryu.ofproto import ofproto_v1_3


DEFAULT_LINK_BW_MBPS = float(os.environ.get("SDN_LB_LINK_BW_MBPS", "10.0"))
STATE_FILE_PATH = os.environ.get(
    "SDN_LB_STATE_FILE",
    os.path.join(tempfile.gettempdir(), "sdn_lb_state.json"),
)


class AdaptiveLBEngine:
    """Core state machine for server/network monitoring, overload/failure detection, and adaptive selection."""

    def __init__(
        self,
        link_bw_mbps=DEFAULT_LINK_BW_MBPS,
        heartbeat_timeout=5.0,
        cpu_overload_pct=80.0,
        mem_overload_pct=85.0,
        conn_overload_count=25,
        net_overload_pct=80.0,
        lat_overload_ms=250.0,
        conn_capacity=25,
    ):
        self.lock = threading.Lock()
        self.link_bw_bps = max(1.0, float(link_bw_mbps) * 1_000_000.0)
        self.heartbeat_timeout = float(heartbeat_timeout)
        self.cpu_overload_pct = float(cpu_overload_pct)
        self.mem_overload_pct = float(mem_overload_pct)
        self.conn_overload_count = int(conn_overload_count)
        self.net_overload_pct = float(net_overload_pct)
        self.lat_overload_ms = float(lat_overload_ms)
        self.conn_capacity = max(1, int(conn_capacity))

        self.servers = {
            "10.0.0.1": self._make_server("srv1", "10.0.0.1", "00:00:00:00:00:01", 1),
            "10.0.0.2": self._make_server("srv2", "10.0.0.2", "00:00:00:00:00:02", 2),
            "10.0.0.3": self._make_server("srv3", "10.0.0.3", "00:00:00:00:00:03", 3),
        }
        self.port_to_ip = {info["port"]: ip for ip, info in self.servers.items()}
        self.prev_port_stats = {}
        self.rr_cursor = 0
        self.events = []

    @staticmethod
    def _make_server(srv_id, ip, mac, port):
        return {
            "id": srv_id,
            "ip": ip,
            "mac": mac,
            "port": port,
            "cpu": 0.0,
            "mem": 0.0,
            "active_connections": 0,
            "rps": 0.0,
            "avg_proc_ms": 0.0,
            "net_rx_bps": 0.0,
            "net_tx_bps": 0.0,
            "net_util_pct": 0.0,
            "net_drop_rate": 0.0,
            "active_flows": 0,
            "link_up": True,
            "registered": False,
            "status": "ACTIVE",
            "last_seen": 0.0,
            "assigned_requests": 0,
            "burst_counter": 0,
            "last_score": 0.0,
        }

    def _record_event(self, message):
        entry = {"ts": round(time.time(), 3), "msg": message}
        self.events.append(entry)
        if len(self.events) > 100:
            self.events.pop(0)

    def _evaluate_overload_locked(self, srv):
        """Updates ACTIVE <-> OVERLOADED status with hysteresis."""
        if not srv["link_up"]:
            srv["status"] = "DEAD"
            return

        is_overloaded = (
            srv["cpu"] >= self.cpu_overload_pct
            or srv["mem"] >= self.mem_overload_pct
            or srv["active_connections"] >= self.conn_overload_count
            or srv["net_util_pct"] >= self.net_overload_pct
            or srv["avg_proc_ms"] >= self.lat_overload_ms
        )
        is_recovered = (
            srv["cpu"] < (self.cpu_overload_pct - 10.0)
            and srv["mem"] < (self.mem_overload_pct - 10.0)
            and srv["active_connections"] < max(1, int(self.conn_overload_count * 0.8))
            and srv["net_util_pct"] < (self.net_overload_pct - 10.0)
            and srv["avg_proc_ms"] < (self.lat_overload_ms * 0.72)
        )

        prev_status = srv["status"]
        if prev_status == "DEAD":
            srv["status"] = "OVERLOADED" if is_overloaded else "ACTIVE"
            msg = (
                f"[RECOVERY] {srv['id']} ({srv['ip']}) back ONLINE -> {srv['status']} "
                f"(CPU={srv['cpu']:.1f}%, NetUtil={srv['net_util_pct']:.1f}%)"
            )
            self._record_event(msg)
            print(msg, flush=True)
        elif prev_status == "ACTIVE" and is_overloaded:
            srv["status"] = "OVERLOADED"
            msg = (
                f"[OVERLOAD DETECTED] {srv['id']} ({srv['ip']}) -> "
                f"CPU={srv['cpu']:.1f}%, Mem={srv['mem']:.1f}%, "
                f"Conns={srv['active_connections']}, NetUtil={srv['net_util_pct']:.1f}%, "
                f"ProcMs={srv['avg_proc_ms']:.1f}"
            )
            self._record_event(msg)
            print(msg, flush=True)
        elif prev_status == "OVERLOADED" and is_recovered:
            srv["status"] = "ACTIVE"
            msg = (
                f"[OVERLOAD CLEARED] {srv['id']} ({srv['ip']}) recovered to ACTIVE "
                f"(CPU={srv['cpu']:.1f}%, NetUtil={srv['net_util_pct']:.1f}%)"
            )
            self._record_event(msg)
            print(msg, flush=True)

    def update_server_telemetry(self, payload, now=None):
        """Ingests JSON telemetry from a backend TCP server."""
        now = time.time() if now is None else now
        srv_ip = payload.get("ip")
        if not srv_ip:
            return False

        with self.lock:
            srv = self.servers.get(srv_ip)
            if srv is None:
                return False

            srv["cpu"] = float(payload.get("cpu", srv["cpu"]))
            srv["mem"] = float(payload.get("mem", srv["mem"]))
            srv["active_connections"] = int(payload.get("active_connections", srv["active_connections"]))
            srv["rps"] = float(payload.get("rps", srv["rps"]))
            srv["avg_proc_ms"] = float(payload.get("avg_proc_ms", srv["avg_proc_ms"]))
            srv["last_seen"] = now
            srv["burst_counter"] = 0
            if not srv["registered"]:
                srv["registered"] = True
                msg = f"[REGISTERED] {srv['id']} ({srv['ip']}) telemetry active"
                self._record_event(msg)
                print(msg, flush=True)
            self._evaluate_overload_locked(srv)
            return True

    def update_port_stats(self, dpid, port_no, rx_bytes, tx_bytes, rx_dropped, tx_dropped,
                          rx_errors, tx_errors, now=None):
        """Updates per-port network load metrics from OpenFlow PortStatsReply."""
        now = time.time() if now is None else now
        key = (dpid, port_no)

        with self.lock:
            prev = self.prev_port_stats.get(key)
            self.prev_port_stats[key] = {
                "ts": now,
                "rx_bytes": rx_bytes,
                "tx_bytes": tx_bytes,
                "drops": rx_dropped + tx_dropped,
                "errors": rx_errors + tx_errors,
            }
            if prev is None:
                return

            dt = now - prev["ts"]
            if dt <= 0.05:
                return

            d_rx = max(0, rx_bytes - prev["rx_bytes"])
            d_tx = max(0, tx_bytes - prev["tx_bytes"])
            d_drops = max(0, (rx_dropped + tx_dropped) - prev["drops"])
            d_errors = max(0, (rx_errors + tx_errors) - prev["errors"])

            rx_bps = (d_rx * 8.0) / dt
            tx_bps = (d_tx * 8.0) / dt
            total_bps = rx_bps + tx_bps
            util_pct = min(100.0, (total_bps / self.link_bw_bps) * 100.0)
            drop_rate = (d_drops + d_errors) / dt

            srv_ip = self.port_to_ip.get(port_no)
            if srv_ip and srv_ip in self.servers:
                srv = self.servers[srv_ip]
                srv["net_rx_bps"] = round(rx_bps, 2)
                srv["net_tx_bps"] = round(tx_bps, 2)
                srv["net_util_pct"] = round(util_pct, 2)
                srv["net_drop_rate"] = round(drop_rate, 3)
                if srv["registered"] and srv["status"] != "DEAD":
                    self._evaluate_overload_locked(srv)

    def update_flow_counts(self, port_flow_counts):
        """Updates active VIP flow counts per server port from OpenFlow FlowStatsReply."""
        with self.lock:
            for port_no, srv_ip in self.port_to_ip.items():
                self.servers[srv_ip]["active_flows"] = int(port_flow_counts.get(port_no, 0))

    def handle_port_status(self, port_no, link_up, now=None):
        """Handles OpenFlow PortStatus changes for instant link/server failure detection."""
        now = time.time() if now is None else now
        newly_dead = []
        with self.lock:
            srv_ip = self.port_to_ip.get(port_no)
            if not srv_ip or srv_ip not in self.servers:
                return newly_dead
            srv = self.servers[srv_ip]
            srv["link_up"] = bool(link_up)
            if not link_up:
                if srv["status"] != "DEAD":
                    srv["status"] = "DEAD"
                    msg = f"[LINK FAILURE] Port {port_no} ({srv['id']} / {srv_ip}) went DOWN -> DEAD"
                    self._record_event(msg)
                    print(msg, flush=True)
                    newly_dead.append((srv_ip, port_no))
            else:
                if srv["registered"] and (now - srv["last_seen"] <= self.heartbeat_timeout):
                    self._evaluate_overload_locked(srv)
                else:
                    msg = f"[LINK UP] Port {port_no} ({srv['id']} / {srv_ip}) restored; awaiting telemetry"
                    self._record_event(msg)
                    print(msg, flush=True)
        return newly_dead

    def check_health_timeouts(self, now=None):
        """Marks registered servers DEAD when heartbeat telemetry stops; returns newly dead list."""
        now = time.time() if now is None else now
        newly_dead = []
        with self.lock:
            for srv_ip, srv in self.servers.items():
                if not srv["registered"]:
                    continue
                if srv["status"] != "DEAD" and (now - srv["last_seen"] > self.heartbeat_timeout):
                    srv["status"] = "DEAD"
                    msg = (
                        f"[FAILURE DETECTED] {srv['id']} ({srv_ip}) heartbeat timeout "
                        f"(>{self.heartbeat_timeout:.1f}s) -> marked DEAD"
                    )
                    self._record_event(msg)
                    print(msg, flush=True)
                    newly_dead.append((srv_ip, srv["port"]))
        return newly_dead

    def compute_composite_score(self, srv):
        """Computes composite server + network load score (lower is better)."""
        cpu_score = float(srv["cpu"])
        effective_conns = srv["active_connections"] + srv["burst_counter"]
        conn_score = min(100.0, (effective_conns / float(self.conn_capacity)) * 100.0)
        net_score = min(100.0, float(srv["net_util_pct"]))
        lat_score = min(100.0, float(srv["avg_proc_ms"]) / 5.0)
        drop_penalty = min(50.0, float(srv["net_drop_rate"]) * 10.0)
        # Small burst tie-breaker distributes simultaneous SYNs before next telemetry tick
        burst_tie = srv["burst_counter"] * 2.5
        score = (
            0.35 * cpu_score
            + 0.25 * conn_score
            + 0.25 * net_score
            + 0.15 * lat_score
            + drop_penalty
            + burst_tie
        )
        return round(score, 3)

    def select_best_server(self, now=None):
        """Selects optimal backend server combining server load, network load, and health."""
        now = time.time() if now is None else now
        with self.lock:
            # Refresh heartbeat timeout status inline before picking
            for srv_ip, srv in self.servers.items():
                if srv["registered"] and srv["status"] != "DEAD":
                    if now - srv["last_seen"] > self.heartbeat_timeout:
                        srv["status"] = "DEAD"
                        msg = (
                            f"[FAILURE DETECTED] {srv['id']} ({srv_ip}) heartbeat timeout "
                            f"-> marked DEAD"
                        )
                        self._record_event(msg)
                        print(msg, flush=True)

            # Update scores for all non-dead servers
            for srv in self.servers.values():
                srv["last_score"] = self.compute_composite_score(srv)

            # Tier 1: Registered ACTIVE servers
            candidates = [
                (ip, s) for ip, s in self.servers.items()
                if s["registered"] and s["status"] == "ACTIVE" and s["link_up"]
            ]

            # Tier 2: Registered OVERLOADED servers (when all healthy servers are saturated)
            if not candidates:
                candidates = [
                    (ip, s) for ip, s in self.servers.items()
                    if s["registered"] and s["status"] == "OVERLOADED" and s["link_up"]
                ]

            # Tier 3: Unregistered servers with link up (startup fallback before telemetry arrives)
            if not candidates:
                unreg = [
                    (ip, s) for ip, s in self.servers.items()
                    if not s["registered"] and s["link_up"] and s["status"] != "DEAD"
                ]
                if unreg:
                    ip, srv = unreg[self.rr_cursor % len(unreg)]
                    self.rr_cursor += 1
                    srv["assigned_requests"] += 1
                    srv["burst_counter"] += 1
                    return ip, dict(srv)

            # Tier 4: Absolute fallback if every server appears dead
            if not candidates:
                all_srvs = list(self.servers.items())
                ip, srv = all_srvs[self.rr_cursor % len(all_srvs)]
                self.rr_cursor += 1
                srv["assigned_requests"] += 1
                return ip, dict(srv)

            # Choose minimum composite score (tie-break by fewest assigned requests)
            best_ip, best_srv = min(
                candidates,
                key=lambda item: (item[1]["last_score"], item[1]["assigned_requests"]),
            )
            best_srv["assigned_requests"] += 1
            best_srv["burst_counter"] += 1
            best_srv["last_score"] = self.compute_composite_score(best_srv)
            return best_ip, dict(best_srv)

    def snapshot(self):
        with self.lock:
            return {
                "timestamp": round(time.time(), 3),
                "link_bw_mbps": self.link_bw_bps / 1_000_000.0,
                "servers": {
                    ip: dict(info) for ip, info in self.servers.items()
                },
                "recent_events": list(self.events[-20:]),
            }


class AdaptiveLoadBalancer(app_manager.RyuApp):
    """D2 Adaptive SDN Load Balancer using Server + Network Load Telemetry (OpenFlow 1.3)."""

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(AdaptiveLoadBalancer, self).__init__(*args, **kwargs)
        self.mac_to_port = {}
        self.datapaths = {}
        self.virtual_ip = "10.0.0.100"
        self.virtual_mac = "00:00:00:00:00:99"
        self.virtual_port = 8080
        self.telemetry_ip = "10.0.0.254"
        self.telemetry_mac = "00:00:00:00:00:fe"
        self.telemetry_port = 5555

        self.engine = AdaptiveLBEngine()
        # (client_ip, client_port) -> (server_ip, timestamp)
        self.active_vip_flows = {}

        # Optional out-of-band UDP socket listener (in addition to OpenFlow in-band telemetry)
        self.telemetry_thread = threading.Thread(target=self.udp_telemetry_listener, daemon=True)
        self.telemetry_thread.start()

        # Ryu greenlet for periodic OpenFlow port/flow stats & failure monitoring
        self.monitor_thread = hub.spawn(self._monitor_loop)

    def udp_telemetry_listener(self):
        """Listens on host UDP 5555 as a fallback alongside in-band SDN telemetry."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", self.telemetry_port))
        except OSError as exc:
            self.logger.warning("[AdaptiveLB] Fallback UDP socket bind skipped: %s", exc)
            sock.close()
            return

        print(f"[*] SDN Controller UDP Telemetry Listener active on 0.0.0.0:{self.telemetry_port}", flush=True)
        while True:
            try:
                data, _ = sock.recvfrom(4096)
                payload = json.loads(data.decode("utf-8"))
                self.engine.update_server_telemetry(payload)
            except Exception as exc:
                print(f"[Telemetry Socket Error] {exc}", flush=True)

    def _monitor_loop(self):
        """Periodically polls OpenFlow stats, checks server heartbeats, and saves state."""
        while True:
            hub.sleep(2.0)
            newly_dead = self.engine.check_health_timeouts()
            for srv_ip, srv_port in newly_dead:
                for dp in list(self.datapaths.values()):
                    self._remove_server_flows(dp, srv_ip, srv_port)

            for dp in list(self.datapaths.values()):
                self._request_stats(dp)

            self._prune_flows()
            self._write_state_snapshot()

    def _request_stats(self, datapath):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        datapath.send_msg(parser.OFPPortStatsRequest(datapath, 0, ofproto.OFPP_ANY))
        datapath.send_msg(parser.OFPFlowStatsRequest(datapath))

    def _write_state_snapshot(self):
        try:
            snap = self.engine.snapshot()
            with open(STATE_FILE_PATH, "w", encoding="utf-8") as fh:
                json.dump(snap, fh, indent=2)
        except Exception:
            pass

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        self.datapaths[datapath.id] = datapath

        # 1. Table-miss rule
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 0, match, actions)

        # 2. In-band SDN telemetry interception rule (UDP 10.0.0.254:5555 -> Controller)
        tele_match = parser.OFPMatch(
            eth_type=ether_types.ETH_TYPE_IP,
            ip_proto=17,
            ipv4_dst=self.telemetry_ip,
            udp_dst=self.telemetry_port,
        )
        self.add_flow(datapath, 100, tele_match, actions)
        print(f"[AdaptiveLB] Switch {datapath.id} connected; telemetry & table-miss rules installed.", flush=True)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats_reply_handler(self, ev):
        dpid = ev.msg.datapath.id
        now = time.time()
        for stat in ev.msg.body:
            if stat.port_no > ofproto_v1_3.OFPP_MAX:
                continue
            self.engine.update_port_stats(
                dpid=dpid,
                port_no=stat.port_no,
                rx_bytes=stat.rx_bytes,
                tx_bytes=stat.tx_bytes,
                rx_dropped=stat.rx_dropped,
                tx_dropped=stat.tx_dropped,
                rx_errors=stat.rx_errors,
                tx_errors=stat.tx_errors,
                now=now,
            )

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def _flow_stats_reply_handler(self, ev):
        port_counts = {}
        for stat in ev.msg.body:
            if stat.priority != 10:
                continue
            for inst in stat.instructions:
                if hasattr(inst, "actions"):
                    for action in inst.actions:
                        if hasattr(action, "port") and action.port in self.engine.port_to_ip:
                            port_counts[action.port] = port_counts.get(action.port, 0) + 1
        self.engine.update_flow_counts(port_counts)

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def _port_status_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        desc = msg.desc
        port_no = desc.port_no

        if msg.reason == ofproto.OFPPR_DELETE:
            link_up = False
        elif msg.reason == ofproto.OFPPR_ADD:
            link_up = not bool(desc.state & ofproto.OFPPS_LINK_DOWN)
        else:
            link_up = not bool(desc.state & ofproto.OFPPS_LINK_DOWN)

        newly_dead = self.engine.handle_port_status(port_no, link_up)
        for srv_ip, srv_port in newly_dead:
            self._remove_server_flows(datapath, srv_ip, srv_port)

    def add_flow(self, datapath, priority, match, actions, idle_timeout=0, hard_timeout=0):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(
            datapath=datapath,
            priority=priority,
            idle_timeout=idle_timeout,
            hard_timeout=hard_timeout,
            match=match,
            instructions=inst,
        )
        datapath.send_msg(mod)

    def _remove_server_flows(self, datapath, server_ip, server_port):
        """Proactively removes forward and reverse OpenFlow rules for a failed server."""
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        # Remove forward flows outputting to server_port
        mod_fwd = parser.OFPFlowMod(
            datapath=datapath,
            command=ofproto.OFPFC_DELETE,
            out_port=server_port,
            out_group=ofproto.OFPG_ANY,
            table_id=ofproto.OFPTT_ALL,
            match=parser.OFPMatch(),
        )
        datapath.send_msg(mod_fwd)

        # Remove reverse flows sourced from server_ip
        mod_rev = parser.OFPFlowMod(
            datapath=datapath,
            command=ofproto.OFPFC_DELETE,
            out_port=ofproto.OFPP_ANY,
            out_group=ofproto.OFPG_ANY,
            table_id=ofproto.OFPTT_ALL,
            match=parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP, ipv4_src=server_ip),
        )
        datapath.send_msg(mod_rev)
        print(f"[Flow Cleanup] Removed OpenFlow rules for dead server {server_ip} (port {server_port})", flush=True)

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

        # In-band SDN Telemetry ingestion (UDP 10.0.0.254:5555)
        if ipv4_pkt and udp_pkt and ipv4_pkt.dst == self.telemetry_ip and udp_pkt.dst_port == self.telemetry_port:
            self._handle_inband_telemetry(pkt)
            return

        # ICMP Echo Reply for VIP
        if ipv4_pkt and icmp_pkt and ipv4_pkt.dst == self.virtual_ip and icmp_pkt.type == icmp.ICMP_ECHO_REQUEST:
            self.reply_icmp(datapath, eth, ipv4_pkt, icmp_pkt, in_port)
            return

        # Forward TCP traffic destined to VIP:8080 adaptively
        if ipv4_pkt and tcp_pkt and ipv4_pkt.dst == self.virtual_ip and tcp_pkt.dst_port == self.virtual_port:
            self.handle_tcp_forward(msg, datapath, in_port, eth, ipv4_pkt, tcp_pkt)
            return

        # Reverse NAT only for active VIP TCP flows returning from backend servers
        if ipv4_pkt and tcp_pkt and ipv4_pkt.src in self.engine.servers and tcp_pkt.src_port == self.virtual_port:
            flow_key = (ipv4_pkt.dst, tcp_pkt.dst_port)
            flow_info = self.active_vip_flows.get(flow_key)
            if flow_info and flow_info[0] == ipv4_pkt.src:
                self.handle_tcp_reverse(msg, datapath, in_port, eth, ipv4_pkt, tcp_pkt)
                return

        # Standard L2 forwarding
        dst = eth.dst
        out_port = self.mac_to_port[dpid].get(dst, ofproto.OFPP_FLOOD)
        actions = [parser.OFPActionOutput(out_port)]
        if out_port != ofproto.OFPP_FLOOD:
            match = parser.OFPMatch(in_port=in_port, eth_dst=dst)
            self.add_flow(datapath, 1, match, actions, idle_timeout=30)

        self._send_packet_out(datapath, msg, in_port, actions)

    def _handle_inband_telemetry(self, pkt):
        if not pkt.protocols:
            return
        raw_payload = pkt.protocols[-1]
        if not isinstance(raw_payload, (bytes, bytearray)):
            return
        try:
            payload = json.loads(raw_payload.decode("utf-8"))
            self.engine.update_server_telemetry(payload)
        except Exception as exc:
            self.logger.warning("[In-Band Telemetry Parse Error] %s", exc)

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

        # Keep existing TCP connection pinned unless it's a fresh SYN or its server became DEAD
        server_ip = None
        server = None
        existing = None if is_new_syn else self.active_vip_flows.get(flow_key)
        if existing:
            cand_ip = existing[0]
            cand_srv = self.engine.servers.get(cand_ip)
            if cand_srv and cand_srv["status"] != "DEAD" and cand_srv["link_up"]:
                server_ip = cand_ip
                server = cand_srv

        if server is None:
            server_ip, server = self.engine.select_best_server()
            self.active_vip_flows[flow_key] = (server_ip, time.time())
            print(
                f"[Adaptive Routing] {ipv4_pkt.src}:{tcp_pkt.src_port} -> "
                f"{server['id']} ({server_ip}) | Status={server['status']} "
                f"Score={server['last_score']:.2f} CPU={server['cpu']:.1f}% "
                f"Conns={server['active_connections']} NetUtil={server['net_util_pct']:.1f}%",
                flush=True,
            )

        actions = [
            parser.OFPActionSetField(eth_dst=server["mac"]),
            parser.OFPActionSetField(ipv4_dst=server_ip),
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
        self.add_flow(datapath, 10, match, actions, idle_timeout=10, hard_timeout=30)
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
            self.add_flow(datapath, 10, match, actions, idle_timeout=10, hard_timeout=30)

        self._send_packet_out(datapath, msg, in_port, actions)
