"""Settings, loaded from .env next to the project root."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _get(key, default=""):
    return os.environ.get(key, default).strip()


# --- who am I -------------------------------------------------------------
NEO_ID = _get("NEO_ID")
REG_NO = _get("REG_NO")
FULL_NAME = _get("FULL_NAME")
ME = {"neo_id": NEO_ID, "reg_no": REG_NO, "name": FULL_NAME}

# --- what to watch --------------------------------------------------------
# Comma separated: the CDC sends from more than one address, and a shortlist
# that arrives from the wrong one is a shortlist missed.
WATCH_SENDERS = [
    a.strip() for a in
    _get("WATCH_SENDER", "students.cdc2027@vitap.ac.in").split(",")
    if a.strip()
]
GMAIL_LABEL = _get("GMAIL_LABEL", "CDC")

# --- delivery -------------------------------------------------------------
TELEGRAM_BOT_TOKEN = _get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _get("TELEGRAM_CHAT_ID")

# Shared-bot registration. Friends send /start <code> once; without a code
# set, nobody but the owner can register.
INVITE_CODE = _get("INVITE_CODE")

# --- optional LLM for company/round off the subject line ------------------
NIM_API_KEY = _get("NIM_API_KEY")
NIM_BASE_URL = _get("NIM_BASE_URL", "https://integrate.api.nvidia.com/v1")
NIM_MODEL = _get("NIM_MODEL", "meta/llama-3.3-70b-instruct")

# --- runtime --------------------------------------------------------------
MODE = _get("MODE", "poll")                      # poll | push
POLL_INTERVAL = int(_get("POLL_INTERVAL", "120"))
GCP_PROJECT = _get("GCP_PROJECT")
PUBSUB_SUBSCRIPTION = _get("PUBSUB_SUBSCRIPTION", "cdc-watch-sub")
PUBSUB_TOPIC = _get("PUBSUB_TOPIC", "cdc-watch")

DB_PATH = Path(_get("DB_PATH", str(ROOT / "state.db")))
TOKEN_PATH = Path(_get("TOKEN_PATH", str(ROOT / "token.json")))
CREDENTIALS_PATH = Path(_get("CREDENTIALS_PATH", str(ROOT / "credentials.json")))

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
