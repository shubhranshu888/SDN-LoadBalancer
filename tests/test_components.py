import json
import os
import socket
import sys
import threading
import types
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _install_ryu_stubs_if_missing():
    """Allows importing adaptive_controller.py in environments without Ryu installed."""
    try:
        import ryu  # noqa: F401
        return
    except ImportError:
        pass

    ryu_mod = types.ModuleType("ryu")
    base_mod = types.ModuleType("ryu.base")
    app_mgr_mod = types.ModuleType("ryu.base.app_manager")

    class _DummyRyuApp:
        def __init__(self, *args, **kwargs):
            pass

    app_mgr_mod.RyuApp = _DummyRyuApp

    ctrl_mod = types.ModuleType("ryu.controller")
    ofp_event_mod = types.ModuleType("ryu.controller.ofp_event")
    for ev_name in (
        "EventOFPSwitchFeatures",
        "EventOFPPortStatsReply",
        "EventOFPFlowStatsReply",
        "EventOFPPortStatus",
        "EventOFPPacketIn",
    ):
        setattr(ofp_event_mod, ev_name, type(ev_name, (), {}))

    handler_mod = types.ModuleType("ryu.controller.handler")
    handler_mod.CONFIG_DISPATCHER = "config"
    handler_mod.MAIN_DISPATCHER = "main"
    handler_mod.set_ev_cls = lambda *a, **kw: (lambda fn: fn)

    lib_mod = types.ModuleType("ryu.lib")
    hub_mod = types.ModuleType("ryu.lib.hub")
    hub_mod.spawn = lambda fn, *a, **kw: None
    hub_mod.sleep = lambda s: None

    pkt_pkg = types.ModuleType("ryu.lib.packet")
    for sub in ("arp", "ethernet", "ether_types", "icmp", "ipv4", "packet", "tcp", "udp"):
        setattr(pkt_pkg, sub, types.ModuleType(f"ryu.lib.packet.{sub}"))

    ofproto_pkg = types.ModuleType("ryu.ofproto")
    ofproto_v13_mod = types.ModuleType("ryu.ofproto.ofproto_v1_3")
    ofproto_v13_mod.OFP_VERSION = 4
    ofproto_v13_mod.OFPP_MAX = 0xFFFFFF00

    sys.modules.update({
        "ryu": ryu_mod,
        "ryu.base": base_mod,
        "ryu.base.app_manager": app_mgr_mod,
        "ryu.controller": ctrl_mod,
        "ryu.controller.ofp_event": ofp_event_mod,
        "ryu.controller.handler": handler_mod,
        "ryu.lib": lib_mod,
        "ryu.lib.hub": hub_mod,
        "ryu.lib.packet": pkt_pkg,
        "ryu.lib.packet.arp": pkt_pkg.arp,
        "ryu.lib.packet.ethernet": pkt_pkg.ethernet,
        "ryu.lib.packet.ether_types": pkt_pkg.ether_types,
        "ryu.lib.packet.icmp": pkt_pkg.icmp,
        "ryu.lib.packet.ipv4": pkt_pkg.ipv4,
        "ryu.lib.packet.packet": pkt_pkg.packet,
        "ryu.lib.packet.tcp": pkt_pkg.tcp,
        "ryu.lib.packet.udp": pkt_pkg.udp,
        "ryu.ofproto": ofproto_pkg,
        "ryu.ofproto.ofproto_v1_3": ofproto_v13_mod,
    })


_install_ryu_stubs_if_missing()

from client.tcp_client import make_request, run_client
from controller.adaptive_controller import AdaptiveLBEngine
from server.tcp_server import ServerMetrics, tcp_listener, telemetry_reporter


def _get_free_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class TestSocketServerAndClient(unittest.TestCase):
    def test_tcp_server_client_and_stress_endpoints(self):
        port = _get_free_port()
        udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp_sock.bind(("127.0.0.1", 0))
        tele_port = udp_sock.getsockname()[1]
        udp_sock.settimeout(3.0)

        stop_event = threading.Event()
        metrics = ServerMetrics("srv1", "127.0.0.1", port, work_ms=5.0)

        srv_thread = threading.Thread(
            target=tcp_listener,
            args=(metrics, "127.0.0.1", port, stop_event),
            daemon=True,
        )
        tele_thread = threading.Thread(
            target=telemetry_reporter,
            args=(metrics, "127.0.0.1", tele_port, 0.2, stop_event),
            daemon=True,
        )
        srv_thread.start()
        tele_thread.start()

        try:
            # Verify UDP telemetry payload arrives
            raw_tele, _ = udp_sock.recvfrom(4096)
            tele_json = json.loads(raw_tele.decode("utf-8"))
            self.assertEqual(tele_json["server_id"], "srv1")
            self.assertEqual(tele_json["ip"], "127.0.0.1")

            # Run concurrent client batch
            report = run_client(
                "127.0.0.1",
                num_requests=8,
                port=port,
                concurrency=4,
                delay=0.0,
                quiet=True,
            )
            self.assertEqual(report["summary"]["succeeded"], 8)
            self.assertEqual(report["summary"]["server_distribution"], {"srv1": 8})

            # Trigger simulated overload via /stress and verify /stats reflects it
            res_stress = make_request("127.0.0.1", port, 99, path="/stress?cpu=91&delay=0.05")
            self.assertTrue(res_stress["success"])
            self.assertGreaterEqual(metrics.snapshot()["cpu"], 91.0)

            res_reset = make_request("127.0.0.1", port, 100, path="/reset")
            self.assertTrue(res_reset["success"])
            self.assertEqual(metrics.snapshot()["simulated_cpu"], 0.0)
        finally:
            stop_event.set()
            udp_sock.close()
            srv_thread.join(timeout=2.0)


class TestAdaptiveLBEngine(unittest.TestCase):
    def _seed_telemetry(self, engine, now=100.0, cpu_map=None):
        cpu_map = cpu_map or {"10.0.0.1": 15.0, "10.0.0.2": 20.0, "10.0.0.3": 10.0}
        for idx, (ip, cpu) in enumerate(cpu_map.items(), start=1):
            engine.update_server_telemetry({
                "server_id": f"srv{idx}",
                "ip": ip,
                "cpu": cpu,
                "mem": 30.0,
                "active_connections": 1,
                "rps": 2.0,
                "avg_proc_ms": 10.0,
            }, now=now)

    def test_selects_lowest_load_server(self):
        engine = AdaptiveLBEngine(link_bw_mbps=10.0)
        self._seed_telemetry(engine, now=100.0, cpu_map={
            "10.0.0.1": 65.0,
            "10.0.0.2": 40.0,
            "10.0.0.3": 12.0,
        })
        best_ip, best_srv = engine.select_best_server(now=100.5)
        self.assertEqual(best_ip, "10.0.0.3")
        self.assertEqual(best_srv["id"], "srv3")

    def test_overload_detection_and_hysteresis_recovery(self):
        engine = AdaptiveLBEngine(cpu_overload_pct=80.0)
        self._seed_telemetry(engine, now=100.0, cpu_map={
            "10.0.0.1": 88.0,
            "10.0.0.2": 20.0,
            "10.0.0.3": 25.0,
        })
        self.assertEqual(engine.servers["10.0.0.1"]["status"], "OVERLOADED")

        # Selection should avoid OVERLOADED srv1
        for _ in range(6):
            ip, _ = engine.select_best_server(now=101.0)
            self.assertNotEqual(ip, "10.0.0.1")

        # Hysteresis: dropping to 75% stays OVERLOADED until below 70%
        engine.update_server_telemetry({"ip": "10.0.0.1", "cpu": 75.0, "mem": 30.0, "active_connections": 1, "avg_proc_ms": 10.0}, now=102.0)
        self.assertEqual(engine.servers["10.0.0.1"]["status"], "OVERLOADED")

        engine.update_server_telemetry({"ip": "10.0.0.1", "cpu": 55.0, "mem": 30.0, "active_connections": 1, "avg_proc_ms": 10.0}, now=103.0)
        self.assertEqual(engine.servers["10.0.0.1"]["status"], "ACTIVE")

    def test_network_load_monitoring_avoids_congested_port(self):
        engine = AdaptiveLBEngine(link_bw_mbps=10.0)
        self._seed_telemetry(engine, now=100.0, cpu_map={
            "10.0.0.1": 15.0,
            "10.0.0.2": 15.0,
            "10.0.0.3": 15.0,
        })
        # Baseline port stats at t=100
        for p in (1, 2, 3):
            engine.update_port_stats(1, p, 0, 0, 0, 0, 0, 0, now=100.0)
        # At t=102 (2 seconds): port 1 transferred 2,250,000 bytes => 9.0 Mbps (90% util -> OVERLOADED!)
        engine.update_port_stats(1, 1, 1_125_000, 1_125_000, 0, 0, 0, 0, now=102.0)
        engine.update_port_stats(1, 2, 50_000, 50_000, 0, 0, 0, 0, now=102.0)
        engine.update_port_stats(1, 3, 25_000, 25_000, 0, 0, 0, 0, now=102.0)

        self.assertGreaterEqual(engine.servers["10.0.0.1"]["net_util_pct"], 85.0)
        self.assertEqual(engine.servers["10.0.0.1"]["status"], "OVERLOADED")
        best_ip, _ = engine.select_best_server(now=102.1)
        self.assertEqual(best_ip, "10.0.0.3")

    def test_heartbeat_failure_and_port_down_detection(self):
        engine2 = AdaptiveLBEngine(heartbeat_timeout=5.0)
        self._seed_telemetry(engine2, now=100.0)
        engine2.update_server_telemetry({"ip": "10.0.0.1", "cpu": 15.0, "mem": 30.0}, now=104.0)
        engine2.update_server_telemetry({"ip": "10.0.0.3", "cpu": 15.0, "mem": 30.0}, now=104.0)

        # srv2 stops sending telemetry -> at t=106s (>5s) it must be marked DEAD
        newly_dead2 = engine2.check_health_timeouts(now=106.0)
        self.assertEqual(newly_dead2, [("10.0.0.2", 2)])
        self.assertEqual(engine2.servers["10.0.0.2"]["status"], "DEAD")

        # Selection never picks DEAD srv2
        for _ in range(5):
            ip, _ = engine2.select_best_server(now=106.2)
            self.assertIn(ip, ("10.0.0.1", "10.0.0.3"))

        # PortStatus link down on port 1 immediately marks srv1 DEAD
        dead_port = engine2.handle_port_status(1, link_up=False, now=106.5)
        self.assertEqual(dead_port, [("10.0.0.1", 1)])
        self.assertEqual(engine2.servers["10.0.0.1"]["status"], "DEAD")

        # Only srv3 remains healthy
        ip, _ = engine2.select_best_server(now=106.6)
        self.assertEqual(ip, "10.0.0.3")

        # Recover srv2 when telemetry resumes
        engine2.update_server_telemetry({"ip": "10.0.0.2", "cpu": 10.0, "mem": 25.0}, now=107.0)
        self.assertEqual(engine2.servers["10.0.0.2"]["status"], "ACTIVE")


if __name__ == "__main__":
    unittest.main()
