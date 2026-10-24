# SDN-Based TCP Load Balancer

## Prerequisites
* Ubuntu Linux VM
* Mininet & Open vSwitch
* Python 3.9 (with isolated virtual environment)
* Ryu, psutil, eventlet==0.30.2


## Overview

SDN-LoadBalancer is a Software-Defined Networking (SDN) project that distributes TCP traffic across multiple backend servers using a centralized Ryu controller.

The project uses Mininet to create a virtual network with three client hosts, three backend servers, and an OpenFlow switch.

Clients send HTTP requests to a common Virtual IP (`10.0.0.100`). The controller selects a backend server and manages traffic forwarding using OpenFlow rules.

## Architecture

The project consists of the following components:

- **Ryu Controller:** Manages traffic forwarding and backend server selection.
- **Mininet:** Creates the virtual network topology.
- **Backend Servers:** Handle incoming TCP requests and return HTTP responses.
- **Client:** Generates concurrent TCP requests and measures response time.

The repository includes two controller implementations:

- `static_lb.py` – Implements round-robin load balancing.
- `adaptive_controller.py` – Implements CPU-based server selection using server telemetry and health information.

## Working

1. The Mininet topology connects three clients and three backend servers through an OpenFlow switch.
2. Clients send TCP requests to the load balancer's Virtual IP.
3. The Ryu controller selects a backend server.
4. The switch forwards the request to the selected server.
5. The backend server processes the request and returns an HTTP response.
6. The client measures and displays the response time.

The backend servers also report CPU and memory utilization through UDP telemetry for the adaptive controller.

## Technologies

- Python
- Ryu SDN Controller
- Mininet
- Open vSwitch
- OpenFlow 1.3
- TCP/IP
- psutil

## Execution

### 1. Start the Controller

```bash
ryu-manager controller/static_lb.py
```

### 2. Start Mininet

```bash
sudo python3 topology/topology.py
```

### 3. Start Backend Servers

At the Mininet prompt:

```bash
srv1 python3 server/tcp_server.py srv1 10.0.0.1 &
srv2 python3 server/tcp_server.py srv2 10.0.0.2 &
srv3 python3 server/tcp_server.py srv3 10.0.0.3 &
```

### 4. Generate Client Traffic

```bash
c1 python3 client/tcp_client.py 10.0.0.100 6
```

This sends six TCP requests to the Virtual IP and displays their success status and response times.
## Execution Instructions for D1 Demo

**1. Start the SDN Controller**
`ryu-manager controller/static_lb.py`

**2. Start Mininet**
`sudo python3 topology/topology.py`

**3. Start Backend Servers**
At the `mininet>` prompt, start the three multithreaded servers:
`mininet> srv1 python3 server/tcp_server.py srv1 10.0.0.1 &`
`mininet> srv2 python3 server/tcp_server.py srv2 10.0.0.2 &`
`mininet> srv3 python3 server/tcp_server.py srv3 10.0.0.3 &`

**4. Generate Concurrent Traffic**
At the `mininet>` prompt, trigger the client to send 6 simultaneous TCP requests to the Load Balancer Virtual IP (10.0.0.100):
`mininet> c1 python3 client/tcp_client.py 10.0.0.100 6`
