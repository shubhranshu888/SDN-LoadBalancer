import socket
import threading
import psutil
import time
import json
import sys

# Server Configuration
HOST = '0.0.0.0'
PORT = 8080
CONTROLLER_IP = '10.0.0.254' # Dummy IP for the SDN switch to intercept
CONTROLLER_PORT = 5555

def handle_client(conn, addr):
    """Handles incoming client TCP connections."""
    try:
        data = conn.recv(1024)
        if data:
            # Send a simple HTTP 200 OK response
            response = b"HTTP/1.1 200 OK\r\n\r\nHello from Adaptive Server!"
            conn.sendall(response)
    except Exception as e:
        pass
    finally:
        conn.close()

def tcp_listener():
    """Listens for TCP traffic on port 8080."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen(10)
    print(f"[*] TCP Server listening on {HOST}:{PORT}")
    
    while True:
        conn, addr = server.accept()
        # Spawn a new thread for every client request to support concurrency
        thread = threading.Thread(target=handle_client, args=(conn, addr))
        thread.start()

def telemetry_reporter(server_id, server_ip):
    """Sends server CPU/Mem load to the SDN controller every 2 seconds."""
    udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    while True:
        # Note: In Mininet, hosts share the VM's CPU. 
        # For demo purposes, we monitor the local VM CPU load.
        cpu_load = psutil.cpu_percent(interval=None)
        mem_load = psutil.virtual_memory().percent
        
        telemetry_data = {
            "server_id": server_id,
            "ip": server_ip,
            "cpu": cpu_load,
            "mem": mem_load
        }
        
        message = json.dumps(telemetry_data).encode('utf-8')
        udp_sock.sendto(message, (CONTROLLER_IP, CONTROLLER_PORT))
        time.sleep(2)

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python3 tcp_server.py <Server_ID> <Server_IP>")
        sys.exit(1)
        
    srv_id = sys.argv[1]
    srv_ip = sys.argv[2]
    
    # Start the telemetry reporter in a background thread
    t_thread = threading.Thread(target=telemetry_reporter, args=(srv_id, srv_ip), daemon=True)
    t_thread.start()
    
    # Start the main TCP listener on the main thread
    tcp_listener()
