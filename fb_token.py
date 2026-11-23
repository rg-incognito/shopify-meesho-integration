"""Creates the Facebook Page token used by the bot (pipeline/facebook.py).

1. In Graph API Explorer (developers.facebook.com/tools/explorer) pick the "Noor Naksh Posts" app,
   add the permissions below, click "Generate Access Token" and approve The Noor Naksh Page.
2. Put that token in .env as FB_USER_TOKEN (plus FB_APP_ID / FB_APP_SECRET from App settings > Basic).
3. Run:  python fb_token.py
The short-lived user token is exchanged for a long-lived one, and the Page token taken from it does
not expire. It is saved to data/fb_token.json; FB_USER_TOKEN can then be removed from .env.
"""
import json
import sys
import time

import requests

from pipeline.config import DATA_DIR, env
from pipeline.facebook import API, TOKEN_FILE

NEEDED = ["pages_show_list", "pages_read_engagement", "pages_read_user_content", "pages_manage_posts",
          "pages_manage_engagement", "pages_messaging"]
PAGE_NAME = "The Noor Naksh"


def main() -> None:
    user, secret = env("FB_USER_TOKEN"), env("FB_APP_SECRET", required=False)
    if secret:   # short-lived Explorer token -> long-lived
        r = requests.get(f"{API}/oauth/access_token", params={
            "grant_type": "fb_exchange_token", "client_id": env("FB_APP_ID"), "client_secret": secret,
            "fb_exchange_token": user}, timeout=60).json()
        if "access_token" not in r:
            sys.exit(f"Token exchange failed: {r.get('error', r)}")
        long_user = r["access_token"]
    else:        # already extended (e.g. Access Token Debugger -> "Extend Access Token")
        info = requests.get(f"{API}/debug_token", params={"input_token": user, "access_token": user},
                            timeout=60).json().get("data", {})
        if not info.get("is_valid"):
            sys.exit(f"FB_USER_TOKEN is not valid: {info}")
        if info.get("expires_at") and info["expires_at"] - time.time() < 7 * 86400:
            sys.exit("FB_USER_TOKEN is short-lived: add FB_APP_SECRET to .env, or extend it in the "
                     "Access Token Debugger, then run again.")
        long_user = user

    perms = requests.get(f"{API}/me/permissions", params={"access_token": long_user}, timeout=60).json()
    granted = {p["permission"] for p in perms.get("data", []) if p.get("status") == "granted"}
    missing = [p for p in NEEDED if p not in granted]
    if missing:
        print(f"⚠️  Missing permissions (some features will fail): {', '.join(missing)}")

    pages = requests.get(f"{API}/me/accounts", params={"access_token": long_user, "fields": "id,name,access_token"},
                         timeout=60).json().get("data", [])
    page = next((p for p in pages if p["name"] == PAGE_NAME), pages[0] if len(pages) == 1 else None)
    if not page:
        sys.exit(f"Page '{PAGE_NAME}' not found. Pages on this token: {[p['name'] for p in pages]}")

    DATA_DIR.mkdir(exist_ok=True)
    TOKEN_FILE.write_text(json.dumps({"page_id": page["id"], "page_name": page["name"],
                                      "page_token": page["access_token"]}, indent=1), encoding="utf-8")
    print(f"✅ Saved a non-expiring token for '{page['name']}' (id {page['id']}) to {TOKEN_FILE}")
    print("   You can now delete FB_USER_TOKEN from .env and restart the bot.")


if __name__ == "__main__":
    main()
