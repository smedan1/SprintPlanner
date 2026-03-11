"""
fetch-team.py
Opens Workday "Manage My Team" in Chrome, waits for SSO login,
and extracts team member names directly from the page.

Usage: python fetch-team.py
Output: Updates team-config.json with team member names.
"""

import json
import time
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

WORKDAY_URL = "https://www.myworkday.com/autodesk/d/task/23748$5.htmld"
LOGIN_TIMEOUT_S = 180  # 3 minutes
SCRIPT_DIR = Path(__file__).parent


def is_logged_in(url: str) -> bool:
    return (
        "myworkday.com/autodesk" in url
        and "login" not in url
        and "gateway" not in url
        and "auth" not in url
    )


def load_existing_config() -> dict:
    """Load existing team-config.json, or return empty dict."""
    path = SCRIPT_DIR / "team-config.json"
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def save_config(cfg: dict) -> None:
    path = SCRIPT_DIR / "team-config.json"
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
    print(f">>> Saved config to {path}")


async def wait_for_login(page) -> None:
    print("\n>>> Browser opened. Please log in to Workday (SSO).")
    print(f">>> Waiting up to {LOGIN_TIMEOUT_S // 60} minutes...\n")
    deadline = time.time() + LOGIN_TIMEOUT_S
    while time.time() < deadline:
        await asyncio.sleep(1.5)
        try:
            if is_logged_in(page.url):
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=3_000)
                except Exception:
                    pass
                print(">>> Login detected. Proceeding...\n")
                return
        except Exception:
            pass
    raise TimeoutError("Timed out waiting for SSO login.")


async def extract_team_names(page) -> list[str]:
    """Extract team member names from the Manage My Team page DOM."""
    return await page.evaluate(r"""() => {
        // Workday renders employee names as "FirstName LastName (12345)"
        const EMP_ID_RE = /\(\d{5,7}\)/;

        // Try specific Workday automation-id selectors first, then broader ones
        const selectors = [
            'a, [data-automation-id*="promptOption"], [data-automation-id*="compositeLink"]',
            '[data-automation-id] a, td a, span a',
        ];

        const names = new Set();
        for (const sel of selectors) {
            const els = [...document.querySelectorAll(sel)].filter(el => {
                const t = (el.innerText || '').trim();
                return EMP_ID_RE.test(t) && t.length < 80 && el.children.length <= 4;
            });
            for (const el of els) {
                const raw = (el.innerText || '').trim();
                // Remove employee ID and any extra whitespace/newlines
                const name = raw.replace(/\s*\(\d+\)/, '').replace(/\n.*/s, '').trim();
                if (name && name.length > 2) {
                    names.add(name);
                }
            }
            if (names.size > 0) break;  // Stop if first selector worked
        }

        // Broadest fallback: any element with employee ID pattern
        if (names.size === 0) {
            const all = [...document.querySelectorAll('*')].filter(el => {
                const t = (el.innerText || '').trim();
                return EMP_ID_RE.test(t) && t.length < 60
                    && el.children.length <= 2 && !t.includes('\n');
            });
            for (const el of all) {
                const raw = (el.innerText || '').trim();
                const name = raw.replace(/\s*\(\d+\)/, '').trim();
                if (name && name.length > 2) {
                    names.add(name);
                }
            }
        }

        return [...names].sort();
    }""")


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=False, slow_mo=30)
        context = await browser.new_context()
        page = await context.new_page()

        try:
            print(">>> Opening Workday team list...")
            await page.goto(WORKDAY_URL, wait_until="domcontentloaded", timeout=30_000)
            await wait_for_login(page)

            # If SSO redirected away from the team list, navigate back
            if "23748$5" not in page.url:
                print(">>> Navigating to team list...")
                await page.goto(WORKDAY_URL, wait_until="domcontentloaded", timeout=30_000)

            print(">>> Waiting for team page to load...")
            await page.wait_for_load_state("networkidle", timeout=30_000)
            await asyncio.sleep(3)

            # Extract team names from the DOM
            print(">>> Extracting team member names from page...")
            names = await extract_team_names(page)

            # Retry with wait if nothing found
            if not names:
                print(">>> No names found on first attempt. Waiting for page to fully render...")
                await asyncio.sleep(10)
                await page.wait_for_load_state("networkidle", timeout=15_000)
                names = await extract_team_names(page)

            # Final fallback: ask user to ensure page is ready
            if not names:
                print(">>> Still no names found.")
                print(">>> Please ensure the team list is fully visible (scroll down if needed).")
                print(">>> Waiting 30 seconds for manual adjustment...")
                await asyncio.sleep(30)
                names = await extract_team_names(page)

            if not names:
                print(">>> ERROR: Could not extract team member names from the page.")
                return

            print(f"\n=== TEAM MEMBERS ({len(names)}) ===")
            for name in names:
                print(f"  - {name}")

            # Update config
            cfg = load_existing_config()
            cfg['team'] = names
            save_config(cfg)

        finally:
            print("\n>>> Done. Closing browser in 3 seconds...")
            await asyncio.sleep(3)
            await browser.close()


asyncio.run(main())
