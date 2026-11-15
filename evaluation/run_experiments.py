import argparse
import json
import os
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import time

# Ensure project root is on sys.path when run as `python3 evaluation/run_experiments.py`
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from mininet.clean import cleanup
from mininet.log import setLogLevel
from topology.topology import build_topology


VIP = "10.0.0.100"
PORT = 8080


def find_ryu_manager(explicit_path=None):
    if explicit_path and os.path.exists(explicit_path):
        return explicit_path
    sibling = os.path.join(os.path.dirname(sys.executable), "ryu-manager")
    if os.path.exists(sibling):
        return sibling
    which_path = shutil.which("ryu-manager")
    if which_path:
        return which_path
    raise FileNotFoundError(
        "Could not locate 'ryu-manager'. Pass --ryu-bin /path/to/ryu-manager "
        "or run with sudo using your virtualenv python."
    )


def wait_for_port(host, port, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.5)
        try:
            sock.connect((host, port))
            sock.close()
            return True
        except Exception:
            sock.close()
            time.sleep(0.25)
    return False


def start_controller(ryu_bin, controller_script, log_path):
    env = os.environ.copy()
    env["SDN_LB_LINK_BW_MBPS"] = "10.0"
    log_fh = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [ryu_bin, controller_script],
        cwd=PROJECT_ROOT,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        env=env,
        preexec_fn=os.setsid,
    )
    if not wait_for_port("127.0.0.1", 6653, timeout=15.0):
        stop_process(proc)
        log_fh.close()
        raise RuntimeError(f"Controller {controller_script} did not open port 6653. Check {log_path}")
    return proc, log_fh


def stop_process(proc):
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=5)
    except Exception:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            pass


def start_servers(net, srv1_extra=""):
    procs = {}
    server_specs = [
        ("srv1", "10.0.0.1", srv1_extra),
        ("srv2", "10.0.0.2", ""),
        ("srv3", "10.0.0.3", ""),
    ]
    for name, ip, extra in server_specs:
        host = net.get(name)
        cmd = (
            f"{sys.executable} {PROJECT_ROOT}/server/tcp_server.py {name} {ip} "
            f"--telemetry-interval 1.0 {extra}"
        )
        procs[name] = host.popen(cmd.split(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return procs


def stop_servers(procs):
    for proc in procs.values():
        if proc and proc.poll() is None:
            proc.terminate()
    for proc in procs.values():
        if proc and proc.poll() is None:
            try:
                proc.wait(timeout=3)
            except Exception:
                proc.kill()


def aggregate_client_reports(report_paths):
    all_reqs = []
    wall_times = []
    for path in report_paths:
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        all_reqs.extend(data.get("requests", []))
        wall_times.append(data.get("summary", {}).get("wall_time_sec", 0.0))

    total = len(all_reqs)
    succeeded = [r for r in all_reqs if r.get("success")]
    rtts = sorted(r["rtt_ms"] for r in succeeded)
    dist = {}
    for r in succeeded:
        sid = r.get("server_id", "unknown")
        dist[sid] = dist.get(sid, 0) + 1

    mean_rtt = statistics.mean(rtts) if rtts else 0.0
    p95_rtt = rtts[min(len(rtts) - 1, int(0.95 * len(rtts)))] if rtts else 0.0
    max_wall = max(wall_times) if wall_times else 1.0
    throughput = len(succeeded) / max_wall if max_wall > 0 else 0.0
    success_rate = (len(succeeded) / float(total) * 100.0) if total > 0 else 0.0

    counts = [dist.get(s, 0) for s in ("srv1", "srv2", "srv3")]
    sum_c = sum(counts)
    sum_c2 = sum(c * c for c in counts)
    jains = (sum_c ** 2) / (len(counts) * sum_c2) if sum_c2 > 0 else 0.0

    return {
        "total_requests": total,
        "succeeded": len(succeeded),
        "failed": total - len(succeeded),
        "success_rate_pct": round(success_rate, 2),
        "throughput_rps": round(throughput, 2),
        "mean_rtt_ms": round(mean_rtt, 2),
        "p95_rtt_ms": round(p95_rtt, 2),
        "server_distribution": dist,
        "jains_fairness_index": round(jains, 4),
    }


def run_concurrent_clients(net, tmp_dir, tag, num_reqs_per_client=15, concurrency=5, delay=0.05):
    clients = ["c1", "c2", "c3"]
    procs = []
    report_paths = []
    for cname in clients:
        out_path = os.path.join(tmp_dir, f"{tag}_{cname}.json")
        report_paths.append(out_path)
        host = net.get(cname)
        cmd = [
            sys.executable,
            f"{PROJECT_ROOT}/client/tcp_client.py",
            VIP,
            str(num_reqs_per_client),
            "--concurrency", str(concurrency),
            "--delay", str(delay),
            "--timeout", "4.0",
            "--client-id", cname,
            "--output", out_path,
            "--quiet",
        ]
        procs.append(host.popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT))

    for proc in procs:
        proc.wait()

    return aggregate_client_reports(report_paths)


def run_single_scenario(ryu_bin, controller_rel_path, scenario_name, results_dir):
    print(f"\n>>> Running [{scenario_name}] with [{controller_rel_path}]", flush=True)
    cleanup()
    ctrl_log = os.path.join(results_dir, f"{scenario_name}_{os.path.basename(controller_rel_path)}.log")
    ctrl_proc, ctrl_log_fh = start_controller(ryu_bin, controller_rel_path, ctrl_log)

    topo_kwargs = {"bw_mbps": 10.0, "delay_ms": 1.0}
    if scenario_name == "network_congestion":
        # Introduce severe bottleneck + packet loss on srv1 link
        topo_kwargs.update({"srv1_bw": 1.0, "srv1_delay": 100.0, "srv1_loss": 8.0})

    net, c0, s1 = build_topology(**topo_kwargs)
    srv_procs = {}
    extra_bg_procs = []

    try:
        net.build()
        c0.start()
        s1.start([c0])
        time.sleep(2.0)

        srv1_extra = "--base-cpu 92 --base-delay 0.25" if scenario_name == "server_overload" else ""
        srv_procs = start_servers(net, srv1_extra=srv1_extra)

        # Allow initial telemetry & ARP warm-up
        time.sleep(3.5)

        if scenario_name == "network_congestion":
            # Generate cross-traffic UDP burst on srv1 port 1 so OpenFlow PortStats registers high utilization
            srv1_host = net.get("srv1")
            c3_host = net.get("c3")
            sink_code = (
                "import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); "
                "s.bind(('0.0.0.0',9999)); "
                "\nwhile True: s.recvfrom(4096)"
            )
            flood_code = (
                "import socket,time; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); "
                "pkt=b'x'*1200; end=time.time()+12\n"
                "while time.time()<end:\n"
                " s.sendto(pkt,('10.0.0.1',9999)); time.sleep(0.0012)"
            )
            extra_bg_procs.append(srv1_host.popen([sys.executable, "-c", sink_code]))
            extra_bg_procs.append(c3_host.popen([sys.executable, "-c", flood_code]))
            time.sleep(2.8)

        if scenario_name == "server_failure":
            # Launch client traffic while killing srv2 mid-way
            clients = ["c1", "c2"]
            report_paths = []
            c_procs = []
            for cname in clients:
                out_path = os.path.join(results_dir, f"{scenario_name}_{os.path.basename(controller_rel_path)}_{cname}.json")
                report_paths.append(out_path)
                cmd = [
                    sys.executable,
                    f"{PROJECT_ROOT}/client/tcp_client.py",
                    VIP,
                    "25",
                    "--concurrency", "3",
                    "--delay", "0.35",
                    "--timeout", "3.0",
                    "--client-id", cname,
                    "--output", out_path,
                    "--quiet",
                ]
                c_procs.append(net.get(cname).popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT))

            time.sleep(2.0)
            print("    [!] Simulating srv2 crash (SIGKILL)...", flush=True)
            srv_procs["srv2"].kill()
            for p in c_procs:
                p.wait()
            metrics = aggregate_client_reports(report_paths)
        else:
            tag = f"{scenario_name}_{os.path.basename(controller_rel_path)}"
            metrics = run_concurrent_clients(net, results_dir, tag, num_reqs_per_client=15, concurrency=5, delay=0.05)

        print(f"    Result: {metrics}", flush=True)
        return metrics
    finally:
        for p in extra_bg_procs:
            if p and p.poll() is None:
                p.kill()
        stop_servers(srv_procs)
        try:
            net.stop()
        except Exception:
            pass
        stop_process(ctrl_proc)
        ctrl_log_fh.close()
        cleanup()
        time.sleep(1.0)


def write_markdown_report(results, out_md):
    lines = [
        "# SDN Load Balancer: Static vs Adaptive Performance Comparison",
        "",
        f"Generated on: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "| Scenario | Controller | Success Rate (%) | Mean RTT (ms) | P95 RTT (ms) | Throughput (req/s) | Distribution | Jain's Fairness |",
        "|---|---|---:|---:|---:|---:|---|---:|",
    ]
    for scenario, controllers in results.items():
        for ctrl_name, m in controllers.items():
            dist_str = ", ".join(f"{k}:{v}" for k, v in sorted(m["server_distribution"].items()))
            lines.append(
                f"| {scenario} | {ctrl_name} | {m['success_rate_pct']:.2f} | "
                f"{m['mean_rtt_ms']:.2f} | {m['p95_rtt_ms']:.2f} | {m['throughput_rps']:.2f} | "
                f"{dist_str} | {m['jains_fairness_index']:.4f} |"
            )
    lines.append("")
    with open(out_md, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def main():
    parser = argparse.ArgumentParser(description="Automated Mininet Benchmark: Static vs Adaptive SDN LB")
    parser.add_argument("--ryu-bin", default=None, help="Path to ryu-manager executable")
    parser.add_argument(
        "--scenarios",
        nargs="+",
        default=["normal_load", "server_overload", "network_congestion", "server_failure"],
        choices=["normal_load", "server_overload", "network_congestion", "server_failure"],
        help="Scenarios to benchmark",
    )
    args = parser.parse_args()

    if os.geteuid() != 0:
        print("ERROR: Mininet experiments require root privileges. Run with sudo.", file=sys.stderr)
        sys.exit(1)

    setLogLevel("warning")
    ryu_bin = find_ryu_manager(args.ryu_bin)
    results_dir = os.path.join(PROJECT_ROOT, "results")
    os.makedirs(results_dir, exist_ok=True)

    controllers = [
        ("Static_RR", "controller/static_lb.py"),
        ("Adaptive_SDN", "controller/adaptive_controller.py"),
    ]

    all_results = {}
    for scenario in args.scenarios:
        all_results[scenario] = {}
        for label, ctrl_path in controllers:
            all_results[scenario][label] = run_single_scenario(ryu_bin, ctrl_path, scenario, results_dir)

    json_path = os.path.join(results_dir, "benchmark_results.json")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(all_results, fh, indent=2)

    md_path = os.path.join(results_dir, "comparison_report.md")
    write_markdown_report(all_results, md_path)

    print("\n" + "=" * 70)
    print(f" Benchmark Complete! JSON: {json_path}")
    print(f" Markdown Comparison Report: {md_path}")
    print("=" * 70)

    # Attempt optional chart generation
    plot_script = os.path.join(PROJECT_ROOT, "evaluation", "plot_results.py")
    if os.path.exists(plot_script):
        subprocess.run([sys.executable, plot_script, "--input", json_path, "--out-dir", results_dir], check=False)


if __name__ == "__main__":
    main()
