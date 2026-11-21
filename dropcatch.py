#!/usr/bin/env python3
"""
dropcatch.py - Drop-catching bot for the Dynadot API.

Reads a target list of domains with (optional) drop datetimes, then fires
registration attempts through the Dynadot API the moment each name drops,
with rapid retries until the name is caught or the attempt window closes.

Stdlib only. Python 3.8+.

Commands:
  balance                 Show account balance and exit.
  check                   One-shot availability + price check of all targets.
  run                     Watch targets and fire register attempts (long-running).
  catch DOMAIN            Fire attempts for a single domain right now.

Configuration (environment variables, see .env.example):
  DYNADOT_API_KEY         Your Dynadot API key. REQUIRED. Never hardcode it.
  DYNADOT_SANDBOX         "1" to use api-sandbox.dynadot.com for testing.
  DYNADOT_API_BASE        Full override of the API base URL (used by tests).
  DYNADOT_DURATION        Registration years per catch (default: 1).
  DYNADOT_CURRENCY        Currency code, e.g. USD (default: account default).

Targets file (CSV, default targets.csv):
  domain,drop_datetime_utc,max_price_usd
  - drop_datetime_utc: ISO 8601, e.g. 2026-11-25T19:00:00Z. Leave blank to
    use poll mode: the bot checks availability every --poll-interval seconds
    and registers the moment the name becomes available.
  - max_price_usd: optional cap. If the registry reports a premium price
    above this, the bot skips the name instead of spending big money.

Rate limits (Dynadot): Regular account = 1 req/sec. Bulk = 10/sec.
Set --rate to match your account tier or you will get over_quota errors.
"""

import argparse
import csv
import json
import logging
import os
import random
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

API_PROD = "https://api.dynadot.com/api3.json"
API_SANDBOX = "https://api-sandbox.dynadot.com/api3.json"

TERMINAL_REGISTER_RESULTS = {"insufficient_funds"}
TRANSIENT_REGISTER_RESULTS = {"system_busy", "over_quota", "offline", "not_available", "error"}

log = logging.getLogger("dropcatch")


# ---------------------------------------------------------------- API client

class RateLimiter:
    """Token-bucket rate limiter shared by all threads."""

    def __init__(self, per_second):
        self.min_interval = 1.0 / per_second if per_second > 0 else 0
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            if now < self.next_at:
                delay = self.next_at - now
            else:
                delay = 0.0
            self.next_at = max(now, self.next_at) + self.min_interval
        if delay:
            time.sleep(delay)


class DynadotError(Exception):
    pass


class Dynadot:
    def __init__(self, api_key, base_url, rate_limiter, timeout=20):
        self.api_key = api_key
        self.base_url = base_url
        self.rl = rate_limiter
        self.timeout = timeout
        self.ctx = ssl.create_default_context()

    def call(self, command, **params):
        q = {"key": self.api_key, "command": command}
        q.update({k: v for k, v in params.items() if v is not None})
        url = self.base_url + "?" + urllib.parse.urlencode(q)
        self.rl.wait()
        req = urllib.request.Request(url, headers={"User-Agent": "dropcatch-bot/1.0"})
        with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as r:
            body = r.read().decode("utf-8", "replace")
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            raise DynadotError("Non-JSON response to %s: %.200s" % (command, body))

    # -- commands -----------------------------------------------------------

    def get_balance(self):
        """Return balance string, or raise."""
        data = self.call("get_account_balance")
        found = _find_key(data, "balance")
        if found is None:
            raise DynadotError("Could not parse balance response: %s" % json.dumps(data)[:300])
        return str(found)

    def search(self, domain, show_price=True):
        """Return (available: bool, price_str or None, raw_status or None)."""
        params = {"domain0": domain}
        if show_price:
            params["show_price"] = "1"
        data = self.call("search", **params)
        resp = data.get("SearchResponse", {})
        results = resp.get("SearchResults", [])
        if not results:
            raise DynadotError("Empty search response for %s: %s" % (domain, json.dumps(data)[:300]))
        item = results[0]
        available = str(item.get("Available", "")).lower() == "yes"
        price = item.get("Price")
        status = item.get("Status") or item.get("Result")
        return available, price, status

    def register(self, domain, duration, currency=None):
        """Fire one registration attempt. Returns (success: bool, status: str, detail: str)."""
        data = self.call("register", domain=domain, duration=duration, currency=currency)
        resp = data.get("RegisterResponse", {})
        code = resp.get("ResponseCode", resp.get("SuccessCode", "-1"))
        status = str(resp.get("Status", "")).strip().lower()
        detail = str(resp.get("Error", "") or resp.get("Message", "") or "")
        success = str(code) == "0" and status in ("", "success")
        if success:
            return True, "success", detail
        if not status:
            status = "error"
        return False, status, detail


def _find_key(obj, needle):
    """Depth-first search for a dict key containing `needle` (case-insensitive)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if needle in str(k).lower() and not isinstance(v, (dict, list)):
                return v
        for v in obj.values():
            hit = _find_key(v, needle)
            if hit is not None:
                return hit
    elif isinstance(obj, list):
        for v in obj:
            hit = _find_key(v, needle)
            if hit is not None:
                return hit
    return None


# ---------------------------------------------------------------- targets

class Target:
    def __init__(self, domain, drop_at=None, max_price=None):
        self.domain = domain.strip().lower()
        self.drop_at = drop_at          # aware datetime (UTC) or None -> poll mode
        self.max_price = max_price      # float or None
        self.done = False
        self.result = None

    def __repr__(self):
        return "<Target %s drop_at=%s max_price=%s>" % (self.domain, self.drop_at, self.max_price)


def load_targets(path):
    targets = []
    with open(path, newline="") as f:
        reader = csv.reader(row for row in f if not row.lstrip().startswith("#"))
        header = None
        for row in reader:
            if not row or not any(c.strip() for c in row):
                continue
            if header is None:
                header = [c.strip().lower() for c in row]
                if "domain" not in header:
                    raise SystemExit("targets file must have a 'domain' column, got: %s" % header)
                continue
            rec = dict(zip(header, [c.strip() for c in row]))
            domain = rec.get("domain", "")
            if not domain or "." not in domain:
                continue
            drop_at = None
            raw_dt = rec.get("drop_datetime_utc", "")
            if raw_dt:
                drop_at = _parse_dt(raw_dt)
            max_price = None
            raw_mp = rec.get("max_price_usd", "")
            if raw_mp:
                try:
                    max_price = float(raw_mp)
                except ValueError:
                    log.warning("%s: bad max_price_usd %r, ignoring", domain, raw_mp)
            targets.append(Target(domain, drop_at, max_price))
    return targets


def _parse_dt(s):
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_price_number(price_str):
    """'77.00 in USD' -> 77.0 ; returns None when unparseable."""
    if not price_str:
        return None
    try:
        return float(str(price_str).split()[0].replace(",", ""))
    except (ValueError, IndexError):
        return None


# ---------------------------------------------------------------- catching

class Catcher:
    def __init__(self, api, duration, currency, args):
        self.api = api
        self.duration = duration
        self.currency = currency
        self.retry_interval = args.retry_interval
        self.lead_seconds = args.lead
        self.window_minutes = args.window
        self.poll_interval = args.poll_interval
        self.dry_run = args.dry_run
        self.lock = threading.Lock()

    def _register_attempt(self, t):
        if self.dry_run:
            log.info("[dry-run] would register %s (duration=%s)", t.domain, self.duration)
            return True, "dry_run", ""
        try:
            return self.api.register(t.domain, self.duration, self.currency)
        except (DynadotError, urllib.error.URLError, OSError) as e:
            return False, "error", "request failed: %s" % e

    def _mark_done(self, t, result):
        with self.lock:
            t.done = True
            t.result = result

    def catch_loop(self, t, start_at):
        """Fire register attempts from start_at until success or window close."""
        deadline = start_at + timedelta(minutes=self.window_minutes)
        attempts = 0
        log.info("%s: catch window open until %s", t.domain, deadline.isoformat())
        while datetime.now(timezone.utc) < deadline and not t.done:
            attempts += 1
            ok, status, detail = self._register_attempt(t)
            if ok:
                log.info("CAUGHT %s on attempt %d (%s)", t.domain, attempts, status)
                self._mark_done(t, "caught (attempt %d)" % attempts)
                return
            if status in TERMINAL_REGISTER_RESULTS:
                log.error("%s: giving up, terminal result: %s %s", t.domain, status, detail)
                self._mark_done(t, "FAILED: %s %s" % (status, detail))
                return
            log.debug("%s: attempt %d -> %s %s", t.domain, attempts, status, detail)
            # jitter keeps parallel targets from hammering in lockstep
            time.sleep(self.retry_interval * random.uniform(0.7, 1.3))
        if not t.done:
            log.error("%s: window closed after %d attempts, not caught", t.domain, attempts)
            self._mark_done(t, "FAILED: window closed after %d attempts" % attempts)

    def run_timed(self, t):
        """Sleep until drop time minus lead, then catch."""
        assert t.drop_at is not None
        start_at = t.drop_at - timedelta(seconds=self.lead_seconds)
        now = datetime.now(timezone.utc)
        if now < start_at:
            wait = (start_at - now).total_seconds()
            log.info("%s: drops %s, sleeping %.0fs until window opens", t.domain,
                     t.drop_at.isoformat(), wait)
            while wait > 0 and not t.done:
                time.sleep(min(wait, 30))
                wait = (start_at - datetime.now(timezone.utc)).total_seconds()
        if not t.done:
            self.catch_loop(t, t.drop_at)

    def run_polled(self, t):
        """No known drop time: poll availability, register when it appears."""
        log.info("%s: no drop time set, polling every %ds", t.domain, self.poll_interval)
        while not t.done:
            try:
                available, price, status = self.api.search(t.domain)
            except (DynadotError, urllib.error.URLError, OSError) as e:
                log.warning("%s: poll failed (%s), retrying in %ds", t.domain, e, self.poll_interval)
                time.sleep(self.poll_interval)
                continue
            if available:
                amount = parse_price_number(price)
                if t.max_price is not None and amount is not None and amount > t.max_price:
                    log.error("%s: available but priced %s, above max $%.2f. Skipping.",
                              t.domain, price, t.max_price)
                    self._mark_done(t, "SKIPPED: premium price %s > max %.2f" % (price, t.max_price))
                    return
                log.info("%s: AVAILABLE (price: %s). Catching now.", t.domain, price or "unknown")
                self.catch_loop(t, datetime.now(timezone.utc))
                return
            time.sleep(self.poll_interval)


# ---------------------------------------------------------------- commands

def cmd_balance(api, args):
    bal = api.get_balance()
    print("Account balance: %s" % bal)


def cmd_check(api, targets, args):
    for t in targets:
        try:
            available, price, status = api.search(t.domain)
        except (DynadotError, urllib.error.URLError, OSError) as e:
            print("%-40s ERROR %s" % (t.domain, e))
            continue
        state = "AVAILABLE" if available else "taken"
        print("%-40s %-10s %s" % (t.domain, state, price or ""))


def cmd_run(api, targets, args, duration, currency):
    # Balance pre-flight: make sure the account can plausibly pay for the wave.
    try:
        bal_raw = api.get_balance()
        bal = parse_price_number(bal_raw)
        est = len(targets) * args.est_cost
        if bal is not None and bal < est:
            log.error("Balance %s looks short of estimated wave cost ~$%.0f "
                      "(%d targets x $%.0f). Prepay or pass --force.",
                      bal_raw, est, len(targets), args.est_cost)
            if not args.force:
                sys.exit(2)
        else:
            log.info("Balance pre-flight OK: %s (est. wave cost ~$%.0f)", bal_raw, est)
    except DynadotError as e:
        log.warning("Balance check failed (%s); continuing anyway", e)

    catcher = Catcher(api, duration, currency, args)
    threads = []
    for t in targets:
        fn = catcher.run_timed if t.drop_at else catcher.run_polled
        th = threading.Thread(target=fn, args=(t,), name=t.domain, daemon=True)
        th.start()
        threads.append(th)
        if len(threads) >= args.max_threads:
            # stagger thread creation so bursts of timed targets share the limiter
            time.sleep(0.2)
    log.info("Watching %d targets. Ctrl-C to stop.", len(threads))
    try:
        while any(th.is_alive() for th in threads):
            time.sleep(5)
    except KeyboardInterrupt:
        log.warning("Interrupted by user.")
    caught = [t for t in targets if t.result and t.result.startswith("caught")]
    failed = [t for t in targets if t.result and t.result.startswith("FAILED")]
    skipped = [t for t in targets if t.result and t.result.startswith("SKIPPED")]
    log.info("SUMMARY: %d caught, %d failed, %d skipped, %d unresolved",
             len(caught), len(failed), len(skipped),
             len(targets) - len(caught) - len(failed) - len(skipped))
    for t in failed + skipped:
        log.info("  %-40s %s", t.domain, t.result)


def cmd_catch(api, args, duration, currency):
    t = Target(args.domain)
    catcher = Catcher(api, duration, currency, args)
    catcher.catch_loop(t, datetime.now(timezone.utc))
    print(t.domain, "->", t.result)


# ---------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(description="Dynadot drop-catching bot")
    p.add_argument("command", choices=["balance", "check", "run", "catch"])
    p.add_argument("domain", nargs="?", help="domain for 'catch'")
    p.add_argument("--targets", default="targets.csv", help="targets CSV (default: targets.csv)")
    p.add_argument("--rate", type=float, default=1.0,
                   help="max API requests/sec; Regular Dynadot tier = 1, Bulk = 10 (default: 1)")
    p.add_argument("--retry-interval", type=float, default=1.2,
                   help="seconds between register attempts per target (default: 1.2)")
    p.add_argument("--lead", type=int, default=60,
                   help="seconds before the drop time to open the catch window (default: 60)")
    p.add_argument("--window", type=int, default=30,
                   help="minutes to keep trying after the drop time (default: 30)")
    p.add_argument("--poll-interval", type=int, default=300,
                   help="seconds between availability polls for untimed targets (default: 300)")
    p.add_argument("--est-cost", type=float, default=12.0,
                   help="assumed registration cost per domain for the balance pre-flight (default: 12)")
    p.add_argument("--max-threads", type=int, default=50)
    p.add_argument("--log-file", default="dropcatch.log")
    p.add_argument("--dry-run", action="store_true", help="log register attempts without sending them")
    p.add_argument("--force", action="store_true", help="run even if the balance pre-flight fails")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)sZ %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        handlers=[logging.FileHandler(args.log_file), logging.StreamHandler(sys.stderr)],
    )
    logging.Formatter.converter = time.gmtime

    api_key = os.environ.get("DYNADOT_API_KEY", "").strip()
    if not api_key:
        sys.exit("DYNADOT_API_KEY is not set. Export it first (see .env.example). "
                 "Never hardcode the key in this file.")
    if os.environ.get("DYNADOT_API_BASE"):
        base = os.environ["DYNADOT_API_BASE"]
    elif os.environ.get("DYNADOT_SANDBOX") == "1":
        base = API_SANDBOX
        log.info("Using Dynadot SANDBOX endpoint")
    else:
        base = API_PROD
    duration = os.environ.get("DYNADOT_DURATION", "1")
    currency = os.environ.get("DYNADOT_CURRENCY") or None

    api = Dynadot(api_key, base, RateLimiter(args.rate))

    if args.command == "balance":
        cmd_balance(api, args)
    elif args.command == "check":
        cmd_check(api, load_targets(args.targets), args)
    elif args.command == "run":
        targets = load_targets(args.targets)
        if not targets:
            sys.exit("No targets in %s" % args.targets)
        cmd_run(api, targets, args, duration, currency)
    elif args.command == "catch":
        if not args.domain:
            sys.exit("catch needs a domain: dropcatch.py catch example.com")
        cmd_catch(api, args, duration, currency)


if __name__ == "__main__":
    main()
