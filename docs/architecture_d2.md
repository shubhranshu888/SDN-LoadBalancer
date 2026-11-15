# Phase 2: Adaptive SDN Load Balancer Architecture (D2)

## 1. Objective
Extend the Phase 1 (D1) static round-robin load balancer into an **Adaptive SDN Load Balancer** that monitors both **backend server load** (via UDP socket telemetry) and **network load** (via OpenFlow 1.3 switch port/flow statistics), detects **overloaded** and **failed** servers, dynamically reroutes TCP connections around degraded or dead backends, and benchmarks performance against the static round-robin baseline.

---

## 2. End-to-End Architecture

```text
  Clients (c1, c2, c3)                         Backend TCP Servers (srv1, srv2, srv3)
  10.0.0.4 - 10.0.0.6                          10.0.0.1 - 10.0.0.3 (:8080)
         |                                                ^         |
         | TCP SYN -> VIP 10.0.0.100:8080                 |         | UDP Telemetry (every 1s)
         v                                                |         | -> 10.0.0.254:5555
  +-------------------------------------------------------+---------+--------------------+
  |                          Mininet OpenFlow 1.3 Switch (s1)                            |
  |  - Priority 100: UDP 10.0.0.254:5555 -> OFPP_CONTROLLER (in-band telemetry)          |
  |  - Priority  10: Per-5-tuple VIP DNAT/SNAT rules (idle_timeout=10s, hard_timeout=30s)|
  |  - Priority   1: Standard L2 MAC forwarding rules                                    |
  |  - Priority   0: Table-miss -> OFPP_CONTROLLER                                       |
  +------------------------------------+-------------------------------------------------+
                                       | OpenFlow 1.3 (Packet-In, FlowMod, PortStats,
                                       | FlowStats, PortStatus, OFPFC_DELETE)
                                       v
  +--------------------------------------------------------------------------------------+
  |                     Ryu Adaptive Controller (adaptive_controller.py)                 |
  |  1. Telemetry Listener   : Parses in-band UDP JSON (CPU, Mem, Conns, RPS, Proc ms)   |
  |  2. OpenFlow Monitor     : Polls PortStats & FlowStats every 2s (Mbps, Util %, Drops)|
  |  3. Health & Failure FSM : ACTIVE <-> OVERLOADED <-> DEAD (5s heartbeat + PortStatus)|
  |  4. Adaptive Selector    : Weighted composite score + proactive flow cleanup         |
  +--------------------------------------------------------------------------------------+
```

---

## 3. Socket–SDN Integration Mechanisms

### 3.1 In-Band Server Load Telemetry (`10.0.0.254:5555`)
Because Mininet hosts run inside isolated Linux network namespaces while the Ryu controller runs in the root namespace, `adaptive_controller.py` integrates application-layer UDP socket telemetry directly through OpenFlow:
1. When a backend server sends a UDP telemetry datagram to `10.0.0.254:5555`, the controller responds to its ARP request with virtual telemetry MAC `00:00:00:00:00:fe`.
2. Switch `s1` installs a priority-100 rule matching `(eth_type=0x0800, ip_proto=17, ipv4_dst=10.0.0.254, udp_dst=5555)` -> `OFPP_CONTROLLER`.
3. `_packet_in_handler` extracts the JSON telemetry payload (`cpu`, `mem`, `active_connections`, `rps`, `avg_proc_ms`) directly from the Packet-In event.

### 3.2 Network Load Monitoring via OpenFlow 1.3
Every 2 seconds, the controller's `hub.spawn` monitor loop sends:
- **`OFPPortStatsRequest`**: Computes per-port RX/TX bitrates, link utilization percentage against the configured link bandwidth (`SDN_LB_LINK_BW_MBPS`, default `10.0 Mbps`), and packet drop/error rates.
- **`OFPFlowStatsRequest`**: Counts active priority-10 VIP flows currently pinned to each backend server port.

---

## 4. Adaptive Server Selection Formula

For every healthy candidate server, `AdaptiveLBEngine.compute_composite_score()` computes a normalized load score (lower is better):

$$\text{Score} = 0.35 \cdot S_{\text{cpu}} + 0.25 \cdot S_{\text{conn}} + 0.25 \cdot S_{\text{net}} + 0.15 \cdot S_{\text{lat}} + P_{\text{drop}} + B_{\text{burst}}$$

Where:
- $S_{\text{cpu}}$: Server effective CPU utilization (`0–100%`).
- $S_{\text{conn}}$: Normalized active connection utilization (`min(100, active_conns / conn_capacity * 100)`).
- $S_{\text{net}}$: Switch port link utilization (`min(100, (rx_bps + tx_bps) / link_bw_bps * 100)`).
- $S_{\text{lat}}$: Normalized server request processing latency (`min(100, avg_proc_ms / 5.0)`).
- $P_{\text{drop}}$: Congestion penalty from port drops/errors (`min(50, drop_rate * 10)`).
- $B_{\text{burst}}$: Instantaneous burst tie-breaker (`burst_counter * 2.5`) so simultaneous SYNs arriving within the same 1-second telemetry window spread across healthy backends rather than piling onto one server.

---

## 5. Server Health State Machine (Overload & Failure Handling)

| State | Trigger Condition | Controller Action |
|---|---|---|
| **ACTIVE** | Registered, `link_up=True`, all metrics below overload thresholds | Eligible in Tier 1 candidate pool (lowest composite score wins). |
| **OVERLOADED** | `cpu >= 80%` OR `mem >= 85%` OR `active_connections >= 25` OR `net_util_pct >= 80%` OR `avg_proc_ms >= 250ms` | Excluded from selection whenever any `ACTIVE` server exists. Recovers to `ACTIVE` with hysteresis (`cpu < 70%`, `net_util_pct < 70%`, etc.). |
| **DEAD** | Telemetry heartbeat missing `> 5.0s` OR OpenFlow `OFPPortStatus` reports link down (`OFPPS_LINK_DOWN` / `OFPPR_DELETE`) | Excluded from routing; controller issues `OFPFC_DELETE` to purge forward and reverse flow rules pointing to the failed server. Automatically recovers when telemetry and link return. |

---

## 6. Flow Rule Lifecycle & VIP NAT Isolation

- Forward rules (`priority=10`, `idle_timeout=10s`, `hard_timeout=30s`) match exact 5-tuple `(ipv4_src, ipv4_dst=10.0.0.100, tcp_src, tcp_dst=8080)` and rewrite `eth_dst` and `ipv4_dst` to the selected server.
- Fresh TCP `SYN` packets always re-run adaptive selection so new connections respond immediately to changing load conditions.
- `active_vip_flows` tracks VIP-initiated connections so reverse SNAT (`10.0.0.X -> 10.0.0.100`) only applies to VIP traffic, allowing direct administrative/health requests (e.g., `/stress`, `/stats`) to function cleanly alongside load-balanced traffic.
