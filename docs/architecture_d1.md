# Phase 1: Architecture and System Design (D1)

## 1. Overview and Objective

The aim of this project is to build a software-defined network application using Python socket programming, Mininet, and the Ryu SDN controller. The main focus is to develop a load balancer that distributes incoming client traffic across multiple backend servers.

Instead of connecting directly to individual servers, clients will send their requests to a single Virtual IP (VIP). The Ryu controller will decide which backend server should handle each new connection.

For D1, the focus is on setting up the network, establishing TCP communication between clients and servers, and implementing the initial forwarding rules.

## 2. System Architecture

The network is set up in Mininet using OpenFlow 1.3. It consists of one OpenFlow switch, three client hosts, and three backend servers.

### 2.1 Data Plane

The data plane handles packet forwarding between the clients and backend servers.

- **Switch (`s1`):** Connects all the hosts and forwards packets based on the flow rules installed by the controller.
- **Clients (`c1`, `c2`, `c3`):** Generate TCP requests to the load balancer's Virtual IP.
- **Backend Servers (`srv1`, `srv2`, `srv3`):** Run multithreaded Python TCP servers to handle incoming client connections.

### 2.2 Control Plane

The control plane is managed by the Ryu SDN controller, which communicates with the switch and decides how traffic should be forwarded.

- **`static_lb.py`:** Handles the initial load-balancing logic. It processes incoming packets, selects a backend server using round-robin scheduling, and installs the required flow rules.
- **`adaptive_controller.py`:** Intended for a later phase, where server load and health information will be used to make better backend selection decisions.

### 2.3 Virtual IP Configuration

The load balancer uses a single Virtual IP so that clients do not need to know the addresses of the individual backend servers.

| Parameter | Value |
|---|---|
| Virtual IP | `10.0.0.100` |
| Virtual Port | `8080` |
| Virtual MAC | `00:00:00:00:00:99` |
| OpenFlow Version | `1.3` |
| D1 Algorithm | Round-robin |
| Planned D2 Algorithm | Load-aware scheduling |

## 3. Communication Flow

The communication process starts when a client tries to connect to the Virtual IP. The controller then selects a backend server and sets up the forwarding rules needed for communication.

### 3.1 ARP Resolution

Before sending packets to the Virtual IP, a client needs to know its MAC address.

1. The client sends an ARP request for `10.0.0.100`.
2. The switch forwards the request to the Ryu controller through a Packet-In event.
3. The controller replies with the virtual MAC address `00:00:00:00:00:99`.
4. The client can now send packets to the load balancer using this MAC address.

### 3.2 TCP Connection Handling

When a client starts a TCP connection, the following steps take place:

1. The client sends a TCP SYN packet to `10.0.0.100:8080`.
2. The switch checks whether a matching flow rule already exists.
3. If there is no matching rule, the packet is sent to the Ryu controller.
4. The controller processes the packet and selects a backend server.

This allows the controller to handle new connections and decide where they should be forwarded.

### 3.3 Backend Server Selection

For D1, the load balancer uses a round-robin algorithm to distribute connections among the three backend servers.

The controller maintains an index that keeps track of the next server to be selected. Requests are distributed in the following order:

`srv1 → srv2 → srv3 → srv1 → ...`

Once a server is selected, the controller installs the necessary flow rules on the switch. These rules allow subsequent packets belonging to the connection to be forwarded to the same backend server.

### 3.4 Forwarding Rules

After selecting a server, the controller installs flow rules to direct the client's traffic to that server.

The rules handle the following operations:

- Change the destination IP address from the VIP to the selected server's IP address.
- Change the destination MAC address to the selected server's MAC address.
- Forward the packet through the appropriate switch port.
- Keep subsequent packets of the connection associated with the same backend server.

This allows the switch to handle established traffic without sending every packet to the controller.

### 3.5 Reverse Packet Translation

The response from the backend server must appear to come from the Virtual IP, rather than from the server's actual IP address.

To achieve this:

1. The backend server sends a response to the client.
2. The switch matches the response against the reverse flow rule.
3. The source IP address is changed to `10.0.0.100`.
4. The source MAC address is changed to the virtual MAC address where required by the forwarding design.
5. The switch forwards the response to the client.

This way, the client continues communicating with the Virtual IP without needing to know which backend server handled the request.

The forward and reverse rules must be configured correctly, including the appropriate output ports and address translations, to ensure that the TCP connection works properly.

## 4. Overall Packet Flow

The following diagram summarizes how a client request travels through the network.

```text
Client (c1 / c2 / c3)
        |
        | ARP Request for VIP
        v
OpenFlow Switch (s1)
        |
        | Packet-In
        v
Ryu Controller
        |
        | ARP Reply with Virtual MAC
        v
Client Resolves VIP
        |
        | TCP SYN to 10.0.0.100:8080
        v
OpenFlow Switch (s1)
        |
        | Packet-In if no matching rule
        v
Ryu Controller (static_lb.py)
        |
        | Select Backend using Round-Robin
        | Install Forward and Reverse Rules
        v
Selected Backend Server
(srv1 / srv2 / srv3)
        |
        | TCP Response
        v
OpenFlow Switch (s1)
        |
        | Reverse Address Translation
        v
Client Receives Response from VIP
```

## 5. Load-Balancing Algorithm

### 5.1 D1: Round-Robin

Round-robin is used as the initial load-balancing strategy because it is simple to implement and makes it easy to test whether traffic is being distributed correctly.

Each new connection is assigned to the next server in the list. After `srv3` is selected, the controller starts again from `srv1`.

For example:

- First connection: `srv1`
- Second connection: `srv2`
- Third connection: `srv3`
- Fourth connection: `srv1`

The selected server remains associated with the connection so that subsequent packets are forwarded to the same destination.

This implementation will serve as the baseline for testing the load balancer before introducing more advanced scheduling methods.

### 5.2 D2: Load-Aware Scheduling

In D2, the load-balancing algorithm will be improved to consider the current condition of each backend server.

Instead of selecting servers only by their position in the list, the controller will use server health or load information to make its decisions. The monitoring mechanism and selection criteria will be implemented in the next phase.

## 6. D1 Implementation Checklist

- [ ] Set up the Mininet topology with one switch, three clients, and three backend servers.
- [ ] Configure the Ryu controller to use OpenFlow 1.3.
- [ ] Implement ARP handling for the Virtual IP.
- [ ] Set up multithreaded Python TCP servers on the backend hosts.
- [ ] Configure clients to connect to `10.0.0.100:8080`.
- [ ] Implement round-robin server selection in `static_lb.py`.
- [ ] Install flow rules for forwarding requests to the selected server.
- [ ] Configure reverse flow rules for response translation.
- [ ] Test TCP connectivity through the Virtual IP.
- [ ] Test simultaneous connections from multiple clients.
- [ ] Verify packet forwarding and address translation using Mininet and packet-capture tools.

## 7. Expected Outcome

By the end of D1, the basic network and load-balancing setup should be in place. Clients should be able to send TCP requests to the Virtual IP, and the controller should be able to distribute new connections among the available backend servers.

The main goals are to establish the Mininet topology, get the TCP servers running, configure the initial SDN forwarding rules, and verify that communication through the Virtual IP works as expected.

This setup will provide the foundation for D2, where server monitoring and load-aware scheduling will be introduced.

## 8. Next Phase

**D2: Adaptive Load Balancing and Backend Health Monitoring**

The next phase will focus on monitoring backend server health, collecting load information, and using those metrics to select suitable servers dynamically.
