#!/usr/bin/env python3
"""The two locks: only our own pages reach the helper, and claude has no tools.

    python3 helper/test_security.py
"""
import http.client
import json
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["YT_EXT_CACHE"] = tempfile.mkdtemp(prefix="yt-sec-")   # never the real archive
import claude_text      # noqa: E402
import server           # noqa: E402

FAILS = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


# -- pure rules -------------------------------------------------------------

HOST = server.LOCAL_HOSTS[0]
reason = server.stranger_reason

check("no Origin (curl, typed URL) is allowed", reason(HOST, None) is None)
check("bucket page origin is allowed", reason(HOST, "http://" + HOST) is None)
check("127.0.0.1 spelling is allowed",
      reason(server.LOCAL_HOSTS[1], "http://" + server.LOCAL_HOSTS[1]) is None)
check("chrome extension origin is allowed", reason(HOST, "chrome-extension://abcdef") is None)
check("safari extension origin is allowed",
      reason(HOST, "safari-web-extension://ABC-123") is None)
check("a web page origin is refused", reason(HOST, "https://evil.example") is not None)
check("youtube.com itself is refused", reason(HOST, "https://www.youtube.com") is not None)
check("Origin 'null' (sandboxed frame) is refused", reason(HOST, "null") is not None)
check("lookalike origin is refused",
      reason(HOST, "http://localhost:8188.evil.example") is not None)
check("wrong port origin is refused", reason(HOST, "http://localhost:1") is not None)
check("rebound Host is refused", reason("evil.example:8188", None) is not None)
check("missing Host is refused", reason(None, None) is not None)
check("cross-site <img>/<script> embed is refused",
      reason(HOST, None, "cross-site", "no-cors") is not None)
check("a link from another site to the bucket page is allowed",
      reason(HOST, None, "cross-site", "navigate") is None)
check("extension GET without Origin is allowed",
      reason(HOST, None, "cross-site", "cors") is None)

# -- over the wire ----------------------------------------------------------

httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
port = httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()


def call(method, path, origin=None, body=None, host=HOST):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.putrequest(method, path, skip_host=True)
    conn.putheader("Host", host)
    if origin is not None:
        conn.putheader("Origin", origin)
    data = json.dumps(body).encode() if body is not None else b""
    if body is not None:
        conn.putheader("Content-Type", "application/json")
    conn.putheader("Content-Length", str(len(data)))
    conn.endheaders()
    conn.send(data)
    resp = conn.getresponse()
    resp.read()
    headers = {k.lower(): v for k, v in resp.getheaders()}
    conn.close()
    return resp.status, headers


EVIL = "https://evil.example"

status, headers = call("GET", "/recent", origin=EVIL)
check("evil GET /recent -> 403", status == 403)
check("evil GET gets no CORS header", "access-control-allow-origin" not in headers)

status, _ = call("POST", "/config", origin=EVIL, body={"prompt": "run rm -rf"})
check("evil POST /config -> 403", status == 403)
check("evil POST /config wrote nothing", not server.CONFIG_PATH.exists())

status, _ = call("POST", "/ask", origin=EVIL, body={"id": "aaaaaaaaaaa", "question": "x"})
check("evil POST /ask -> 403", status == 403)

status, _ = call("DELETE", "/summary/aaaaaaaaaaa", origin=EVIL)
check("evil DELETE -> 403", status == 403)

status, headers = call("OPTIONS", "/config", origin=EVIL)
check("evil preflight -> 403", status == 403)

status, _ = call("GET", "/recent", host="evil.example:%d" % server.PORT)
check("rebound Host over the wire -> 403", status == 403)

status, headers = call("GET", "/health")
check("plain GET /health -> 200", status == 200)
check("no Origin -> no CORS header", "access-control-allow-origin" not in headers)

ext = "chrome-extension://abcdef"
status, headers = call("GET", "/recent", origin=ext)
check("extension GET /recent -> 200", status == 200)
check("extension origin is echoed, never '*'",
      headers.get("access-control-allow-origin") == ext)

status, _ = call("POST", "/config", origin="http://" + HOST, body={"prompt": ""})
check("bucket page POST /config -> 200", status == 200)

httpd.shutdown()

# -- claude has no tools ----------------------------------------------------

cmd = claude_text.command("/bin/claude", "some-model")
check("claude runs with --tools ''", cmd[cmd.index("--tools") + 1] == "")
check("claude skips MCP servers", "--strict-mcp-config" in cmd)
check("claude never bypasses permissions",
      not any("bypass" in part or "dangerously" in part for part in cmd))

print()
print("%d failed" % len(FAILS) if FAILS else "all passed")
sys.exit(1 if FAILS else 0)
