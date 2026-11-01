import http.client
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = ("127.0.0.1", 8888)
LISTEN = ("0.0.0.0", 8890)

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        try:
            conn = http.client.HTTPConnection(*UPSTREAM, timeout=25)
            headers = {}
            for key, value in self.headers.items():
                if key.lower() in {"host", "connection", "content-length"}:
                    continue
                headers[key] = value
            conn.request("GET", self.path, headers=headers)
            response = conn.getresponse()
            body = response.read()
            self.send_response(response.status, response.reason)
            for key, value in response.getheaders():
                if key.lower() in {"transfer-encoding", "connection", "content-length"}:
                    continue
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            conn.close()
        except Exception as exc:
            body = f"proxy error: {exc}".encode("utf-8", "replace")
            self.send_response(502)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, fmt, *args):
        if time.time() % 1 < 0.02:
            sys.stderr.write("[api-proxy] " + (fmt % args) + "\n")

if __name__ == "__main__":
    ThreadingHTTPServer(LISTEN, Handler).serve_forever()
