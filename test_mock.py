#!/usr/bin/env python3
"""
test_mock.py - End-to-end test of dropcatch.py against a mock Dynadot API.

Spins a local HTTP server that mimics api3.json (balance, search, register),
runs dropcatch.py against it via DYNADOT_API_BASE, and asserts the bot:
  1. passes the balance pre-flight,
  2. polls an unavailable name until the mock "drops" it,
  3. retries register attempts until one succeeds,
  4. logs the catch.

No real API key and no money involved. Run: python3 test_mock.py
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))


class MockState:
    def __init__(self):
        self.started = time.monotonic()
        self.register_calls = []
        self.search_calls = 0
        self.drop_after_seconds = 4        # name "drops" 4s into the test
        self.fail_first_registers = 2      # first 2 register attempts fail


STATE = MockState()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        cmd = q.get("command", [""])[0]
        if q.get("key", [""])[0] != "test-key":
            return self._send({"Error": "bad key"})
        if cmd == "get_account_balance":
            return self._send({"GetAccountBalanceResponse": {"ResponseCode": 0, "Balance": "500.00"}})
        if cmd == "search":
            STATE.search_calls += 1
            dropped = (time.monotonic() - STATE.started) > STATE.drop_after_seconds
            return self._send({"SearchResponse": {"ResponseCode": "0", "SearchResults": [{
                "DomainName": q.get("domain0", ["?"])[0],
                "Available": "yes" if dropped else "no",
                "Price": "11.99 in USD" if dropped else None,
            }]}})
        if cmd == "register":
            domain = q.get("domain", ["?"])[0]
            STATE.register_calls.append(domain)
            if len(STATE.register_calls) <= STATE.fail_first_registers:
                return self._send({"RegisterResponse": {"ResponseCode": "-1",
                                                        "Status": "not_available"}})
            return self._send({"RegisterResponse": {"ResponseCode": 0, "Status": "success",
                                                    "DomainName": domain,
                                                    "Expiration": 1830000000000}})
        return self._send({"Error": "unknown command %s" % cmd})


def main():
    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    with tempfile.TemporaryDirectory() as tmp:
        targets = os.path.join(tmp, "targets.csv")
        with open(targets, "w") as f:
            f.write("domain,drop_datetime_utc,max_price_usd\n")
            f.write("fakecatch-test.com,,50\n")   # no drop time -> poll mode
        log_file = os.path.join(tmp, "dropcatch.log")
        env = dict(os.environ)
        env.update({
            "DYNADOT_API_KEY": "test-key",
            "DYNADOT_API_BASE": "http://127.0.0.1:%d/api3.json" % port,
        })
        proc = subprocess.run(
            [sys.executable, os.path.join(HERE, "dropcatch.py"), "run",
             "--targets", targets, "--log-file", log_file,
             "--poll-interval", "1", "--retry-interval", "0.2",
             "--rate", "50", "--window", "1"],
            env=env, capture_output=True, text=True, timeout=120,
        )
        log = open(log_file).read()

    failures = []
    if STATE.register_calls != ["fakecatch-test.com"] * 3:
        failures.append("expected 3 register attempts (2 fail + 1 success), got %r"
                        % STATE.register_calls)
    if "CAUGHT fakecatch-test.com on attempt 3" not in log:
        failures.append("log missing catch line; log was:\n" + log)
    if "Balance pre-flight OK: 500.00" not in log:
        failures.append("balance pre-flight not logged")
    if proc.returncode != 0:
        failures.append("exit code %d; stderr:\n%s" % (proc.returncode, proc.stderr))

    if failures:
        print("FAIL")
        for f in failures:
            print(" -", f)
        sys.exit(1)
    print("PASS: bot polled, retried, caught, and logged as expected")
    print("  register attempts: %d (2 simulated failures then success)" % len(STATE.register_calls))
    print("  availability polls: %d" % STATE.search_calls)


if __name__ == "__main__":
    main()
