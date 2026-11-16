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
│   └── architecture_d2.md       # Phase 2 (D2) adaptive architecture & scoring design
├── requirements.txt
└── README.md
```

---

## Prerequisites (Ubuntu Linux VM)

- Ubuntu 20.04 / 22.04 LTS
- Mininet & Open vSwitch (`sudo apt install mininet openvswitch-switch`)
- Python 3.9 / 3.10 (virtual environment for Ryu compatibility)
- Python packages (`pip install -r requirements.txt`):
  - `ryu>=4.34`
  - `eventlet==0.30.2` (Python 3.9) or `eventlet==0.33.2` (Python 3.10)
  - `psutil>=5.9.0`
  - `matplotlib>=3.5.0` (optional, for PNG comparison charts)

> **Tip:** On Python 3.10+, `ryu 4.34` + `eventlet 0.33.2` drops `ALREADY_HANDLED` from `eventlet.wsgi`. Use `python3 controller/launch_ryu.py <controller.py>` (included in this repo) to launch Ryu cleanly on both Python 3.9 and 3.10.

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

## Execution Instructions (Copy-Paste Ready)

> **Note:** On this Ubuntu VM setup, Ryu & `psutil` live in `~/ryu_env` while Mininet is installed in system `/usr/bin/python3`. Always activate `~/ryu_env` in **Terminal 1 (Controller)** and use plain system `sudo python3` in **Terminal 2 (Mininet)**. If running on a different machine, adjust `~/adaptive_loadbalancer` and the `ryu_env` path to match your local setup.

### 1. Run Unit & Socket Tests (No Mininet Required)

```bash
cd ~/adaptive_loadbalancer
source ~/ryu_env/bin/activate
python3 -m unittest -v tests/test_components.py
```

---

### 2. D1 Execution — Static Round-Robin Load Balancer

**Terminal 1 — Start Static Controller (`ryu_env`):**
```bash
cd ~/adaptive_loadbalancer
source ~/ryu_env/bin/activate
python3 controller/launch_ryu.py controller/static_lb.py
```

**Terminal 2 — Start Mininet Topology (System Python):**
```bash
cd ~/adaptive_loadbalancer
sudo mn -c
sudo python3 topology/topology.py
```

**Inside `mininet>` — Start Backend Servers & Run D1 Traffic Test:**
```bash
srv1 /home/shubhranshu-mishra/ryu_env/bin/python3 server/tcp_server.py srv1 10.0.0.1 &
srv2 /home/shubhranshu-mishra/ryu_env/bin/python3 server/tcp_server.py srv2 10.0.0.2 &
srv3 /home/shubhranshu-mishra/ryu_env/bin/python3 server/tcp_server.py srv3 10.0.0.3 &
```
*(Wait ~2 seconds, then paste:)*
```bash
c1 ping -c 2 10.0.0.100
c1 python3 client/tcp_client.py 10.0.0.100 12 --concurrency 4
```

**Cleanup after D1:**
```bash
exit
```
Then press `Ctrl+C` in **Terminal 1** to stop `static_lb.py`.

---

### 3. D2 Execution — Adaptive SDN Load Balancer & Challenging Scenarios

**Terminal 1 — Start Adaptive Controller (`ryu_env`):**
```bash
cd ~/adaptive_loadbalancer
source ~/ryu_env/bin/activate
python3 controller/launch_ryu.py controller/adaptive_controller.py
```

**Terminal 2 — Start Mininet Topology (System Python):**
```bash
cd ~/adaptive_loadbalancer
sudo mn -c
sudo python3 topology/topology.py
```

**Inside `mininet>` — Start Backend Servers:**
```bash
srv1 /home/shubhranshu-mishra/ryu_env/bin/python3 server/tcp_server.py srv1 10.0.0.1 &
srv2 /home/shubhranshu-mishra/ryu_env/bin/python3 server/tcp_server.py srv2 10.0.0.2 &
srv3 /home/shubhranshu-mishra/ryu_env/bin/python3 server/tcp_server.py srv3 10.0.0.3 &
```
*(Wait ~3 seconds until Terminal 1 prints `[REGISTERED]` for `srv1`, `srv2`, and `srv3`.)*

**Test 3A — Normal Load Distribution:**
```bash
c1 python3 client/tcp_client.py 10.0.0.100 15 --concurrency 5
```

**Test 3B — Challenging Scenario 1: Server Overload Avoidance (`srv1` stressed):**
```bash
c1 curl -s "http://10.0.0.1:8080/stress?cpu=92&delay=0.25"
```
*(Wait 2 seconds so Terminal 1 prints `[OVERLOAD DETECTED] srv1`, then paste:)*
```bash
c1 python3 client/tcp_client.py 10.0.0.100 15 --concurrency 5
c1 curl -s "http://10.0.0.1:8080/reset"
```

**Test 3C — Challenging Scenario 2: Server Crash & Recovery (`srv2` killed):**
```bash
srv2 pkill -f tcp_server.py
```
*(Wait 6 seconds so Terminal 1 prints `[FAILURE DETECTED] srv2` and `[Flow Cleanup]`, then paste:)*
```bash
c1 python3 client/tcp_client.py 10.0.0.100 15 --concurrency 5
srv2 /home/shubhranshu-mishra/ryu_env/bin/python3 server/tcp_server.py srv2 10.0.0.2 &
```

**Test 3D — Challenging Scenario 3: Instant Link Failure & Recovery (`srv1` link down):**
```bash
link s1 srv1 down
c1 python3 client/tcp_client.py 10.0.0.100 10 --concurrency 3
link s1 srv1 up
```

**Cleanup after D2:**
```bash
exit
```
Then press `Ctrl+C` in **Terminal 1** to stop `adaptive_controller.py`.

---

### 4. Automated Static vs. Adaptive Comparison Benchmark

Make sure Mininet and any running Ryu controller are stopped first, then run in a single terminal:

```bash
cd ~/adaptive_loadbalancer
sudo mn -c
sudo python3 evaluation/run_experiments.py
```

View the generated report and output artifacts:

```bash
cat results/comparison_report.md
ls -l results/
```

This runs all 4 scenarios (`normal_load`, `server_overload`, `network_congestion`, `server_failure`) for both `static_lb.py` and `adaptive_controller.py`, and writes `results/benchmark_results.json`, `results/comparison_report.md`, and `results/comparison_charts.png`.
