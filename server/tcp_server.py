import argparse
from collections import deque
import json
import math
import os
import signal
import socket
import sys
import threading
import time
from urllib.parse import parse_qs, urlparse

try:
    import psutil
except ImportError:
    psutil = None

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8080
DEFAULT_CONTROLLER_IP = "10.0.0.254"
DEFAULT_CONTROLLER_PORT = 5555


class ServerMetrics:
    """Thread-safe container for tracking per-server load and request metrics."""

    def __init__(self, server_id, server_ip, port, base_cpu=0.0, base_delay=0.0,
                 work_ms=10.0, max_conns=50):
        self.server_id = server_id
        self.server_ip = server_ip
        self.port = port
        self.simulated_cpu = max(0.0, min(100.0, float(base_cpu)))
        self.simulated_delay = max(0.0, float(base_delay))
        self.work_ms = max(0.0, float(work_ms))
        self.max_conns = max(1, int(max_conns))

        self.lock = threading.Lock()
        self.active_connections = 0
        self.total_requests = 0
        self.failed_requests = 0
        self.recent_timestamps = deque(maxlen=500)
        self.recent_proc_ms = deque(maxlen=100)
        if psutil is not None:
            self.process = psutil.Process(os.getpid())
            # Prime psutil cpu_percent so subsequent non-blocking calls return meaningful deltas
            self.process.cpu_percent(interval=None)
            psutil.cpu_percent(interval=None)
        else:
            self.process = None

    def on_connect(self):
        with self.lock:
            self.active_connections += 1
            return self.active_connections

    def on_complete(self, proc_ms, success=True):
        now = time.time()
        with self.lock:
            self.active_connections = max(0, self.active_connections - 1)
            self.total_requests += 1
            if not success:
                self.failed_requests += 1
            self.recent_timestamps.append(now)
            self.recent_proc_ms.append(proc_ms)

    def set_stress(self, cpu=None, delay=None):
        with self.lock:
            if cpu is not None:
                self.simulated_cpu = max(0.0, min(100.0, float(cpu)))
            if delay is not None:
                self.simulated_delay = max(0.0, float(delay))

    def reset_stress(self):
        with self.lock:
            self.simulated_cpu = 0.0
            self.simulated_delay = 0.0

    def snapshot(self):
        now = time.time()
        if self.process is not None:
            try:
                proc_cpu = min(100.0, self.process.cpu_percent(interval=None))
            except Exception:
                proc_cpu = 0.0
            vm_cpu = psutil.cpu_percent(interval=None)
            mem_load = psutil.virtual_memory().percent
        else:
            proc_cpu = 0.0
            vm_cpu = 0.0
            mem_load = 0.0

        with self.lock:
            active_conns = self.active_connections
            total_reqs = self.total_requests
            failed_reqs = self.failed_requests
            sim_cpu = self.simulated_cpu
            sim_delay = self.simulated_delay

            # Requests in the last 5 seconds
            while self.recent_timestamps and (now - self.recent_timestamps[0] > 5.0):
                self.recent_timestamps.popleft()
            rps = len(self.recent_timestamps) / 5.0

            avg_proc_ms = (
                sum(self.recent_proc_ms) / len(self.recent_proc_ms)
                if self.recent_proc_ms
                else self.work_ms
            )

        # Combine real process/connection pressure with simulated overload so
        # shared-VM Mininet hosts report distinct per-server CPU load.
        conn_cpu_pressure = (active_conns / float(self.max_conns)) * 60.0
        effective_cpu = min(
            100.0,
            max(sim_cpu, proc_cpu * 0.5 + conn_cpu_pressure + vm_cpu * 0.15),
        )

        return {
            "server_id": self.server_id,
            "ip": self.server_ip,
            "port": self.port,
            "cpu": round(effective_cpu, 2),
            "vm_cpu": round(vm_cpu, 2),
            "proc_cpu": round(proc_cpu, 2),
            "simulated_cpu": round(sim_cpu, 2),
            "simulated_delay": round(sim_delay, 3),
            "mem": round(mem_load, 2),
            "active_connections": active_conns,
            "total_requests": total_reqs,
            "failed_requests": failed_reqs,
            "rps": round(rps, 2),
            "avg_proc_ms": round(avg_proc_ms, 2),
            "timestamp": now,
        }


def _burn_cpu(duration_sec):
    """Performs brief math computation to create real process CPU load."""
    end_t = time.perf_counter() + duration_sec
    acc = 0.0
    i = 1
    while time.perf_counter() < end_t:
        acc += math.sqrt(i)
        i += 1
    return acc


def _recv_http_request(conn):
    """Reads an HTTP request header from a client socket."""
    conn.settimeout(5.0)
    chunks = []
    total_bytes = 0
    while total_bytes < 8192:
        data = conn.recv(1024)
        if not data:
            break
        chunks.append(data)
        total_bytes += len(data)
        joined = b"".join(chunks)
        if b"\r\n\r\n" in joined or b"\n\n" in joined:
            return joined.decode("utf-8", errors="replace")
    return b"".join(chunks).decode("utf-8", errors="replace")


def _build_http_response(status_code, status_text, payload_dict):
    body = json.dumps(payload_dict).encode("utf-8")
    headers = (
        f"HTTP/1.1 {status_code} {status_text}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    ).encode("utf-8")
    return headers + body


def handle_client(conn, addr, metrics):
    """Handles a single incoming client TCP connection."""
    req_start = time.perf_counter()
    active_now = metrics.on_connect()
    success = True
    try:
        raw_req = _recv_http_request(conn)
        if not raw_req:
            return

        first_line = raw_req.splitlines()[0] if raw_req.splitlines() else "GET / HTTP/1.1"
        parts = first_line.split()
        raw_target = parts[1] if len(parts) >= 2 else "/"
        parsed = urlparse(raw_target)
        path = parsed.path
        query = parse_qs(parsed.query)

        # Control/health endpoints for testing and overload simulation
        if path == "/stress":
            cpu_val = float(query["cpu"][0]) if "cpu" in query else 90.0
            delay_val = float(query["delay"][0]) if "delay" in query else 0.25
            metrics.set_stress(cpu=cpu_val, delay=delay_val)
            snap = metrics.snapshot()
            resp = _build_http_response(200, "OK", {
                "status": "stressed",
                "message": f"Simulated overload enabled on {metrics.server_id}",
                **snap,
            })
            conn.sendall(resp)
            return

        if path == "/reset":
            metrics.reset_stress()
            snap = metrics.snapshot()
            resp = _build_http_response(200, "OK", {
                "status": "reset",
                "message": f"Simulated overload cleared on {metrics.server_id}",
                **snap,
            })
            conn.sendall(resp)
            return

        if path == "/health":
            snap = metrics.snapshot()
            resp = _build_http_response(200, "OK", {"status": "healthy", **snap})
            conn.sendall(resp)
            return

        if path == "/stats":
            snap = metrics.snapshot()
            resp = _build_http_response(200, "OK", {"status": "ok", **snap})
            conn.sendall(resp)
            return

        # Reject if concurrency far exceeds server capacity
        if active_now > metrics.max_conns:
            success = False
            snap = metrics.snapshot()
            resp = _build_http_response(503, "Service Unavailable", {
                "status": "overloaded",
                "error": "Max concurrent connections exceeded",
                **snap,
            })
            conn.sendall(resp)
            return

        # Simulate request processing + any configured overload delay
        with metrics.lock:
            sim_delay = metrics.simulated_delay
            base_work = metrics.work_ms / 1000.0

        if path == "/heavy":
            _burn_cpu(0.05)
            if sim_delay > 0:
                time.sleep(sim_delay)
        else:
            total_sleep = base_work + sim_delay
            if total_sleep > 0:
                time.sleep(total_sleep)

        proc_ms = (time.perf_counter() - req_start) * 1000.0
        snap = metrics.snapshot()
        resp = _build_http_response(200, "OK", {
            "status": "ok",
            "message": f"Hello from {metrics.server_id} ({metrics.server_ip})",
            "client_addr": f"{addr[0]}:{addr[1]}",
            "processing_ms": round(proc_ms, 2),
            **snap,
        })
        conn.sendall(resp)
    except Exception:
        success = False
    finally:
        proc_ms = (time.perf_counter() - req_start) * 1000.0
        metrics.on_complete(proc_ms, success=success)
        try:
            conn.close()
        except Exception:
            pass


def telemetry_reporter(metrics, controller_ip, controller_port, interval, stop_event):
    """Sends server load & health telemetry via UDP to the SDN controller."""
    udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    while not stop_event.is_set():
        try:
            payload = metrics.snapshot()
            message = json.dumps(payload).encode("utf-8")
            udp_sock.sendto(message, (controller_ip, controller_port))
        except Exception as exc:
            print(f"[{metrics.server_id} Telemetry Error] {exc}", file=sys.stderr)
        stop_event.wait(interval)
    udp_sock.close()


def tcp_listener(metrics, host, port, stop_event):
    """Listens for incoming TCP connections and spawns worker threads."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, port))
    server.listen(128)
    server.settimeout(1.0)
    print(f"[*] TCP Server {metrics.server_id} ({metrics.server_ip}) listening on {host}:{port}", flush=True)

    try:
        while not stop_event.is_set():
            try:
                conn, addr = server.accept()
            except socket.timeout:
                continue
            thread = threading.Thread(
                target=handle_client,
                args=(conn, addr, metrics),
                daemon=True,
            )
            thread.start()
    finally:
        server.close()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Multithreaded TCP Backend Server with SDN UDP Telemetry"
    )
    parser.add_argument("server_id", help="Logical server ID (e.g., srv1)")
    parser.add_argument("server_ip", help="Server IP address (e.g., 10.0.0.1)")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="TCP listen port (default: 8080)")
    parser.add_argument("--controller-ip", default=DEFAULT_CONTROLLER_IP,
                        help="Controller telemetry IP (default: 10.0.0.254)")
    parser.add_argument("--controller-port", type=int, default=DEFAULT_CONTROLLER_PORT,
                        help="Controller telemetry UDP port (default: 5555)")
    parser.add_argument("--base-cpu", type=float, default=0.0,
                        help="Initial simulated CPU %% for overload testing (default: 0.0)")
    parser.add_argument("--base-delay", type=float, default=0.0,
                        help="Initial simulated extra delay in seconds (default: 0.0)")
    parser.add_argument("--work-ms", type=float, default=10.0,
                        help="Normal request processing time in ms (default: 10.0)")
    parser.add_argument("--max-conns", type=int, default=50,
                        help="Soft max concurrent connections before HTTP 503 (default: 50)")
    parser.add_argument("--telemetry-interval", type=float, default=1.0,
                        help="UDP telemetry interval in seconds (default: 1.0)")
    return parser.parse_args()


def main():
    args = parse_args()
    stop_event = threading.Event()

    def _handle_signal(signum, frame):
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    metrics = ServerMetrics(
        server_id=args.server_id,
        server_ip=args.server_ip,
        port=args.port,
        base_cpu=args.base_cpu,
        base_delay=args.base_delay,
        work_ms=args.work_ms,
        max_conns=args.max_conns,
    )

    t_thread = threading.Thread(
        target=telemetry_reporter,
        args=(metrics, args.controller_ip, args.controller_port, args.telemetry_interval, stop_event),
        daemon=True,
    )
    t_thread.start()

    tcp_listener(metrics, args.host, args.port, stop_event)


if __name__ == "__main__":
    main()
