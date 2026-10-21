# Phase 1 Architecture (D1)

## 1. Objective
To implement a static, SDN-based round-robin load balancer using Mininet, Ryu, and low-level multithreaded TCP sockets.

## 2. System Architecture
* **Data Plane:** 1 OpenFlow switch connected to 3 Client hosts and 3 Backend TCP Server hosts.
* **Control Plane:** Ryu SDN Controller (`static_lb.py`) managing OpenFlow 1.3 rules.

## 3. Communication Flow
1. TCP Clients generate concurrent traffic targeting a Virtual IP (`10.0.0.100:8080`).
2. The OpenFlow switch intercepts the `SYN` packets and forwards them to the Ryu Controller via Packet-In events.
3. The controller calculates the next available server using a round-robin index.
4. The controller installs flow rules on the switch to rewrite the Destination IP/MAC to the selected backend server.
5. It also installs reverse flow rules to rewrite the server's Source IP/MAC back to the Virtual IP before delivering the response to the client.
