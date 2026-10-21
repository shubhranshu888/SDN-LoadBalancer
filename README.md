# SDN-Based TCP Load Balancer

## Prerequisites
* Ubuntu Linux VM
* Mininet & Open vSwitch
* Python 3.9 (with isolated virtual environment)
* Ryu, psutil, eventlet==0.30.2

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
