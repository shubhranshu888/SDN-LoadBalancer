# SDN-Based Adaptive TCP Load Balancer

An OpenFlow 1.3 Software-Defined Networking (SDN) TCP Load Balancer built with **Mininet**, **Ryu**, and **low-level Python TCP/UDP sockets**.  
Clients connect to a single Virtual IP (`10.0.0.100:8080`), while the SDN controller dynamically distributes TCP connections across backend servers (`srv1`, `srv2`, `srv3`) based on **server load**, **network load**, **overload detection**, and **failure recovery**—and benchmarks adaptive routing against a static round-robin baseline.

---

## Project Structure

```text
SDN-LoadBalancer/
├── controller/
│   ├── static_lb.py             # D1: Static Round-Robin OpenFlow 1.3 controller
│   └── adaptive_controller.py   # D2: Adaptive controller (server + network load, overload & failure handling)
├── server/
│   └── tcp_server.py            # Multithreaded TCP backend server + UDP telemetry reporter
├── client/
│   └── tcp_client.py            # Concurrent TCP client & latency/throughput/fairness profiler
├── topology/
│   └── topology.py              # Mininet TCLink star topology (s1, srv1-srv3, c1-c3)
├── evaluation/
│   ├── run_experiments.py       # Automated Mininet benchmark: Static RR vs Adaptive SDN across 4 scenarios
│   └── plot_results.py          # Generates PNG comparison charts from benchmark_results.json
├── tests/
│   └── test_components.py       # Unit & local socket integration tests (no Mininet/root required)
├── docs/
│   ├── architecture_d1.md       # Phase 1 (D1) system architecture
│   ├── architecture_d2.md       # Phase 2 (D2) adaptive architecture & scoring design
│   └── viva_and_demo_guide.md   # Rubric mapping, live demo walkthrough & Viva Q&A
├── requirements.txt
└── README.md
```

---

## Prerequisites (Ubuntu Linux VM)

- Ubuntu 20.04 / 22.04 LTS
- Mininet & Open vSwitch (`sudo apt install mininet openvswitch-switch`)
- Python 3.9 (recommended virtual environment for Ryu compatibility)
- Python packages (`pip install -r requirements.txt`):
  - `ryu>=4.34`
  - `eventlet==0.30.2`
  - `psutil>=5.9.0`
  - `matplotlib>=3.5.0` (optional, for PNG comparison charts)

> **Tip:** Activate your Python 3.9 virtual environment before running `ryu-manager`, or pass `--ryu-bin $(which ryu-manager)` to automated scripts under `sudo`.

---

## Key Features Implemented

1. **Multiple Concurrent TCP Servers (`server/tcp_server.py`)**:
   - Multithreaded low-level TCP sockets listening on port `8080`, returning structured JSON responses identifying which backend handled each request.
   - Built-in `/heavy`, `/stress?cpu=92&delay=0.25`, `/reset`, `/health`, and `/stats` endpoints for reproducible overload testing.
2. **Concurrent TCP Client Profiler (`client/tcp_client.py`)**:
   - Dispatches concurrent TCP connections to VIP `10.0.0.100:8080` and computes Success Rate (%), Min/Mean/Median/P95/Max RTT, Throughput (req/s), Server Distribution, and Jain's Fairness Index.
3. **In-Band Server Load Monitoring**:
   - Servers stream UDP JSON telemetry (`cpu`, `mem`, `active_connections`, `rps`, `avg_proc_ms`) to `10.0.0.254:5555`.
   - The SDN switch intercepts `10.0.0.254:5555` via a priority-100 OpenFlow rule and delivers telemetry directly to Ryu via `EventOFPPacketIn`.
4. **OpenFlow Network Load Monitoring**:
   - Ryu polls `OFPPortStatsRequest` and `OFPFlowStatsRequest` every 2 seconds to compute per-port RX/TX throughput, link utilization %, packet drops/errors, and active flows.
5. **Adaptive Composite Scoring & Overload Avoidance**:
   - Combines CPU, active connections, switch port utilization, processing latency, and drop penalties.
   - Marks saturated servers `OVERLOADED` (with hysteresis recovery) and steers traffic to healthy servers.
6. **Dual Failure Detection & Automatic Recovery**:
   - Detects both **process crashes** (5-second UDP heartbeat timeout) and **link failures** (`EventOFPPortStatus`), proactively purges stale rules via `OFPFC_DELETE`, reroutes traffic, and automatically recovers backends when they return.
7. **Static vs Adaptive Benchmarking (`evaluation/run_experiments.py`)**:
   - Compares `static_lb.py` and `adaptive_controller.py` across 4 scenarios: `normal_load`, `server_overload`, `network_congestion`, and `server_failure`.

---

## Execution Instructions

### 1. Run Unit & Socket Tests (No Mininet Required)

```bash
python3 -m unittest -v tests/test_components.py
```

### 2. Run Interactive D1 Demo (Static Round-Robin)

**Terminal 1 — Start Static Controller:**
```bash
ryu-manager controller/static_lb.py
```

**Terminal 2 — Start Mininet Topology:**
```bash
sudo python3 topology/topology.py
```

**Inside `mininet>` prompt — Start Backend Servers & Generate Traffic:**
```text
mininet> srv1 python3 server/tcp_server.py srv1 10.0.0.1 &
mininet> srv2 python3 server/tcp_server.py srv2 10.0.0.2 &
mininet> srv3 python3 server/tcp_server.py srv3 10.0.0.3 &
mininet> c1 python3 client/tcp_client.py 10.0.0.100 12 --concurrency 4
```

### 3. Run Interactive D2 Demo (Adaptive SDN Load Balancer)

Clean up any previous Mininet state first (`sudo mn -c`).

**Terminal 1 — Start Adaptive Controller:**
```bash
ryu-manager controller/adaptive_controller.py
```

**Terminal 2 — Start Mininet Topology:**
```bash
sudo python3 topology/topology.py
```

**Inside `mininet>` prompt — Start Servers & Test Normal + Challenging Scenarios:**
```text
mininet> srv1 python3 server/tcp_server.py srv1 10.0.0.1 &
mininet> srv2 python3 server/tcp_server.py srv2 10.0.0.2 &
mininet> srv3 python3 server/tcp_server.py srv3 10.0.0.3 &

# 1. Normal Load Test
mininet> c1 python3 client/tcp_client.py 10.0.0.100 15 --concurrency 5

# 2. Server Overload Test (stress srv1, then send client burst)
mininet> c1 curl -s "http://10.0.0.1:8080/stress?cpu=92&delay=0.25"
mininet> c1 python3 client/tcp_client.py 10.0.0.100 15 --concurrency 5
mininet> c1 curl -s "http://10.0.0.1:8080/reset"

# 3. Server Failure & Recovery Test
mininet> srv2 pkill -f tcp_server.py
mininet> c1 python3 client/tcp_client.py 10.0.0.100 15 --concurrency 5
mininet> srv2 python3 server/tcp_server.py srv2 10.0.0.2 &

# 4. Link Failure Test (instant OpenFlow PortStatus detection)
mininet> link s1 srv1 down
mininet> c1 python3 client/tcp_client.py 10.0.0.100 10 --concurrency 3
mininet> link s1 srv1 up
```

### 4. Run Automated Static vs Adaptive Benchmark Suite

This runs all 4 evaluation scenarios automatically for both `static_lb.py` and `adaptive_controller.py`, and generates `results/benchmark_results.json`, `results/comparison_report.md`, and `results/comparison_charts.png`:

```bash
# If using a virtualenv, preserve its python & ryu-manager paths under sudo:
sudo $(which python3) evaluation/run_experiments.py --ryu-bin $(which ryu-manager)
```
