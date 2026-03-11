"""
fetch-absences.py
Opens Workday Team Absence Calendar in Chrome, waits for SSO login,
and extracts per-person absences for a sprint window.

Usage: python fetch-absences.py [sprint-start] [sprint-end-exclusive]
Example: python fetch-absences.py 2026-03-17 2026-03-31
  (end date is exclusive, matching Jira convention — first day of next sprint)
Output: absences.json  (per-person absence days within the sprint window)
"""

import sys
import json
import time
import asyncio
from pathlib import Path
from datetime import date, timedelta
from playwright.async_api import async_playwright

WORKDAY_URL = "https://www.myworkday.com/autodesk/d/task/2997$12517.htmld"
LOGIN_TIMEOUT_S = 180  # 3 minutes

sprint_start = sys.argv[1] if len(sys.argv) > 1 else "2026-03-17"
sprint_end   = sys.argv[2] if len(sys.argv) > 2 else "2026-03-31"  # exclusive (first day of next sprint)


def is_logged_in(url: str) -> bool:
    return (
        "myworkday.com/autodesk" in url
        and "login" not in url
        and "gateway" not in url
        and "auth" not in url
    )


def working_days_in_range(start: date, end: date) -> int:
    """Count Mon-Fri days in [start, end) — end is exclusive."""
    count = 0
    d = start
    while d < end:
        if d.weekday() < 5:
            count += 1
        d += timedelta(days=1)
    return count


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


async def submit_report(page) -> None:
    await page.wait_for_load_state("networkidle", timeout=30_000)
    for text in ["OK", "Run", "Submit"]:
        btn = page.get_by_role("button", name=text).first
        try:
            if await btn.is_visible(timeout=3_000):
                print(f">>> Clicking '{text}' button...")
                await btn.click()
                await page.wait_for_selector('[data-automation-id="calendarToolbar"]', timeout=20_000)
                print(">>> Calendar loaded.")
                return
        except Exception:
            pass
    print(">>> No OK button found — report may have loaded automatically.")
    await asyncio.sleep(3)


async def click_next_week(page) -> None:
    clicked = await page.evaluate("""() => {
        const btn = document.querySelector('[data-automation-id="nextMonthButton"]');
        if (btn) { btn.click(); return true; }
        const next = [...document.querySelectorAll('button')].find(b =>
            (b.getAttribute('aria-label') || '').toLowerCase().includes('next')
        );
        if (next) { next.click(); return true; }
        return false;
    }""")
    if clicked:
        await page.wait_for_load_state("networkidle", timeout=15_000)
        await asyncio.sleep(0.8)


async def get_current_week_range(page) -> tuple[date | None, date | None]:
    """Return the start/end date of the currently displayed calendar week."""
    text = await page.evaluate(
        "() => document.querySelector('[data-automation-id=\"dateRangeTitle\"]')?.innerText || ''"
    )
    import re
    from datetime import datetime
    # Cross-month format: "Mar 29 – Apr 4, 2026"
    m = re.search(r'(\w+ \d+)\s*[\u2013\u2014-]\s*(\w+ \d+),\s*(\d{4})', text)
    if m:
        try:
            year = m.group(3)
            week_start = datetime.strptime(f"{m.group(1)}, {year}", "%b %d, %Y").date()
            week_end = datetime.strptime(f"{m.group(2)}, {year}", "%b %d, %Y").date()
            return week_start, week_end
        except Exception:
            pass
    # Same-month format: "Mar 15 – 21, 2026"
    m = re.search(r'(\w+ \d+)\s*[\u2013\u2014-]\s*(\d+),\s*(\d{4})', text)
    if m:
        try:
            year = m.group(3)
            week_start = datetime.strptime(f"{m.group(1)}, {year}", "%b %d, %Y").date()
            day = int(m.group(2))
            week_end = week_start.replace(day=day)
            return week_start, week_end
        except Exception:
            pass
    return None, None


async def extract_week_absences(page) -> list[dict]:
    """Extract per-person absence events from the current calendar week view."""
    await page.wait_for_load_state("networkidle", timeout=30_000)
    await asyncio.sleep(1.2)

    data = await page.evaluate("""() => {
        const EMP_ID_RE = /\\(\\d{5,7}\\)/;
        const HOURS_RE = /^\\d+ Hours?$/;

        // --- Find absence event hour elements (innermost text nodes) ---
        const hourEls = [...document.querySelectorAll('*')].filter(el =>
            el.children.length <= 2 && HOURS_RE.test((el.innerText || '').trim())
        );
        if (!hourEls.length) return { people: [], events: [] };

        // --- Find the calendar grid body (first ancestor with 2+ employee IDs) ---
        let gridBody = null;
        for (let anc = hourEls[0].parentElement; anc; anc = anc.parentElement) {
            const ids = [...new Set((anc.innerText || '').match(/\\(\\d{5,7}\\)/g) || [])];
            if (ids.length >= 2) { gridBody = anc; break; }
        }

        // --- Find person name elements with y-positions ---
        // Use the smallest element containing an employee ID (most specific)
        const allIdEls = [...document.querySelectorAll('a, [data-automation-id], span')].filter(el => {
            const t = (el.innerText || '').trim();
            return EMP_ID_RE.test(t) && t.length < 80 && el.children.length <= 4;
        });
        const nameMap = {};
        for (const el of allIdEls) {
            const rect = el.getBoundingClientRect();
            if (rect.top < 50 || rect.height === 0) continue;
            const name = (el.innerText || '').replace(/\\s*\\(\\d+\\)/, '').trim();
            if (!name) continue;
            if (!nameMap[name] || rect.height < nameMap[name].height) {
                nameMap[name] = { name, midY: rect.top + rect.height / 2, height: rect.height };
            }
        }
        const people = Object.values(nameMap).sort((a, b) => a.midY - b.midY);

        // --- Match each event to a person by y-coordinate ---
        const events = [];
        for (const hourEl of hourEls) {
            const hours = (hourEl.innerText || '').trim();

            // Walk up to find the event's SVG container for bounding box
            let container = hourEl;
            for (let i = 0; i < 10; i++) {
                if (!container.parentElement || container.parentElement === gridBody) break;
                const pr = container.parentElement.getBoundingClientRect();
                if (pr.width > 400 && pr.height > 100) break;
                container = container.parentElement;
            }
            const rect = container.getBoundingClientRect();
            if (rect.height === 0) continue;
            const midY = rect.top + rect.height / 2;

            // Find status by walking up from hourEl
            let status = '';
            for (let check = hourEl; check && !status; check = check.parentElement) {
                const t = (check.innerText || '');
                if (t.includes('Approved')) status = 'Approved';
                else if (t.includes('Pending')) status = 'Pending';
                else if (t.includes('Denied')) status = 'Denied';
                if (check === gridBody) break;
            }

            // Match to nearest person
            let best = null, bestDist = Infinity;
            for (const p of people) {
                const dist = Math.abs(p.midY - midY);
                if (dist < bestDist) { bestDist = dist; best = p; }
            }

            events.push({ person: best ? best.name : 'Unknown', hours, status });
        }

        // Deduplicate
        const seen = new Set();
        const deduped = events.filter(e => {
            const key = `${e.person}|${e.hours}`;
            if (seen.has(key)) return false;
            seen.add(key);
            return true;
        });

        return { people: people.map(p => p.name), events: deduped };
    }""")

    print(f"  People: {data['people']}")
    print(f"  Events: {data['events']}")
    return data['events']


async def main():
    s_start = date.fromisoformat(sprint_start)
    s_end   = date.fromisoformat(sprint_end)  # exclusive
    sprint_days = working_days_in_range(s_start, s_end)
    print(f"Sprint: {sprint_start} to {sprint_end} (exclusive), {sprint_days} working days")

    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=False, slow_mo=30)
        context = await browser.new_context()
        page    = await context.new_page()

        try:
            await page.goto(WORKDAY_URL, wait_until="domcontentloaded", timeout=30_000)
            await wait_for_login(page)

            if "2997$12517" not in page.url:
                print(">>> Navigating to absence report...")
                await page.goto(WORKDAY_URL, wait_until="networkidle", timeout=30_000)

            await submit_report(page)

            # Navigate week by week until we've covered the full sprint window
            from collections import defaultdict
            hours_by_person: dict[str, int] = defaultdict(int)
            covered_up_to = date.min

            for week_num in range(1, 6):  # max 5 weeks
                week_start, week_end = await get_current_week_range(page)
                if week_start is None:
                    print(f">>> Could not determine week range, skipping")
                    break

                print(f"\n>>> Week {week_num}: {week_start} to {week_end}")

                # week_end from Workday is inclusive; convert to exclusive for comparisons
                week_end_excl = week_end + timedelta(days=1)

                # Skip if this week is entirely before the sprint
                if week_end_excl <= s_start:
                    print("  (before sprint - navigating forward)")
                    await click_next_week(page)
                    continue

                # Stop if this week is entirely after the sprint
                if week_start >= s_end:
                    break

                events = await extract_week_absences(page)
                for ev in events:
                    if ev.get("status") in ("Approved", ""):
                        h = int(ev["hours"].replace(" Hours", "").replace(" Hour", ""))
                        # Intersect event hours with sprint window in this week
                        # All ranges use exclusive end: [start, end)
                        overlap_start = max(week_start, s_start)
                        overlap_end   = min(week_end_excl, s_end)
                        overlap_days  = working_days_in_range(overlap_start, overlap_end)
                        week_work_days = working_days_in_range(week_start, week_end_excl)
                        if week_work_days > 0:
                            # Pro-rate: assume absence is evenly distributed across the week
                            sprint_h = round(h * overlap_days / week_work_days)
                            hours_by_person[ev["person"]] += sprint_h
                        else:
                            hours_by_person[ev["person"]] += h

                covered_up_to = week_end_excl
                if covered_up_to >= s_end:
                    break

                await click_next_week(page)

            # Convert to days
            print("\n=== SPRINT ABSENCES ===")
            absences = {}
            for person, hours in sorted(hours_by_person.items()):
                days = hours / 8
                absences[person] = {"hours": hours, "days": days}
                print(f"  {person}: {hours}h = {days:.1f} days")

            output = {
                "sprint_start": sprint_start,
                "sprint_end": sprint_end,
                "sprint_working_days": sprint_days,
                "absences": absences,
            }
            out_path = Path(__file__).parent / "absences.json"
            out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False))
            print(f"\n>>> Saved to {out_path}")

        finally:
            print("\n>>> Done. Closing browser in 3 seconds...")
            await asyncio.sleep(3)
            await browser.close()


asyncio.run(main())
