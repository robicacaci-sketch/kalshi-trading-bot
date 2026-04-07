import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

# --- Kalshi credentials ---
KALSHI_API_KEY_ID: str = os.environ["KALSHI_API_KEY_ID"]
KALSHI_PRIVATE_KEY_PATH: Path = Path(os.environ["KALSHI_PRIVATE_KEY_PATH"])

# --- Environment selection ---
KALSHI_ENV: str = os.getenv("KALSHI_ENV", "demo").lower()

# Always use the live elections API for market scanning (read-only).
# No orders are placed, so there is no risk from pointing at the live endpoint.
KALSHI_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

# Order placement uses the demo API when KALSHI_ENV == "demo".
# Switch to live by setting KALSHI_ENV=live in your .env.
KALSHI_ORDER_BASE_URL = (
    "https://demo-api.kalshi.co/trade-api/v2"
    if KALSHI_ENV == "demo"
    else "https://api.elections.kalshi.com/trade-api/v2"
)

# --- Anthropic ---
ANTHROPIC_API_KEY: str = os.environ["ANTHROPIC_API_KEY"]

# --- Scanner thresholds ---
SCANNER_MIN_VOLUME = 200          # minimum open contracts
SCANNER_MAX_DAYS_TO_EXPIRY = 30   # maximum days until market closes
SCANNER_PRICE_MOVE_PCT = 10.0     # flag markets that moved >= this percent

# --- Paths ---
ROOT_DIR = Path(__file__).parent
DATA_DIR = ROOT_DIR / "data"
LOG_DIR = DATA_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
