import os

# Made-up account numbers, set before any module reads them (accounts.py), so the tests never
# depend on, or print, the real ones in .env.
os.environ["RH_AGENTIC_ACCOUNT"] = "900000001"
os.environ["RH_MAIN_ACCOUNT"] = "900000002"
