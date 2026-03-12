"""
fetch-absences.py
Opens Workday Team Absence Calendar in Chrome, waits for SSO login,
and extracts per-person absences for a sprint window.

Usage: python fetch-absences.py [sprint-start] [sprint-end-exclusive]
Example: python fetch-absences.py 2026-03-17 2026-03-31
  (end date is exclusive, matching Jira convention — first day of next sprint)
Output: absences.json  (per-person absence days within the sprint window)
"""

import re
import sys
import json
import time
import asyncio
from pathlib import Path
from datetime import date, datetime, timedelta
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


def parse_absence_entries(raw_entries: list[dict]) -> list[dict]:
    """Parse popup table entries into {start, end, hours_per_day} dicts."""
    results = []
    for entry in raw_entries:
        date_text = entry.get('dates', '').strip()
        duration_text = entry.get('duration', '').strip()
        if not date_text or not duration_text:
            continue
        # Parse hours per day from "8 Hours" or "4 Hours"
        dur_m = re.search(r'(\d+)', duration_text)
        if not dur_m:
            continue
        hours_per_day = int(dur_m.group(1))

        # Parse date(s)
        # Range: "Mon, Mar 16, 2026 – Wed, Mar 18, 2026" (en-dash or hyphen)
        # Single: "Fri, Mar 6, 2026"
        try:
            if '\u2013' in date_text or '\u2014' in date_text:
                # en-dash or em-dash
                parts = re.split(r'\s*[\u2013\u2014]\s*', date_text, maxsplit=1)
                start = datetime.strptime(parts[0].strip(), "%a, %b %d, %Y").date()
                end = datetime.strptime(parts[1].strip(), "%a, %b %d, %Y").date()
            elif date_text.count(',') >= 3:
                # Multiple commas suggest a range with regular hyphen: "Mon, Mar 16, 2026 - Wed, Mar 18, 2026"
                parts = re.split(r'\s*-\s*(?=[A-Z])', date_text, maxsplit=1)
                if len(parts) == 2:
                    start = datetime.strptime(parts[0].strip(), "%a, %b %d, %Y").date()
                    end = datetime.strptime(parts[1].strip(), "%a, %b %d, %Y").date()
                else:
                    start = end = datetime.strptime(date_text.strip(), "%a, %b %d, %Y").date()
            else:
                # Single day
                start = end = datetime.strptime(date_text.strip(), "%a, %b %d, %Y").date()
            results.append({"start": start, "end": end, "hours_per_day": hours_per_day})
        except ValueError as e:
            print(f"    Warning: could not parse date '{date_text}': {e}")
            continue
    return results


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


async def find_event_blocks(page) -> list[dict]:
    """Find absence event blocks on the calendar and return their person + click coordinates."""
    await page.wait_for_load_state("networkidle", timeout=30_000)
    await asyncio.sleep(1.2)

    data = await page.evaluate("""() => {
        const EMP_ID_RE = /\\(\\d{5,7}\\)/;
        const HOURS_RE = /^\\d+ Hours?$/;

        // --- Find absence event hour elements ---
        const hourEls = [...document.querySelectorAll('*')].filter(el =>
            el.children.length <= 2 && HOURS_RE.test((el.innerText || '').trim())
        );
        if (!hourEls.length) return { people: [], events: [] };

        // --- Find the calendar grid body ---
        let gridBody = null;
        for (let anc = hourEls[0].parentElement; anc; anc = anc.parentElement) {
            const ids = [...new Set((anc.innerText || '').match(/\\(\\d{5,7}\\)/g) || [])];
            if (ids.length >= 2) { gridBody = anc; break; }
        }

        // --- Find person name elements with y-positions ---
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

        // --- Match each event to a person, return click coordinates ---
        const events = [];
        const seen = new Set();
        for (const hourEl of hourEls) {
            const hours = (hourEl.innerText || '').trim();

            // Walk up to find the event container
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

            // Find status
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

            // Deduplicate
            const key = `${best ? best.name : 'Unknown'}|${hours}`;
            if (seen.has(key)) continue;
            seen.add(key);

            events.push({
                person: best ? best.name : 'Unknown',
                hours,
                status,
                x: rect.left + rect.width / 2,
                y: rect.top + rect.height / 2,
            });
        }

        return { people: people.map(p => p.name), events };
    }""")

    print(f"  People: {data['people']}")
    print(f"  Events: {len(data['events'])} blocks found")
    for ev in data['events']:
        print(f"    {ev['person']}: {ev['hours']} ({ev['status']})")
    return data['events']


async def scrape_popup_entries(page) -> list[dict]:
    """Scrape the absence entries table from an open popup."""
    await asyncio.sleep(0.8)
    entries = await page.evaluate("""() => {
        const DATE_RE = /\\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\\b.*\\d{4}/;
        const HOURS_RE = /^\\d+\\s+Hours?$/;
        const entries = [];

        // Strategy 1: look in table rows
        const tables = document.querySelectorAll('table');
        for (const table of tables) {
            const rows = table.querySelectorAll('tr');
            for (const row of rows) {
                const cells = row.querySelectorAll('td');
                if (cells.length < 2) continue;
                let dateCell = null, durationCell = null;
                for (const cell of cells) {
                    const t = (cell.innerText || '').trim();
                    if (DATE_RE.test(t)) dateCell = t;
                    if (HOURS_RE.test(t)) durationCell = t;
                }
                if (dateCell && durationCell) {
                    entries.push({ dates: dateCell, duration: durationCell });
                }
            }
        }
        if (entries.length > 0) return entries;

        // Strategy 2: look in any popup/dialog container for text nodes matching date + hours
        const popups = document.querySelectorAll('[data-automation-id="wd-popup"], .wd-popup, [role="dialog"], [data-automation-id="absenceEntriesPopup"]');
        for (const popup of popups) {
            const allEls = popup.querySelectorAll('*');
            let lastDate = null;
            for (const el of allEls) {
                if (el.children.length > 2) continue;
                const t = (el.innerText || '').trim();
                if (DATE_RE.test(t) && t.length < 100) {
                    lastDate = t;
                } else if (HOURS_RE.test(t) && lastDate) {
                    entries.push({ dates: lastDate, duration: t });
                    lastDate = null;
                }
            }
        }
        if (entries.length > 0) return entries;

        // Strategy 3: brute-force scan the whole page for newly appeared elements
        // Look for all visible elements with date + hours near each other
        const allEls = document.querySelectorAll('*');
        const dateEls = [];
        const hoursEls = [];
        for (const el of allEls) {
            if (el.children.length > 2) continue;
            const t = (el.innerText || '').trim();
            const r = el.getBoundingClientRect();
            if (r.height === 0) continue;
            if (DATE_RE.test(t) && t.length < 100) dateEls.push({ text: t, y: r.top });
            if (HOURS_RE.test(t)) hoursEls.push({ text: t, y: r.top });
        }
        // Match date/hours elements by proximity (within 30px vertically)
        for (const dEl of dateEls) {
            let best = null, bestDist = 30;
            for (const hEl of hoursEls) {
                const dist = Math.abs(dEl.y - hEl.y);
                if (dist < bestDist) { bestDist = dist; best = hEl; }
            }
            if (best) entries.push({ dates: dEl.text, duration: best.text });
        }
        return entries;
    }""")
    return entries


async def click_event_and_get_entries(page, ev: dict) -> list[dict]:
    """Click an absence event block, scrape popup entries, close popup."""
    try:
        # Count existing table cells before clicking so we can detect the popup
        pre_count = await page.evaluate("() => document.querySelectorAll('table td').length")
        await page.mouse.click(ev['x'], ev['y'])

        # Wait for popup: either new table cells appear, or a popup/dialog element shows up
        try:
            await page.wait_for_function(
                f"() => document.querySelectorAll('table td').length > {pre_count} "
                f"|| document.querySelector('[data-automation-id=\"wd-popup\"]') "
                f"|| document.querySelector('.wd-popup') "
                f"|| document.querySelector('[role=\"dialog\"]')",
                timeout=5000,
            )
        except Exception:
            # Popup may have opened differently, try a short wait
            await asyncio.sleep(2)

        entries = await scrape_popup_entries(page)

        # Close popup with Escape
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.5)

        return entries
    except Exception as e:
        print(f"    Warning: failed to scrape popup for {ev['person']}: {e}")
        # Try to close any open popup
        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.3)
        except Exception:
            pass
        return []


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
            hours_by_person: dict[str, float] = defaultdict(float)
            dates_by_person: dict[str, set] = defaultdict(set)
            # Track seen entries globally to avoid double-counting across weeks
            seen_entries: set[tuple] = set()  # (person, start_iso, end_iso)
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

                event_blocks = await find_event_blocks(page)

                for ev in event_blocks:
                    if ev.get("status") not in ("Approved", ""):
                        continue

                    print(f"  > Clicking {ev['person']} ({ev['hours']})...")
                    raw_entries = await click_event_and_get_entries(page, ev)
                    print(f"    Popup entries: {raw_entries}")

                    parsed = parse_absence_entries(raw_entries)
                    for entry in parsed:
                        # Dedup: skip if we've already processed this exact entry
                        entry_key = (ev['person'], entry['start'].isoformat(), entry['end'].isoformat())
                        if entry_key in seen_entries:
                            continue
                        seen_entries.add(entry_key)

                        # Enumerate each working day in the entry's range
                        d = entry['start']
                        while d <= entry['end']:
                            if d.weekday() < 5 and d >= s_start and d < s_end:
                                hours_by_person[ev['person']] += entry['hours_per_day']
                                dates_by_person[ev['person']].add(d.isoformat())
                            d += timedelta(days=1)

                covered_up_to = week_end_excl
                if covered_up_to >= s_end:
                    break

                await click_next_week(page)

            # Convert to days
            print("\n=== SPRINT ABSENCES ===")
            absences = {}
            for person, hours in sorted(hours_by_person.items()):
                days = hours / 8
                person_dates = sorted(dates_by_person.get(person, []))
                absences[person] = {"hours": hours, "days": days, "dates": person_dates}
                print(f"  {person}: {hours}h = {days:.1f} days ({len(person_dates)} dates)")

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
