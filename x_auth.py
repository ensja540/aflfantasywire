#!/usr/bin/env python3
"""
AFLFantasyWire — X (Twitter) OAuth helper
=========================================
One-time 3-legged OAuth 1.0a flow to mint an ACCESS TOKEN + SECRET for one X
account (the *poster*) against an App owned by a DIFFERENT account (the *payer*).

Why this exists
---------------
On X, billing and posting identity are separate:
  * WHO PAYS  = the developer account that owns the App (its API key & secret).
  * WHO POSTS = the user whose access token is used (whoever authorised the App).

We want tweets to post as @AFLFantasyWire while the bill lands on a personal,
credit-funded developer account. So:
  1. The personal (funded) account owns the App  -> use ITS API key & secret.
  2. @AFLFantasyWire authorises that App         -> this script captures the
     access token & secret that post as @AFLFantasyWire.

Prerequisites (in the PERSONAL account's developer portal, developer.x.com)
---------------------------------------------------------------------------
  * The App has "User authentication settings" enabled with OAuth 1.0a.
  * App permissions are set to **Read and Write** (NOT read-only) BEFORE you run
    this — tokens inherit the permission level at creation time.
  * A callback URL is set (any value works for the PIN flow, e.g.
    https://aflfantasywire.com/callback).

USAGE
  python x_auth.py
    Reads the App's API key/secret from .env (X_CONSUMER_KEY / X_CONSUMER_SECRET)
    if present, otherwise prompts for them. Walks you through authorising as
    @AFLFantasyWire and prints the four .env values to use.

  python x_auth.py --write
    Same, but also writes the four values straight into .env (a timestamped
    backup of the old .env is kept).
"""
import sys
from pathlib import Path
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(__file__).parent
ENV_PATH = BASE / ".env"

REQUEST_TOKEN_URL = "https://api.twitter.com/oauth/request_token"
AUTHORIZE_URL     = "https://api.twitter.com/oauth/authorize"
ACCESS_TOKEN_URL  = "https://api.twitter.com/oauth/access_token"
VERIFY_URL        = "https://api.twitter.com/2/users/me"


def load_env():
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def write_env(env):
    """Persist the four X_* keys into .env, preserving any other lines and
    keeping a timestamped backup of the previous file."""
    keys = ("X_CONSUMER_KEY", "X_CONSUMER_SECRET",
            "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET")
    existing = {}
    lines = []
    if ENV_PATH.exists():
        backup = ENV_PATH.with_name(f".env.bak-{datetime.now():%Y%m%d-%H%M%S}")
        backup.write_text(ENV_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"  backed up old .env -> {backup.name}")
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if "=" in stripped and not stripped.startswith("#"):
                k = stripped.split("=", 1)[0].strip()
                if k in keys:
                    existing[k] = True
                    lines.append(f"{k}={env[k]}")
                    continue
            lines.append(line)
    for k in keys:
        if k not in existing:
            lines.append(f"{k}={env[k]}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  wrote {', '.join(keys)} -> .env")


def main():
    from requests_oauthlib import OAuth1Session

    env = load_env()
    ck = env.get("X_CONSUMER_KEY", "").strip()
    cs = env.get("X_CONSUMER_SECRET", "").strip()

    print("=" * 60)
    print("  X OAuth helper — authorise @AFLFantasyWire against your")
    print("  personal (credit-funded) App.")
    print("=" * 60)
    print("\nThe API KEY / SECRET below must belong to the PERSONAL, funded")
    print("account's App (the one that pays). They are NOT @AFLFantasyWire's.\n")

    if ck and cs:
        print(f"Using API key from .env: {ck[:6]}…")
        use = input("Use this key? [Y/n] ").strip().lower()
        if use == "n":
            ck = cs = ""
    if not ck:
        ck = input("Paste the personal App's API Key (consumer key): ").strip()
    if not cs:
        cs = input("Paste the personal App's API Key Secret (consumer secret): ").strip()
    if not ck or not cs:
        print("Need both API key and secret. Aborting.")
        return

    # ── Step 1: request token (PIN / out-of-band flow) ──
    try:
        oauth = OAuth1Session(ck, client_secret=cs, callback_uri="oob")
        fetched = oauth.fetch_request_token(REQUEST_TOKEN_URL)
    except Exception as e:
        print(f"\nFailed to get a request token: {e}")
        print("Check the API key/secret and that OAuth 1.0a is enabled on the App.")
        return
    ro_key = fetched.get("oauth_token")
    ro_secret = fetched.get("oauth_token_secret")

    # ── Step 2: user authorises as @AFLFantasyWire ──
    auth_url = oauth.authorization_url(AUTHORIZE_URL)
    print("\n" + "-" * 60)
    print("STEP 1 — open this URL in a browser where you are logged in")
    print("         as @AFLFantasyWire (log out of the personal account")
    print("         first, or use a private window):\n")
    print("   " + auth_url)
    print("\nSTEP 2 — click 'Authorize app'. X shows a 7-digit PIN")
    print("         (or redirects to your callback URL with")
    print("         '?oauth_verifier=...'). Copy that value.")
    print("-" * 60)
    pin = input("\nPaste the PIN / oauth_verifier here: ").strip()
    if not pin:
        print("No PIN entered. Aborting.")
        return

    # ── Step 3: exchange for the access token (posts as @AFLFantasyWire) ──
    try:
        oauth = OAuth1Session(
            ck, client_secret=cs,
            resource_owner_key=ro_key, resource_owner_secret=ro_secret,
            verifier=pin,
        )
        tokens = oauth.fetch_access_token(ACCESS_TOKEN_URL)
    except Exception as e:
        print(f"\nToken exchange failed: {e}")
        print("Most common cause: a stale/typo'd PIN. Re-run and try again.")
        return
    at = tokens.get("oauth_token")
    ats = tokens.get("oauth_token_secret")
    screen_name = tokens.get("screen_name", "?")

    # ── Step 4: verify identity ──
    print(f"\nAuthorised as: @{screen_name}")
    if screen_name and screen_name.lower() != "aflfantasywire":
        print("  WARNING: that is NOT @AFLFantasyWire. You were probably logged")
        print("  in as the wrong account. Re-run in a private window logged in")
        print("  as @AFLFantasyWire.")

    print("\n" + "=" * 60)
    print("  SUCCESS — put these four lines in .env")
    print("=" * 60)
    print(f"X_CONSUMER_KEY={ck}")
    print(f"X_CONSUMER_SECRET={cs}")
    print(f"X_ACCESS_TOKEN={at}")
    print(f"X_ACCESS_TOKEN_SECRET={ats}")
    print("=" * 60)

    if "--write" in sys.argv:
        write_env({"X_CONSUMER_KEY": ck, "X_CONSUMER_SECRET": cs,
                   "X_ACCESS_TOKEN": at, "X_ACCESS_TOKEN_SECRET": ats})
        print("\nDone. Test with:  python tweet_bot.py --post --count=1")
    else:
        print("\nRe-run with  --write  to drop these into .env automatically,")
        print("or paste them in by hand. Then test:")
        print("  python tweet_bot.py --post --count=1")


if __name__ == "__main__":
    main()
