import ssl
import urllib.parse
import os
import dotenv
from base64 import b64encode
from os import urandom

dotenv.load_dotenv("/Users/vamshi/Projects/crypto_platform/.env")
key = os.environ.get("HELIUS_API_KEY")
print(f"Loaded key: {key}")
parsed = urllib.parse.urlparse(f"wss://mainnet.helius-rpc.com/?api-key={key}")
host = parsed.hostname
port = parsed.port or 443
path = parsed.path or "/"
if parsed.query:
    path = f"{path}?{parsed.query}"

sock = ssl.create_default_context().wrap_socket(
    __import__("socket").create_connection((host, port), timeout=5),
    server_hostname=host,
)
key = b64encode(urandom(16)).decode()
req = (
    f"GET {path} HTTP/1.1\r\n"
    f"Host: {host}\r\n"
    "Upgrade: websocket\r\n"
    "Connection: Upgrade\r\n"
    f"Sec-WebSocket-Key: {key}\r\n"
    "Sec-WebSocket-Version: 13\r\n"
    "\r\n"
)
sock.sendall(req.encode())
header = b""
while b"\r\n\r\n" not in header:
    chunk = sock.recv(4096)
    if not chunk:
        print("Closed by server")
        break
    header += chunk

print("Header:")
print(header.decode(errors="replace"))

print("Listening for messages...")
sock.settimeout(5)
try:
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            print("Socket closed by server!")
            break
        print(f"Received chunk of {len(chunk)} bytes")
except Exception as e:
    print("Exception:", e)
