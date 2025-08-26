import os
import time
import asyncio
import logging
import requests
import smtplib
from email.mime.text import MIMEText
from betfairlightweight import APIClient
from betfairlightweight.filters import (
    market_filter, price_projection, limit_order, place_instruction
)

# --- Logging setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger("BetfairBot")

# --- ENV / CONFIG ---
BF_USER = os.getenv("BF_USERNAME")
BF_PASS = os.getenv("BF_PASSWORD")
BF_KEY = os.getenv("BF_APP_KEY")
CERT_DIR = os.getenv("BF_CERTS", "./certs")
AF_KEY = os.getenv("API_FOOTBALL_KEY")

EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")
EMAIL_TO = os.getenv("EMAIL_TO")

SET = {
    "stake_half": 5.0,
    "poll_seconds": 20,
    "cashout_minute": 71,
}

AF_HEADERS = {"x-apisports-key": AF_KEY}

# --- Betfair client ---
client = APIClient(BF_USER, BF_PASS, app_key=BF_KEY, certs=CERT_DIR)
client.login()


# --- Email helper ---
def send_email(subject, body):
    if not EMAIL_USER or not EMAIL_PASS or not EMAIL_TO:
        return
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = EMAIL_USER
        msg["To"] = EMAIL_TO
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(EMAIL_USER, EMAIL_PASS)
            s.sendmail(EMAIL_USER, [EMAIL_TO], msg.as_string())
    except Exception as e:
        log.error("Email send failed: %s", e)


# --- Betfair helpers ---
def get_market_book(market_id):
    return client.betting.list_market_book(
        market_ids=[market_id],
        price_projection=price_projection(
            price_data=["EX_BEST_OFFERS"],
            virtualise=True
        )
    )[0]


def safe_best_back(runner):
    try:
        return runner.ex.available_to_back[0].price
    except Exception:
        return None


def safe_best_lay(runner):
    try:
        return runner.ex.available_to_lay[0].price
    except Exception:
        return None


def place_back_order(market_id, selection_id, size, price):
    instruction = place_instruction(
        selection_id=selection_id,
        order_type="LIMIT",
        side="BACK",
        limit_order=limit_order(
            size=size,
            price=price,
            persistence_type="PERSIST"
        )
    )
    return client.betting.place_orders(market_id=market_id, instructions=[instruction])


def place_lay_order(market_id, selection_id, size, price):
    instruction = place_instruction(
        selection_id=selection_id,
        order_type="LIMIT",
        side="LAY",
        limit_order=limit_order(
            size=size,
            price=price,
            persistence_type="PERSIST"
        )
    )
    return client.betting.place_orders(market_id=market_id, instructions=[instruction])


# --- Match handler ---
async def handle_match(fixture):
    fixture_id = fixture["fixture"]["id"]
    home = fixture["teams"]["home"]["name"]
    away = fixture["teams"]["away"]["name"]
    log.info("Handling fixture %s v %s", home, away)

    # Find Over/Under 1.5 market
    mkt_req = market_filter(
        event_ids=[],
        text_query="Over/Under 1.5 Goals"
    )
    markets = client.betting.list_market_catalogue(
        filter=mkt_req, max_results=5
    )
    if not markets:
        log.warning("No Over/Under 1.5 found for %s v %s", home, away)
        return
    market_id = markets[0].market_id

    # Get market book
    mb = get_market_book(market_id)
    over_sel_id = next(r.selection_id for r in mb.runners if r.selection_id)

    runner_book = next(r for r in mb.runners if r.selection_id == over_sel_id)
    best_back = safe_best_back(runner_book)
    if not best_back:
        log.warning("No back price found for Over 1.5")
        return

    # Place BACK bet
    resp = place_back_order(market_id, over_sel_id, SET["stake_half"], best_back)
    log.info("Back bet placed: %s", getattr(resp, "status", resp))
    send_email("Back bet placed", f"{home} v {away}, stake {SET['stake_half']} @ {best_back}")

    matched_size = SET["stake_half"]  # assume full match (simplified)
    entry_price = best_back

    # Monitor for goal / cashout
    async def wait_and_cashout():
        nonlocal matched_size, entry_price
        while True:
            try:
                r = requests.get(
                    f"https://v3.football.api-sports.io/fixtures?id={fixture_id}",
                    headers=AF_HEADERS, timeout=10
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

                # ✅ Only cashout if a back bet matched
                if matched_size <= 0:
                    log.warning("No matched back stake — skipping cashout.")
                    break

                if home_g != 0 or away_g != 0:
                    log.info("Goal detected %s-%s. Cashing out...", home_g, away_g)
                    send_email("Cash out triggered", f"{home} v {away} {home_g}-{away_g} {minute_now}'")
                    market_book = get_market_book(market_id)
                    runner_book = next((r for r in market_book.runners if r.selection_id == over_sel_id), None)
                    lay_price = safe_best_lay(runner_book)
                    # ✅ Ensure lay < entry back odds
                    if lay_price and lay_price < entry_price:
                        resp_lay = place_lay_order(market_id, over_sel_id, matched_size, lay_price)
                        log.info("Placed lay: %s", getattr(resp_lay, "status", resp_lay))
                    else:
                        log.warning("No safe lay (lay=%s, entry=%s)", lay_price, entry_price)
                    break

                if minute_now >= int(SET["cashout_minute"]):
                    log.info("Time reached %s'. Cashing out...", minute_now)
                    send_email("Cash out time", f"{home} v {away} {minute_now}'")
                    market_book = get_market_book(market_id)
                    runner_book = next((r for r in market_book.runners if r.selection_id == over_sel_id), None)
                    lay_price = safe_best_lay(runner_book)
                    if lay_price and lay_price < entry_price:
                        resp_lay = place_lay_order(market_id, over_sel_id, matched_size, lay_price)
                        log.info("Placed lay: %s", getattr(resp_lay, "status", resp_lay))
                    else:
                        log.warning("No safe lay (lay=%s, entry=%s)", lay_price, entry_price)
                    break

                await asyncio.sleep(SET["poll_seconds"])
            except Exception as e:
                log.exception("Error in monitor: %s", e)
                await asyncio.sleep(SET["poll_seconds"])

    await wait_and_cashout()


# --- Main loop ---
async def main():
    log.info("Starting market scanner...")
    while True:
        try:
            resp = requests.get(
                "https://v3.football.api-sports.io/fixtures?live=all",
                headers=AF_HEADERS, timeout=10
            )
            resp.raise_for_status()
            data = resp.json()
            for fixture in data.get("response", []):
                goals = fixture.get("goals", {})
                if goals.get("home", 0) == 0 and goals.get("away", 0) == 0:
                    await handle_match(fixture)
            await asyncio.sleep(SET["poll_seconds"])
        except Exception as e:
            log.exception("Main loop error: %s", e)
            await asyncio.sleep(SET["poll_seconds"])


if __name__ == "__main__":
    asyncio.run(main())
