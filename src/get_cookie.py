"""Cookie helper — extract X login cookies from user browser."""

import json
import os
from pathlib import Path

SESSION_FILE = Path("data/x_session.json")

# The two cookies X needs for auth
REQUIRED_COOKIES = ["auth_token", "ct0"]


def save_from_manual(auth_token: str, ct0: str = "") -> bool:
    """Save manually-provided cookies to session file."""
    cookies = [
        {
            "name": "auth_token",
            "value": auth_token,
            "domain": ".x.com",
            "path": "/",
            "httpOnly": True,
            "secure": True,
            "sameSite": "Lax",
        },
        {
            "name": "ct0",
            "value": ct0 or auth_token[:32],
            "domain": ".x.com",
            "path": "/",
            "httpOnly": False,
            "secure": True,
            "sameSite": "Lax",
        },
        {
            "name": "twid",
            "value": "u=",
            "domain": ".x.com",
            "path": "/",
            "httpOnly": False,
            "secure": True,
            "sameSite": "Lax",
        },
    ]
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SESSION_FILE, "w") as f:
        json.dump(cookies, f, indent=2)
    print(f"[Cookie] Session saved to {SESSION_FILE}")
    return True


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3:
        save_from_manual(sys.argv[1], sys.argv[2])
    elif len(sys.argv) == 2:
        save_from_manual(sys.argv[1])
    else:
        print("Usage: python get_cookie.py <auth_token> [ct0]")
        print("""
How to get auth_token:
1. Open x.com in your normal Chrome browser (make sure you are logged in)
2. Press F12 to open DevTools
3. Go to Application tab -> Cookies -> https://x.com
4. Find 'auth_token' in the list, copy its Value
5. Run: python get_cookie.py <paste_auth_token_here>
        """)
