import asyncio
import logging
import yaml
import time
from datetime import datetime, timezone
import requests
from betfairlightweight import APIClient
from betfairlightweight.filters import market_filter, price_projection
from dateutil import parser

# ------------ logging ------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("over15_bot.log"), logging.StreamHandler()]
)
log = logging.getLogger("over15bot")

# ------------ load config ------------
with open("config.yml", "r", encoding="utf-8") as f:
    cfg = yaml.safe_load(f)

AF_KEY = cfg["api_football"]["api_key"]
BF_USER = cfg["betfair"]["username"]
BF_PASS = cfg["betfair"]["password"]
BF_APP_KEY = cfg["betfair"]["app_key"]
BF_CERTS = cfg["betfair"].get("certs_path", "")
EMAIL_CFG = cfg.get("email", {})
SET = cfg["settings"]

# ------------ notifier ------------
import smtplib
from email.mime.text import MIMEText

def send_email(subject, body):
    if not EMAIL_CFG or not EMAIL_CFG.get("enabled", False):
        return
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = EMAIL_CFG["from_addr"]
        msg["To"] = ", ".join(EMAIL_CFG["to_addrs"])
        s = smtplib.SMTP(EMAIL_CFG["smtp_host"], EMAIL_CFG["smtp_port"])
        if EMAIL_CFG.get("use_tls", True):
            s.starttls()
        if EMAIL_CFG.get("username"):
            s.login(EMAIL_CFG["username"], EMAIL_CFG["password"])
        s.sendmail(EMAIL_CFG["from_addr"], EMAIL_CFG["to_addrs"], msg.as_string())
        s.quit()
        log.info("Email sent: %s", subject)
    except Exception as e:
        log.exception("Failed to send email: %s", e)

# ------------ API-Football client (simple) ------------
AF_HEADERS = {"x-apisports-key": AF_KEY}

def get_live_fixtures():
    """
    Query API-Football /fixtures?live=all and filter by country list in config.
    """
    url = "https://v3.football.api-sports.io/fixtures?live=all"
    r = requests.get(url, headers=AF_HEADERS, timeout=10)
    r.raise_for_status()
    data = r.json()
    fixtures = []
    allowed = set(SET["countries"])
    for item in data.get("response", []):
        country = item.get("league", {}).get("country")
        if country and country in allowed:
            fixtures.append(item)
    return fixtures

# ------------ Betfair client wrapper ------------
bf_client = None

def bf_login():
    global bf_client
    log.info("Logging into Betfair")
    certs = BF_CERTS if BF_CERTS else None
    bf_client = APIClient(
        username=BF_USER,
        password=BF_PASS,
        app_key=BF_APP_KEY,
        certs=certs if certs else None,
        cert_files=(
            f"{BF_CERTS}/client-2048.crt" if BF_CERTS else None,
            f"{BF_CERTS}/client-2048.key" if BF_CERTS else None,
        ) if BF_CERTS else None,
    )
    bf_client.login()
    log.info("Betfair login OK")

def find_over15_market(event):
    """
    Search Betfair markets for Over/Under 1.5 Goals by text query 'Home v Away'.
    """
    home = event["teams"]["home"]["name"]
    away = event["teams"]["away"]["name"]
    tq = f"{home} v {away}"

    filt = market_filter(text_query=tq, event_type_ids=["1"])  # Football = 1
    markets = bf_client.betting.list_market_catalogue(
        filter=filt,
        max_results=40,
        market_projection=["MARKET_START_TIME", "RUNNER_DESCRIPTION"]
    ) or []

    # Try exact name first, then fuzzy contains
    for m in markets:
        if (m.market_name or "").strip().lower() == "over/under 1.5 goals":
            return m
    for m in markets:
        name = (m.market_name or "").lower()
        if "over/under" in name and "1.5" in name:
            return m
    return None

def get_market_book(market_id):
    proj = price_projection(price_data=["EX_BEST_OFFERS"])
    mb = bf_client.betting.list_market_book(market_ids=[market_id], price_projection=proj)
    return mb[0] if mb else None

def safe_best_back(runner):
    """Return best back price if available, else None (guards all None cases)."""
    if not runner:
        return None
    ex = getattr(runner, "ex", None)
    if not ex:
        return None
    atb = getattr(ex, "available_to_back", None)
    if not atb:
        return None
    if not len(atb):
        return None
    price = getattr(atb[0], "price", None)
    return price

def safe_best_lay(runner):
    """Return best lay price if available, else None (guards all None cases)."""
    if not runner:
        return None
    ex = getattr(runner, "ex", None)
    if not ex:
        return None
    atl = getattr(ex, "available_to_lay", None)
    if not atl:
        return None
    if not len(atl):
        return None
    price = getattr(atl[0], "price", None)
    return price

def compute_stake_for_liability(odds: float) -> float:
    """
    If settings.min_liability_mode is True, size BACK stake so that liability <= settings.max_test_liability.
    Liability for BACK = (odds - 1) * stake.
    Also respect min_back_stake (Betfair typically £2; make configurable).
    In live mode, uses SET['stake'] unless liability would exceed max_live_liability (then skip).
    """
    if odds is None or odds <= 1.0:
        return 0.0

    min_back_stake = float(SET.get("min_back_stake", 2.0))
    test_mode = bool(SET.get("test_mode", True))
    min_liability_mode = bool(SET.get("min_liability_mode", True))

    if test_mode and min_liability_mode:
        max_liab = float(SET.get("max_test_liability", 1.0))
        # stake = liability / (odds - 1)
        stake = max_liab / (odds - 1.0)
        # Betfair enforces a min stake; if computed is below min_back_stake, we have to place min_back_stake.
        # (If that’s too big for your risk, keep test_mode=True and watch only.)
        if stake < min_back_stake:
            stake = min_back_stake
        # Also cap stake at optional SET['test_stake_cap']
        stake_cap = float(SET.get("test_stake_cap", min_back_stake))
        stake = min(stake, stake_cap)
        return round(stake, 2)

    # Live mode (or test without min-liability mode) → use configured stake,
    # but block if liability would exceed max_live_liability (if provided)
    stake = float(SET.get("test_stake", 0.5)) if test_mode else float(SET.get("stake", 5.0))
    max_live_liab = SET.get("max_live_liability")
    if max_live_liab is not None:
        liab = (odds - 1.0) * stake
        if liab > float(max_live_liab):
            # signal to skip by returning 0
            return 0.0
    # Respect min_back_stake too
    if stake < min_back_stake:
        stake = min_back_stake
    return round(stake, 2)

def place_back_order(market_id, selection_id, size, price):
    """
    Place a simple LIMIT BACK order with safe guards.
    """
    try:
        if size <= 0 or price is None:
            return None
        instruction = {
            "orderType": "LIMIT",
            "selectionId": int(selection_id),
            "side": "BACK",
            "limitOrder": {
                "size": float(size),
                "price": float(price),
                "persistenceType": "LAPSE",
            },
        }
        resp = bf_client.betting.place_orders(market_id=market_id, instructions=[instruction])
        return resp
    except Exception as e:
        log.exception("place_back_order failed: %s", e)
        return None

def place_lay_order(market_id, selection_id, size, price):
    try:
        if size <= 0 or price is None:
            return None
        instruction = {
            "orderType": "LIMIT",
            "selectionId": int(selection_id),
            "side": "LAY",
            "limitOrder": {"size": float(size), "price": float(price), "persistenceType": "LAPSE"},
        }
        resp = bf_client.betting.place_orders(market_id=market_id, instructions=[instruction])
        return resp
    except Exception as e:
        log.exception("place_lay_order failed: %s", e)
        return None

# ------------ Core logic ------------
async def handle_match(event):
    """
    For each live fixture from API-Football:
    - check 0-0 and elapsed minute
    - find over 1.5 market
    - place back bet (min-liability sizing in test mode if enabled)
    - monitor for goal/time cutoff and cash out (LAY) if possible
    """
    fixture_id = event["fixture"]["id"]
    home = event["teams"]["home"]["name"]
    away = event["teams"]["away"]["name"]
    minute = event.get("fixture", {}).get("status", {}).get("elapsed")
    if minute is None:
        return

    score = event["goals"]  # dict with 'home' 'away'
    home_goals = score.get("home", 0) or 0
    away_goals = score.get("away", 0) or 0

    if home_goals != 0 or away_goals != 0:
        return

    if not (SET["min_minute"] <= minute <= SET["max_minute"]):
        return

    # found candidate
    log.info("Candidate match %s vs %s at %s' (0-0). Searching Betfair market...", home, away, minute)
    send_email("Market found", f"{home} v {away} at {minute}' is 0-0 and in target time window.")

    # find market (attempt few retries)
    bf_market = None
    for attempt in range(SET["market_check_retry"]):
        try:
            bf_market = find_over15_market(event)
            if bf_market:
                break
        except Exception as e:
            log.exception("Error finding market (attempt %s): %s", attempt+1, e)
        await asyncio.sleep(SET["market_check_delay"])
    if not bf_market:
        log.warning("No market found on Betfair for %s v %s", home, away)
        return

    market_id = bf_market.market_id
    log.info("Found market %s (%s)", bf_market.market_name, market_id)

    # get market book to find selection id and best prices
    book = get_market_book(market_id)
    if not book:
        log.warning("No market book returned for %s", market_id)
        return

    # find the OVER 1.5 runner by name mapping from catalogue
    over_sel_id = None
    for rc in (bf_market.runners or []):
        rn = getattr(rc, "runner_name", "") or ""
        if "over" in rn.lower() and "1.5" in rn:
            over_sel_id = rc.selection_id
            break

    runner = None
    if over_sel_id is not None:
        runner = next((r for r in (book.runners or []) if r.selection_id == over_sel_id), None)

    # fallback: choose first runner with back price
    if runner is None:
        for r in (book.runners or []):
            if safe_best_back(r):
                runner = r
                over_sel_id = r.selection_id
                break

    if not runner or over_sel_id is None:
        log.warning("No suitable runner found for market %s", market_id)
        return

    best_back = safe_best_back(runner)
    if best_back is None:
        log.warning("No back price available for selection %s", over_sel_id)
        return

    if best_back > float(SET.get("max_price", 50.0)):
        log.info("Best back price %s > max_price %s — skipping", best_back, SET["max_price"])
        return

    # compute stake (min-liability mode in test)
    stake = compute_stake_for_liability(best_back)
    if stake <= 0:
        log.warning("Stake computed as 0 (liability or limits). Skipping placement.")
        return

    # notify before placing bet
    send_email(
        "Placing Over 1.5 BACK",
        f"Placing BACK on {home} v {away} - market {market_id} - price {best_back} - stake {stake}"
    )

    log.info("Placing BACK size=%.2f at price=%.2f (test_mode=%s, min_liability_mode=%s)",
             stake, best_back, SET.get("test_mode", True), SET.get("min_liability_mode", True))
    place_resp = place_back_order(market_id, over_sel_id, stake, best_back)
    log.info("Place response: %s", getattr(place_resp, "status", place_resp))

    # Parse place response (best effort)
    matched_size = 0.0
    try:
        if place_resp and getattr(place_resp, "status", "") == "SUCCESS":
            for ir in getattr(place_resp, "instruction_reports", []) or []:
                if getattr(ir, "status", "") == "SUCCESS":
                    # assume full match for simplicity; production: poll order status
                    matched_size += float(stake)
    except Exception:
        log.exception("Failed to parse place response")

    # monitor until goal/time stop
    async def wait_and_cashout():
        nonlocal matched_size
        while True:
            try:
                # requery API-Football for fixture state
                r = requests.get(
                    f"https://v3.football.api-sports.io/fixtures?id={fixture_id}",
                    headers=AF_HEADERS,
                    timeout=10
                )
                r.raise_for_status()
                resp = r.json()
                if not resp.get("response"):
                    await asyncio.sleep(SET["poll_seconds"])
                    continue
                ev = resp["response"][0]
                minute_now = (ev.get("fixture", {}).get("status", {}) or {}).get("elapsed") or 0
                goals = ev.get("goals", {}) or {}
                home_g = goals.get("home", 0) or 0
                away_g = goals.get("away", 0) or 0

                if home_g != 0 or away_g != 0:
                    log.info("Goal detected (%s-%s). Initiating cash-out...", home_g, away_g)
                    send_email("Cash out triggered — goal", f"{home} v {away} now {home_g}-{away_g} at {minute_now}' — cashing out.")
                    market_book = get_market_book(market_id)
                    if not market_book:
                        log.warning("No market book at cashout time.")
                        break
                    runner_book = next((r for r in (market_book.runners or []) if r.selection_id == over_sel_id), None)
                    lay_price = safe_best_lay(runner_book)
                    if lay_price:
                        resp_lay = place_lay_order(market_id, over_sel_id, matched_size or stake, lay_price)
                        log.info("Placed lay to cashout: %s", getattr(resp_lay, "status", resp_lay))
                    else:
                        log.warning("No lay price available. Skipping lay attempt.")
                    break

                if minute_now >= int(SET.get("cashout_minute", 71)):
                    log.info("Time exceeded threshold (%s'). Initiating cash-out...", minute_now)
                    send_email("Cash out triggered — time", f"{home} v {away} reached {minute_now}' — cashing out.")
                    market_book = get_market_book(market_id)
                    if not market_book:
                        log.warning("No market book at time cashout.")
                        break
                    runner_book = next((r for r in (market_book.runners or []) if r.selection_id == over_sel_id), None)
                    lay_price = safe_best_lay(runner_book)
                    if lay_price:
                        resp_lay = place_lay_order(market_id, over_sel_id, matched_size or stake, lay_price)
                        log.info("Placed lay to cashout: %s", getattr(resp_lay, "status", resp_lay))
                    else:
                        log.warning("No lay price available at time cashout.")
                    break

                # wait a bit and recheck
                await asyncio.sleep(SET["poll_seconds"])
            except Exception as e:
                log.exception("Error while monitoring match: %s", e)
                await asyncio.sleep(SET["poll_seconds"])
        # end loop

    await wait_and_cashout()
    log.info("Finished handling %s v %s", home, away)

# ------------ main loop ------------
async def main_loop():
    bf_login()
    try:
        while True:
            try:
                fixtures = get_live_fixtures()
                if not fixtures:
                    await asyncio.sleep(SET["poll_seconds"])
                    continue

                tasks = []
                for f in fixtures:
                    # filter 0-0 and minute window early (to reduce tasks)
                    minute = (f.get("fixture", {}).get("status", {}) or {}).get("elapsed")
                    goals = f.get("goals", {}) or {}
                    if minute is None:
                        continue
                    if goals.get("home", 0) or goals.get("away", 0):
                        continue
                    if not (SET["min_minute"] <= minute <= SET["max_minute"]):
                        continue
                    tasks.append(handle_match(f))

                if tasks:
                    await asyncio.gather(*tasks)

                await asyncio.sleep(SET["poll_seconds"])
            except Exception as e:
                log.exception("Main loop error: %s", e)
                await asyncio.sleep(5)
    finally:
        try:
            bf_client.logout()
        except Exception:
            pass

if __name__ == "__main__":
    asyncio.run(main_loop())
