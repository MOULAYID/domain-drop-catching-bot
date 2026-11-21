# Domain Drop-Catching Bot (Dynadot)

Watches a list of dropping domains and fires registration attempts through the
[Dynadot API](https://www.dynadot.com/domain/api-commands) the moment each name
is released, with rapid retries until the name is caught or the attempt window
closes. Pure Python 3.8+, standard library only, no dependencies to install.

Built for re-catching your own expiring names at registration fee (~$10) instead
of paying redemption/restore fees.

## ⚠️ Read this first

1. **Contested names are unwinnable with this bot.** DropCatch.com and SnapNames
   fire hundreds of parallel registrar connections. If a name has backorders at
   those services, a single-API bot loses every time. Before each wave, check
   backorder counts on ExpiredDomains.net: **no backorders → run the bot;
   backorders → place a backorder or let it go.** This bot wins against
   *nobody*, which covers most unremarkable brandables.
2. **Every catch is a real purchase.** Registration fees come straight out of
   your Dynadot prepaid balance. Prune your targets file: any name left in it
   will be registered if the bot gets it.
3. **Namecheap's API bans drop catching.** This bot is Dynadot-only by design;
   Dynadot explicitly supports drop catching through its API.
4. **Rate limits matter.** A Regular Dynadot account gets 1 API request/sec.
   The default `--rate 1` matches that. Raise it only if your spend tier says so,
   or you'll eat `over_quota` errors at the worst moment.

## Files

| File | Purpose |
|---|---|
| `dropcatch.py` | The bot |
| `targets.example.csv` | Format reference for the targets file |
| `targets_domainepoch.csv` | Generated candidate list from the Nov 2026 renewal triage (PENDING_DELETE + LET DROP names) — **prune before use** |
| `test_mock.py` | End-to-end test against a local mock API (no key, no money) |
| `.env.example` | Environment variable template |
| `systemd/dropcatch.service` | systemd unit for a VPS |

## Targets file

```csv
domain,drop_datetime_utc,max_price_usd
example.com,2026-11-25T18:30:00Z,50
example.net,,50
```

- `drop_datetime_utc` — when the registry releases the name, ISO 8601 in UTC.
  `.com`/`.net` pending-delete drops usually land between **18:00 and 19:30 UTC**
  on the 5th day of pending delete; get exact dates from your registrar or
  ExpiredDomains.net. **Leave it blank** and the bot polls availability every
  `--poll-interval` seconds (default 300) and pounces the moment the name frees up.
- `max_price_usd` — safety cap. If the name drops into a premium price tier
  above this, the bot skips it instead of spending.

## Setup on a small VPS (Ubuntu/Debian, ~15 min)

Any $5 VPS works. A server close to Dynadot's infrastructure (US West) shaves
latency, but honestly the retry loop matters more than milliseconds.

```bash
sudo apt update && sudo apt install -y python3 git
sudo useradd -r -m -d /opt/dropcatch dropcatch
sudo git clone https://github.com/MOULAYID/domain-drop-catching-bot.git /opt/dropcatch
sudo chown -R dropcatch:dropcatch /opt/dropcatch
cd /opt/dropcatch
```

### 1. Get your Dynadot API key

1. Dynadot → **Tools → API** → unlock your account.
2. Copy the **Production Key** (and the **Sandbox Key** for testing).
3. **Whitelist your VPS IP** on the same page ("Set IP Address"). The API
   refuses calls from unlisted IPs. Find yours with `curl ifconfig.me`.
   Changes take ~10 minutes to propagate.
4. **Prepay account balance** (Account → Add Funds). Registrations are paid
   from balance; the bot checks it before every wave and refuses to run short.

### 2. Configure

```bash
cp .env.example .env        # then edit: paste your key
nano .env
cp targets.example.csv targets.csv   # or: cp targets_domainepoch.csv targets.csv
nano targets.csv            # PRUNE THIS — every line is a potential purchase
```

### 3. Test without spending money

```bash
# Offline logic test, no key needed:
python3 test_mock.py

# Live test against Dynadot's sandbox (uses your Sandbox Key, no real orders):
set -a; . ./.env; set +a
DYNADOT_SANDBOX=1 python3 dropcatch.py balance
DYNADOT_SANDBOX=1 python3 dropcatch.py check --targets targets.csv

# Real API, read-only balance check:
python3 dropcatch.py balance
```

Sandbox key + production key are different — set `DYNADOT_API_KEY` to the
sandbox one while `DYNADOT_SANDBOX=1`.

### 4. Run it

Always-on under systemd (recommended — it idles at ~zero CPU and handles
drop windows itself):

```bash
sudo cp systemd/dropcatch.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dropcatch
journalctl -u dropcatch -f        # watch the log
```

Or cron, started each morning so it's live for the 18:00-19:30 UTC drop window:

```cron
0 16 * * * cd /opt/dropcatch && set -a && . ./.env && set +a && /usr/bin/python3 dropcatch.py run --targets targets.csv >> cron.log 2>&1
```

### 5. Watch the results

Everything is logged to `dropcatch.log` (and the journal): every poll, every
attempt with its result, every catch, and an end-of-run summary.

```
2026-11-25T18:30:01Z INFO    hirecarnyc.com: catch window open until 2026-11-25T19:00:00+00:00
2026-11-25T18:31:14Z INFO    CAUGHT hirecarnyc.com on attempt 37 (success)
```

Caught names land in your Dynadot account like any registration. Verify a few
by hand after each wave.

## Commands

```
python3 dropcatch.py balance                      # show account balance
python3 dropcatch.py check --targets targets.csv  # one-shot availability/price scan
python3 dropcatch.py run --targets targets.csv    # watch + catch (the main mode)
python3 dropcatch.py catch example.com            # hammer one name right now
```

Useful flags: `--dry-run` (log attempts without sending), `--rate`,
`--retry-interval`, `--lead` (seconds before drop time to start firing),
`--window` (minutes to keep trying), `--poll-interval`, `--force` (skip the
balance pre-flight abort).

## How it catches

- **Timed targets:** sleeps until drop time minus `--lead` seconds, then fires
  `register` every `--retry-interval` seconds (with jitter) until success or
  the window closes. Registry timing drifts, so firing early and often is the
  whole point.
- **Untimed targets:** polls `search` until the name shows available, checks
  the price against your cap, then switches into the rapid-fire register loop.
- Transient API results (`system_busy`, `over_quota`, `offline`, registry
  hiccups, network errors) are retried; `insufficient_funds` stops that target
  immediately and logs it loudly.
- All threads share one token-bucket rate limiter set to your account tier.
