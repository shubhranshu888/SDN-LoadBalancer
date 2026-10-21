import socket
import threading
import time
import sys

def make_request(target_ip, target_port, client_id):
    """Creates a single TCP socket connection and measures round-trip time."""
    try:
        start_time = time.time()
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect((target_ip, target_port))
        
        # Send a basic data payload
        s.sendall(b"GET / HTTP/1.1\r\nHost: " + target_ip.encode() + b"\r\n\r\n")
        response = s.recv(1024)
        
        rtt = (time.time() - start_time) * 1000
        print(f"[Request {client_id}] Success: {rtt:.2f} ms")
        s.close()
    except Exception as e:
        print(f"[Request {client_id}] Failed: {e}")

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python3 tcp_client.py <Virtual_IP> <Num_Requests>")
        sys.exit(1)
        
    target = sys.argv[1]
    num_reqs = int(sys.argv[2])
    port = 8080
    
    print(f"[*] Sending {num_reqs} concurrent requests to Load Balancer IP {target}:{port}")
    threads = []
    
    for i in range(num_reqs):
        t = threading.Thread(target=make_request, args=(target, port, i))
        threads.append(t)
        t.start()
        time.sleep(0.05) # Tiny delay to simulate natural traffic flow
        
    for t in threads:
        t.join()
        
    print("[*] Traffic generation complete.")
