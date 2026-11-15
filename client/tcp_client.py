import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import math
import socket
import statistics
import sys
import time


def _recv_all(sock):
    chunks = []
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", errors="replace")


def _parse_http_response(raw_response):
    if not raw_response:
        return 0, {}
    sep = "\r\n\r\n" if "\r\n\r\n" in raw_response else "\n\n"
    if sep in raw_response:
        header_part, body_part = raw_response.split(sep, 1)
    else:
        header_part, body_part = raw_response, ""

    status_line = header_part.splitlines()[0] if header_part.splitlines() else ""
    parts = status_line.split()
    status_code = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0

    body_json = {}
    if body_part.strip():
        try:
            body_json = json.loads(body_part)
        except json.JSONDecodeError:
            body_json = {"raw": body_part.strip()}
    return status_code, body_json


def make_request(target_ip, target_port, req_id, path="/", timeout=5.0):
    """Executes a single TCP HTTP request using low-level sockets and measures RTT."""
    start_time = time.perf_counter()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((target_ip, target_port))
        request_bytes = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {target_ip}:{target_port}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode("utf-8")
        sock.sendall(request_bytes)
        raw_resp = _recv_all(sock)
        rtt_ms = (time.perf_counter() - start_time) * 1000.0
        status_code, body = _parse_http_response(raw_resp)
        server_id = body.get("server_id", "unknown")
        server_ip = body.get("server_ip", body.get("ip", "unknown"))
        ok = 200 <= status_code < 300
        return {
            "req_id": req_id,
            "success": ok,
            "status_code": status_code,
            "server_id": server_id,
            "server_ip": server_ip,
            "rtt_ms": round(rtt_ms, 2),
            "server_cpu": body.get("cpu"),
            "error": None if ok else body.get("error", f"HTTP {status_code}"),
        }
    except Exception as exc:
        rtt_ms = (time.perf_counter() - start_time) * 1000.0
        return {
            "req_id": req_id,
            "success": False,
            "status_code": 0,
            "server_id": "none",
            "server_ip": "none",
            "rtt_ms": round(rtt_ms, 2),
            "server_cpu": None,
            "error": str(exc),
        }
    finally:
        try:
            sock.close()
        except Exception:
            pass


def compute_summary(results, total_wall_sec):
    total = len(results)
    succeeded = [r for r in results if r["success"]]
    failed = [r for r in results if not r["success"]]
    rtts = sorted(r["rtt_ms"] for r in succeeded)
    dist = dict(sorted(Counter(r["server_id"] for r in succeeded).items()))

    if rtts:
        p95_idx = min(len(rtts) - 1, int(math.ceil(0.95 * len(rtts))) - 1)
        p95_rtt = rtts[p95_idx]
        mean_rtt = statistics.mean(rtts)
        median_rtt = statistics.median(rtts)
        min_rtt = rtts[0]
        max_rtt = rtts[-1]
    else:
        p95_rtt = mean_rtt = median_rtt = min_rtt = max_rtt = 0.0

    # Jain's Fairness Index across servers that exist in distribution
    counts = list(dist.values())
    if counts and sum(counts) > 0:
        n = len(counts)
        sum_x = sum(counts)
        sum_x2 = sum(c * c for c in counts)
        jains_index = (sum_x ** 2) / (n * sum_x2) if sum_x2 > 0 else 0.0
    else:
        jains_index = 0.0

    throughput = (len(succeeded) / total_wall_sec) if total_wall_sec > 0 else 0.0
    success_rate = (len(succeeded) / float(total) * 100.0) if total > 0 else 0.0

    return {
        "total_requests": total,
        "succeeded": len(succeeded),
        "failed": len(failed),
        "success_rate_pct": round(success_rate, 2),
        "wall_time_sec": round(total_wall_sec, 3),
        "throughput_rps": round(throughput, 2),
        "min_rtt_ms": round(min_rtt, 2),
        "mean_rtt_ms": round(mean_rtt, 2),
        "median_rtt_ms": round(median_rtt, 2),
        "p95_rtt_ms": round(p95_rtt, 2),
        "max_rtt_ms": round(max_rtt, 2),
        "server_distribution": dist,
        "jains_fairness_index": round(jains_index, 4),
    }


def run_client(target_ip, num_requests, port=8080, concurrency=10, delay=0.02,
               path="/", timeout=5.0, client_id="c1", quiet=False):
    if not quiet:
        print(
            f"[*] [{client_id}] Sending {num_requests} requests "
            f"(concurrency={concurrency}) to {target_ip}:{port}{path}",
            flush=True,
        )

    results = []
    wall_start = time.perf_counter()

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as executor:
        futures = []
        for i in range(num_requests):
            futures.append(
                executor.submit(make_request, target_ip, port, i, path, timeout)
            )
            if delay > 0 and i < num_requests - 1:
                time.sleep(delay)

        for fut in as_completed(futures):
            res = fut.result()
            results.append(res)
            if not quiet:
                if res["success"]:
                    print(
                        f"[Req {res['req_id']:>3}] HTTP {res['status_code']} | "
                        f"Server: {res['server_id']} ({res['server_ip']}) | "
                        f"RTT: {res['rtt_ms']:.2f} ms",
                        flush=True,
                    )
                else:
                    print(
                        f"[Req {res['req_id']:>3}] FAILED | Error: {res['error']} | "
                        f"Elapsed: {res['rtt_ms']:.2f} ms",
                        flush=True,
                    )

    results.sort(key=lambda x: x["req_id"])
    wall_sec = time.perf_counter() - wall_start
    summary = compute_summary(results, wall_sec)
    summary["client_id"] = client_id
    summary["target"] = f"{target_ip}:{port}{path}"

    if not quiet:
        print("\n=== Client Traffic Summary ===")
        print(
            f"Success: {summary['succeeded']}/{summary['total_requests']} "
            f"({summary['success_rate_pct']:.1f}%) | "
            f"Throughput: {summary['throughput_rps']:.2f} req/s | "
            f"Duration: {summary['wall_time_sec']:.2f}s"
        )
        print(
            f"RTT (ms): min={summary['min_rtt_ms']:.2f}, "
            f"mean={summary['mean_rtt_ms']:.2f}, "
            f"median={summary['median_rtt_ms']:.2f}, "
            f"p95={summary['p95_rtt_ms']:.2f}, "
            f"max={summary['max_rtt_ms']:.2f}"
        )
        print(f"Server Distribution: {summary['server_distribution']}")
        print(f"Jain's Fairness Index: {summary['jains_fairness_index']:.4f}")

    return {"summary": summary, "requests": results}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Concurrent TCP Client & Load Generator for SDN Load Balancer"
    )
    parser.add_argument("virtual_ip", help="Load balancer Virtual IP (e.g., 10.0.0.100)")
    parser.add_argument("num_requests", type=int, help="Total number of requests to send")
    parser.add_argument("--port", type=int, default=8080, help="Target TCP port (default: 8080)")
    parser.add_argument("--concurrency", type=int, default=10,
                        help="Max concurrent worker threads (default: 10)")
    parser.add_argument("--delay", type=float, default=0.02,
                        help="Stagger delay in seconds between request submissions (default: 0.02)")
    parser.add_argument("--path", default="/",
                        help="HTTP request path, e.g., / or /heavy (default: /)")
    parser.add_argument("--timeout", type=float, default=5.0,
                        help="Per-request socket timeout in seconds (default: 5.0)")
    parser.add_argument("--client-id", default="c1", help="Client identifier for logs (default: c1)")
    parser.add_argument("--output", default=None, help="Optional JSON file path to save metrics")
    parser.add_argument("--quiet", action="store_true", help="Suppress per-request output")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    report = run_client(
        target_ip=args.virtual_ip,
        num_requests=args.num_requests,
        port=args.port,
        concurrency=args.concurrency,
        delay=args.delay,
        path=args.path,
        timeout=args.timeout,
        client_id=args.client_id,
        quiet=args.quiet,
    )
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        print(f"[*] Saved client report to {args.output}")
