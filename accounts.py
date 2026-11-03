"""
accounts.py — the Robinhood account numbers. They come from the environment, never the code:

  RH_AGENTIC_ACCOUNT   the agentic account the system may trade (and read)
  RH_MAIN_ACCOUNT      the main account, read only for the weekly LONG TERM weights

In GitHub Actions they are repository secrets, so they are also masked in job logs. Unset, each is
"", which matches no account, so rh_client and rh_trader refuse every account call (fail closed).
"""

import os

from dotenv import load_dotenv

load_dotenv()

AGENTIC_ACCOUNT = os.environ.get("RH_AGENTIC_ACCOUNT", "").strip()
MAIN_ACCOUNT = os.environ.get("RH_MAIN_ACCOUNT", "").strip()
