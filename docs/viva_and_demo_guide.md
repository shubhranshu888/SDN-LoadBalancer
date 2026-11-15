# Final Demo, Rubric Mapping & Viva Preparation Guide

## 1. Evaluation Rubric & Question 20 Traceability

| Requirement / Rubric Item | Implementation File(s) | Key Mechanism |
|---|---|---|
| **Implement multiple TCP servers** | `server/tcp_server.py` | Multithreaded TCP socket servers (`srv1`, `srv2`, `srv3`) on port `8080` returning JSON responses with server ID, IP, load, and latency. |
| **Generate concurrent client requests** | `client/tcp_client.py` | Low-level TCP sockets with `ThreadPoolExecutor`, measuring per-request RTT, throughput, P95 latency, server distribution, and Jain's Fairness Index. |
| **Monitor server load** | `server/tcp_server.py`, `controller/adaptive_controller.py` | Periodic UDP JSON telemetry (`cpu`, `mem`, `active_connections`, `rps`, `avg_proc_ms`) sent to `10.0.0.254:5555` and intercepted in-band via OpenFlow Packet-In. |
| **Monitor network load** | `controller/adaptive_controller.py` | Periodic OpenFlow `OFPPortStatsRequest` & `OFPFlowStatsRequest` polling calculating per-port RX/TX throughput, link utilization %, packet drops, and active flows. |
| **Distribute flows using SDN** | `controller/static_lb.py`, `controller/adaptive_controller.py` | OpenFlow 1.3 DNAT/SNAT flow rules (`idle_timeout=10s`, `hard_timeout=30s`) translating VIP `10.0.0.100:8080` to backend servers. |
| **Detect overloaded servers** | `controller/adaptive_controller.py` (`AdaptiveLBEngine._evaluate_overload_locked`) | Threshold + hysteresis detection across CPU, memory, active connections, latency, and port utilization (`ACTIVE` <-> `OVERLOADED`). |
| **Detect server failures** | `controller/adaptive_controller.py` (`check_health_timeouts`, `_port_status_handler`, `_remove_server_flows`) | Dual detection via 5s UDP heartbeat timeout + OpenFlow `EventOFPPortStatus` link-down events, plus proactive `OFPFC_DELETE` flow cleanup and automatic recovery. |
| **Compare static & adaptive LB** | `evaluation/run_experiments.py`, `evaluation/plot_results.py` | Automated Mininet benchmark running `static_lb.py` vs `adaptive_controller.py` across 4 scenarios and exporting JSON, Markdown, and PNG charts. |

---

## 2. Reproducible Live Demo Scenarios

### Scenario A: Baseline Normal Load (D1 vs D2)
1. Start controller (`static_lb.py` or `adaptive_controller.py`) and Mininet (`topology.py`).
2. Start `srv1`, `srv2`, `srv3`.
3. Run `c1 python3 client/tcp_client.py 10.0.0.100 18 --concurrency 6`.
4. **Observation:** Both controllers distribute requests cleanly across all 3 servers.

### Scenario B: Server Overload Avoidance (D2 Challenging Scenario 1)
1. Start `adaptive_controller.py`, Mininet, and all 3 servers.
2. Stress `srv1` dynamically from `c1`:
   `mininet> c1 curl -s "http://10.0.0.1:8080/stress?cpu=92&delay=0.25"`
3. Wait 2 seconds; controller terminal prints `[OVERLOAD DETECTED] srv1 (10.0.0.1)`.
4. Send concurrent requests:
   `mininet> c1 python3 client/tcp_client.py 10.0.0.100 20 --concurrency 5`
5. **Observation:** Adaptive controller routes 100% of new connections to `srv2` and `srv3` (keeping RTT ~12–20 ms), whereas `static_lb.py` blindly sends ~1/3 of requests to `srv1` (>250 ms RTT).
6. Reset `srv1`: `mininet> c1 curl -s http://10.0.0.1:8080/reset` -> controller logs `[OVERLOAD CLEARED]`.

### Scenario C: Server Crash & Automatic Recovery (D2 Challenging Scenario 2)
1. Start traffic from `c1`:
   `mininet> c1 python3 client/tcp_client.py 10.0.0.100 30 --concurrency 3 --delay 0.3 &`
2. Kill `srv2` mid-run:
   `mininet> srv2 pkill -f tcp_server.py`
3. **Observation:** Within ~5 seconds, controller logs `[FAILURE DETECTED] srv2 (10.0.0.2) heartbeat timeout -> marked DEAD` and `[Flow Cleanup] Removed OpenFlow rules for dead server 10.0.0.2`. All new flows route to `srv1` and `srv3`.
4. Instant link-down test:
   `mininet> link s1 srv1 down`
   **Observation:** `EventOFPPortStatus` immediately prints `[LINK FAILURE] Port 1 (srv1 / 10.0.0.1) went DOWN -> DEAD` with zero waiting!
5. Restore link/server:
   `mininet> link s1 srv1 up`
   `mininet> srv2 python3 server/tcp_server.py srv2 10.0.0.2 &`
   **Observation:** Controller logs `[RECOVERY] ... back ONLINE -> ACTIVE`.

---

## 3. Common Viva Questions & Answers

**Q1: Why does the server send UDP telemetry to `10.0.0.254` instead of `127.0.0.1`?**
In Mininet, each virtual host (`srv1`, `srv2`, `srv3`) runs in its own isolated Linux network namespace, so `127.0.0.1` inside `srv1` refers to `srv1`'s own loopback interface, not the Ryu controller in the root namespace. Sending UDP packets to `10.0.0.254:5555` lets the OpenFlow switch intercept them via a priority-100 rule and forward them in-band to the Ryu controller via `EventOFPPacketIn`.

**Q2: How do you measure distinct server CPU loads when Mininet hosts share one VM?**
`ServerMetrics` combines real per-process CPU usage (`psutil.Process().cpu_percent()`), active connection pressure (`active_connections / max_conns`), and optional simulated stress (`--base-cpu` / `/stress`) so each backend reports distinct, realistic load metrics while still reacting to genuine concurrent traffic.

**Q3: How is network load measured separately from server CPU load?**
The controller polls OpenFlow `OFPPortStatsRequest` every 2 seconds and computes `(delta_rx_bytes + delta_tx_bytes) * 8 / dt` divided by link bandwidth (`10 Mbps`), plus packet drops/errors. If a server's switch port is congested (`>= 80%` utilization), it is penalized in the composite score and marked `OVERLOADED` even if the server's CPU is idle.

**Q4: Why do OpenFlow rules have `idle_timeout=10` and `hard_timeout=30`?**
Without timeouts, stale 5-tuple rules remain in the switch forever, preventing subsequent requests on reused ports from being re-evaluated by the adaptive algorithm and leaving dead rules behind. Short timeouts keep established TCP connections pinned while allowing new connections to adapt to current load.

**Q5: Why is `active_vip_flows` checked in `handle_tcp_reverse`?**
It ensures the controller only performs source-IP/MAC NAT (`10.0.0.X -> 10.0.0.100`) for connections that originally arrived through the VIP, so direct connections to a server's real IP (such as `/stress`, `/reset`, or background cross-traffic) are not accidentally rewritten.
