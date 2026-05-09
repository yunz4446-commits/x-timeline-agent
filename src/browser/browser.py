"""Playwright browser management for X.com."""

import json
import logging
from pathlib import Path

from playwright.sync_api import sync_playwright, Browser, Page

logger = logging.getLogger(__name__)

USER_DATA_DIR = Path("data/browser_profile")
SESSION_FILE = Path("data/x_session.json")


class XBrowser:
    """Launch a persistent browser with X login session."""

    def __init__(self, headless: bool = True, timeout: int = 60000):
        self._headless = headless
        self._timeout = timeout
        self._playwright = None
        self._browser: Browser | None = None
        self._page: Page | None = None

    def start(self) -> "XBrowser":
        USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        
        # Use persistent context to keep login session
        context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR),
            headless=self._headless,
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="Asia/Shanghai",
            args=[
                "--disable-gpu",
                "--disable-software-rasterizer",
                "--disable-dev-shm-usage",
                f"--disk-cache-dir={USER_DATA_DIR / 'cache'}",
                "--disable-extensions",
            ],
        )
        self._browser = context.browser
        self._page = context.pages[0] if context.pages else context.new_page()
        self._page.set_default_timeout(self._timeout)
        
        # Stealth injection
        self._page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
        """)
        
        return self

    def stop(self) -> None:
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
        if self._playwright:
            try:
                self._playwright.stop()
            except Exception:
                pass

    @property
    def page(self) -> Page:
        if not self._page:
            raise RuntimeError("Browser not started. Call start() first.")
        return self._page

    def is_logged_in(self) -> bool:
        """Check if already logged into X."""
        self.page.goto("https://x.com/home", wait_until="domcontentloaded")
        self.page.wait_for_timeout(5000)
        url = self.page.url.lower()
        # If redirected to login page, not logged in
        if "login" in url:
            return False
        # Look for timeline elements or any main content
        for sel in ['[data-testid="tweet"]', '[data-testid="primaryColumn"]',
                     '[aria-label="Timeline: Your Home Timeline"]', 'nav[aria-label="Primary"]']:
            if self.page.locator(sel).count() > 0:
                return True
        # If not on login page, likely logged in (X may lazy-load content)
        if "home" in url or "x.com/home" in url:
            return True
        return False

    def login(self) -> bool:
        """Open browser window for manual login, then save session."""
        print("[Browser] A Chrome window will open. Please log into X.com.")
        print("[Browser] After login, the window will close automatically.")
        print("[Browser] Waiting for you to login (timeout: 120 seconds)...")
        
        self.page.goto("https://x.com/login", wait_until="domcontentloaded")
        
        import time
        for i in range(60):
            time.sleep(2)
            try:
                url = self.page.url.lower()
                tweets_count = self.page.locator('[data-testid="tweet"]').count()
                if tweets_count > 0:
                    logger.info("Login detected via timeline tweets!")
                    self._save_session()
                    print("[Browser] Login successful! Session saved.")
                    return True
                if "home" in url or "explore" in url or "notifications" in url:
                    logger.info("Login detected via URL: %s", url)
                    self._save_session()
                    print("[Browser] Login successful! Session saved.")
                    return True
            except Exception:
                pass
            if i % 10 == 0:
                print(f"[Browser] Still waiting... ({i*2}s elapsed)")
        
        logger.warning("Login not detected within timeout")
        return False

    def _save_session(self) -> None:
        """Save cookies to file."""
        cookies = self.page.context.cookies()
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(SESSION_FILE, 'w') as f:
            json.dump(cookies, f, indent=2)
        logger.info("Session saved to %s", SESSION_FILE)

    def load_session(self) -> bool:
        """Load saved cookies into browser context."""
        if not SESSION_FILE.exists():
            return False
        try:
            with open(SESSION_FILE) as f:
                cookies = json.load(f)
            self.page.goto("https://x.com", wait_until="domcontentloaded")
            self.page.context.add_cookies(cookies)
            logger.info("Session loaded from %s", SESSION_FILE)
            return True
        except Exception as exc:
            logger.warning("Failed to load session: %s", exc)
            return False
