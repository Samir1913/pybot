import yaml
import logging
import time
import requests
from betfairlightweight import APIClient
from betfairlightweight.filters import market_filter, price_projection

# ---------------- Logging ----------------
logging.basicConfig(
    filename="over15_bot.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# ---------------- Config ----------------
with open("config.yml") as f:
    cfg = yaml.safe_load(f)

# Betfair
BF_USER = cfg.get("betfair", {}).get("username")
BF_PASS = cfg.get("betfair", {}).get("password")
BF_KEY = cfg.get("betfair", {}).get("app_key")
CERT_DIR = cfg.get("betfair", {}).get("certs", "./certs")

# API-Football
API_FOOTBALL_KEY = cfg.get("apifootball", {}).get("api_key")
API_TIMEZONE = cfg.get("apifootball", {}).get("timezone", "Europe/London")

# Mode
TEST_MODE = cfg.get("settings", {}).get("test_mode", True)

# ---------------- Betfair Client ----------------
try:
    client = APIClient(BF_USER, BF_PASS, app_key=BF_KEY, certs=CERT_DIR)
    trading = client.login()
    logger.info("✅ Logged in to Betfair successfully")
except Exception as e:
    logger.error(f"❌ Betfair login failed: {e}")
    trading = None

# ---------------- Betting Functions ----------------
def place_back_over15(market_id, price=2.0, size=2.0):
    """Place a BACK bet on Over 1.5 Goals market."""
    if TEST_MODE:
        logger.info(f"[TEST] Would BACK Over 1.5 @ {price} (stake {size}) on market {market_id}")
        return None

    try:
        instruction = {
            "selectionId": 12345,  # placeholder, replace with correct Over 1.5 selectionId
            "handicap": 0,
            "side": "BACK",
            "orderType": "LIMIT",
            "limitOrder": {
                "size": size,
                "price": price,
                "persistenceType": "LAPSE"
            }
        }
        resp = trading.betting.place_orders(market_id, [instruction])
        logger.info(f"✅ Bet placed: {resp}")
        return resp
    except Exception as e:
        logger.error(f"❌ Bet placement failed: {e}")
        return None


def safe_cashout_on_goal(market_id):
    """Close position after goal."""
    if TEST_MODE:
        logger.info(f"[TEST] Would cash out on market {market_id}")
        return None

    try:
        # Simplified cashout logic (for demo)
        logger.info(f"✅ Cashing out on market {market_id}")
    except Exception as e:
        logger.error(f"❌ Cashout failed: {e}")


# ---------------- API-Football Scanner ----------------
def scan_live_matches():
    """Get live matches and detect goal/xG conditions."""
    url = "https://v3.football.api-sports.io/fixtures"
    headers = {"x-apisports-key": API_FOOTBALL_KEY}
    params = {"live": "all", "timezone": API_TIMEZONE}

    try:
        r = requests.get(url, headers=headers, params=params, timeout=10)
        data = r.json()
        matches = data.get("response", [])
        logger.info(f"Scanned {len(matches)} live matches")
        return matches
    except Exception as e:
        logger.error(f"❌ API-Football scan failed: {e}")
        return []


# ---------------- Main Loop ----------------
if __name__ == "__main__":
    logger.info("🚀 Bot started (TEST_MODE=%s)", TEST_MODE)

    while True:
        matches = scan_live_matches()

        for m in matches:
            fixture = m.get("fixture", {})
            teams = m.get("teams", {})
            goals = m.get("goals", {})
            status = fixture.get("status", {}).get("short")

            home = teams.get("home", {}).get("name")
            away = teams.get("away", {}).get("name")
            score = f"{goals.get('home',0)}-{goals.get('away',0)}"

            logger.info(f"{home} vs {away} | Score {score} | Status {status}")

            # Example condition: 0-0 at HT -> place bet
            if score == "0-0" and status == "HT":
                logger.info(f"🎯 Condition met for {home} vs {away}")
                place_back_over15(market_id="1.234567")  # replace with real market_id

        time.sleep(60)  # scan once per minute
