# SprintPlanner

Automates sprint capacity planning for Jira teams. Replaces manual Excel spreadsheets with a drag-and-drop web UI that syncs to Jira in real time.

## What It Does

Pulls the upcoming sprint from Jira, cross-references team availability (absences, holidays, training, etc.), and calculates realistic story point capacity per person and for the team overall.

## Capacity Formula

```
Sprint Capacity (per person) = (Working Days × Efficiency %) - Deductions

Team Net Capacity (SP) = Sum of individual capacities - Unscheduled buffer
```

Defaults: 70% efficiency, 10 working days per sprint. 1 story point = 1 day of work.

### Per-person efficiency overrides
Configured in `team-config.json` under `efficiency`. Examples:
- **Jonathan Bouchard**: 0% — inner-sourcing for another team
- **Mauro Blanco**: 10% — assigned to other projects

Efficiency is also editable directly in the capacity table UI and auto-saved to config.

### Date arithmetic
**Never assume the weekday of a date.** Always compute it (e.g., `python -c "from datetime import date; print(date(YYYY,M,D).strftime('%A'))"`). Sprints start on Tuesdays.

### Deduction definitions
- **Absence**: vacation, sick days, OOO
- **Events**: team events that take time away from sprint work
- **Hackathon**: company hackathon days
- **Training**: learning & development time
- **PA (Promotion Analysis)**: optional; 1-day duty monitoring the staging build for promotion decisions. Enabled/disabled per team in settings.
- **PR (Pull Request Review)**: optional; cross-team PR review rotation duty. Deduction per rotation is configurable: half day (0.5, default) or full day (1.0) via Settings > PR Duty Weight. Schedule read from Confluence. Enabled/disabled per team in settings.
- **KTLO**: Keep The Lights On — operational/maintenance work
- **Unscheduled**: team-level buffer for mid-sprint critical priority unplanned work; default **5 SP per sprint**

## Configuration

All team-specific settings are stored in `team-config.json`:

```json
{
  "board_id": 17259,
  "board_url": "https://jira.autodesk.com/secure/RapidBoard.jspa?rapidView=17259",
  "team": ["Person A", "Person B", ...],
  "efficiency": {
    "default": 70,
    "Person A": 50
  },
  "pa_enabled": true,
  "pa_confluence_url": "https://your-instance.atlassian.net/wiki/...",
  "pr_enabled": true,
  "pr_confluence_url": "https://autodesk.atlassian.net/wiki/x/fJH5L",
  "pr_duty_weight": 0.5,
  "confluence_account_ids": {}
}
```

Settings are managed via the gear icon in the UI header. On first run with no team configured, the settings modal opens automatically.

### Team refresh from Workday
`fetch-team.py` opens the Workday "Manage My Team" page in headed Chrome, waits for SSO, and scrapes team member names from the DOM. Triggered via Settings > "Refresh from Workday".

## Integrations

### Absence scraper (Workday)
- Script: `fetch-absences.py` — Python Playwright, headed Chrome, manual SSO login
- Run: `python fetch-absences.py YYYY-MM-DD YYYY-MM-DD` (sprint start and end, end exclusive)
- Output: `absences.json` — absence hours, days, and exact dates per team member within the sprint window
- Workday Team Absence Calendar URL: `https://www.myworkday.com/autodesk/d/task/2997$12517.htmld`
- Also triggered automatically from the startup overlay or after team configuration
- **How it works**: navigates week by week through the sprint window, clicks each absence event block to open the "Absence Entries" popup, scrapes exact date ranges and duration per day, then filters to the sprint window
- Deduplicates entries across weeks (same absence request appears in popups regardless of which week's block is clicked)
- No pro-rating — uses exact dates from Workday popups

### Team scraper (Workday)
- Script: `fetch-team.py` — Python Playwright, headed Chrome, manual SSO login
- Direct URL: `https://www.myworkday.com/autodesk/d/task/23748$5.htmld`
- Output: Updates `team-config.json` with team member names
- Triggered via Settings > "Refresh from Workday"

### Jira (via MCP — primary integration)
- MCP server: `mcp-jira` (configured in `.mcp.json`, which is gitignored)
- Docker image: `ghcr.io/sooperset/mcp-atlassian:latest`
- Jira instance: `https://jira.autodesk.com`
- **Direct REST API is blocked by Autodesk corporate policy — always use MCP tools**

### Confluence (via MCP — for PA and PR schedules)
- MCP server: `confluence-wiki-api` (configured in `~/.claude.json`, OAuth via `mcp.atlassian.com`)
- PA schedule page URL is configured in `team-config.json` (`pa_confluence_url`)
- For each sprint, check the schedule for entries whose date falls within `[start_date, end_date)` — reserve 1 day PA per person matched
- PA is optional — can be disabled in settings for teams that don't use it
- PR review schedule page URL is configured in `team-config.json` (`pr_confluence_url`)
- PR page has a 3-column table (Team, Person, Date). Only "Gemini" rows are included. Deduction per rotation configurable via `pr_duty_weight` (0.5 = half day, 1 = full day)
- PR is optional — can be disabled in settings for teams that don't use it

## Jira Boards & Sprints

- Board ID and URL are configured in `team-config.json` (set via Settings)
- Sprint naming pattern: `YYYY-MM-DD <BoardName>` (2-week cadence)
- Backlog sprints (e.g. "Stretch Goal") are selected in Settings

### How to identify the sprint to plan

1. Call `jira_get_sprints_from_board` on the configured board with `state=active` → that's the **current sprint**
2. Call with `state=future` → find the sprint named `YYYY-MM-DD <name>` (ignore backlog sprints) — that's the **sprint to plan**
3. The sprint to plan's `start_date` and `end_date` define the window. **`end_date` is exclusive** (first day of next sprint). Working days = Mon–Fri within `[start_date, end_date)` = typically 10 days
4. Use the sprint-to-plan's ID to pull its issues; also pull from selected backlog sprints

## Holiday List

Stored in `holidays.json`. Covers Canada + Quebec holidays for **2026**.

**Last refreshed:** 2026-03-06
**Needs refresh for 2027:** remind user in ~December 2026

### How to refresh the holiday list

1. Open Workday: https://www.myworkday.com/autodesk/d/home.htmld
2. In the search bar, type **"Holiday Calendar Report"** and select it
3. Set the year to the upcoming year and click **OK**
4. Click **Export** → **Excel (.xlsx)**
5. Tell Claude: *"Read the holiday report at: <path-to-file>"*
6. Claude parses the sheet and extracts **Canada (no region)** + **Quebec** rows
7. Claude updates `holidays.json`

**What to extract:** Columns: Country/Region, Date, Holiday Name. Filter rows where Region is blank (Canada federal) or "Quebec".

## Sprint Server (drag-and-drop Jira sync)

`sprint-server.py` is a lightweight local HTTP server that bridges `sprint-plan.html` to Jira in real time.

**To use:**
1. `python sprint-server.py` — runs on http://localhost:5000, reads token from `.mcp.json`
2. Open `sprint-plan.html` in Chrome
3. A startup overlay checks server, Jira, Docker, and Confluence health — tasks load automatically once all are green

**What the page does:**
- Tasks are loaded dynamically on page open (not baked into the HTML) — the server fetches them from Jira
- **Closed/resolved/done issues and epics are filtered out** from the task list
- **Drag rows** between backlog sections and Sprint Commitment to plan the sprint
- **Right-click context menu**: right-click a task to move it to any section without dragging
- **Editable SP**: type a new value in the SP column; SP=0 means 4h in Jira timetracking
- **Editable Assignee**: dropdown restricted to team members
- **Editable Priority**: custom dropdown with Jira priority icons (Showstopper, Critical, Major, Minor, None)
- **Person detail view**: click a person's name in the capacity table to expand an inline detail row showing vacation date ranges, PA/PR schedule dates, and committed tasks
- **Editable Efficiency %**: per-person, recalculates capacity in real time, auto-saved to config
- **Editable Unscheduled Buffer**: team-level buffer, recalculates net capacity in real time
- **Draggable backlog sections**: drag backlog headers to reorder; order persists in `backlog-prefs.json`
- **Pending changes persist**: all pending edits and moves survive page reloads (stored in localStorage)
- **Discard All**: reverts all pending changes, moving tasks back to their original backlogs
- **Settings gear**: configure board URL, team members, PA toggle, default unscheduled buffer, backlog sprint selection
- **↻ Refresh Tasks**: fetches latest SP, assignees, priorities, and any new tasks from Jira
- **Apply to Jira**: syncs all pending changes (sprint moves, SP edits, assignee edits, priority edits) to Jira

**Jira fields written by Apply to Jira:**
- Sprint move → `POST /rest/agile/1.0/sprint/{id}/issue`
- SP → `customfield_10130` + `timetracking.originalEstimate` (1 SP = 1 day); SP=0 → `4h`; remaining estimate = max(0, original − logged)
- Assignee → resolved via user search (`username` param, Jira Server API)
- Priority → `priority.id` or `priority.name`

**Layout:** Left and right columns scroll independently. Sprint Commitment card has a blue left border accent for visibility when scrolled.

**Without the server:** drag-and-drop and field edits still work locally; failed syncs queue in a collapsible "Pending Jira Changes" panel. Paste the list to Claude to apply via MCP.

## Constraints

- Never commit `.mcp.json` — it contains the Jira personal token
- Never commit `team-config.json` — contains team-specific settings
- Jira REST API is corporate-blocked; MCP is the only route
- Tech stack: Python + Playwright (`channel="chrome"`) for Workday scraping; npm/Node.js blocked by corporate network

## File Reference

| File | Purpose |
|---|---|
| `sprint-plan.html` | Interactive sprint planner UI (single-page app) |
| `sprint-server.py` | Local HTTP server bridging UI to Jira |
| `fetch-absences.py` | Workday absence scraper (Playwright) |
| `fetch-team.py` | Workday team list scraper (Playwright) |
| `auth-confluence.py` | Confluence OAuth session helper |
| `team-config.json` | Team settings (board, members, efficiency, PA config) |
| `holidays.json` | Holiday calendar (Canada + Quebec) |
| `absences.json` | Cached absence data for current sprint |
| `pa-schedule.json` | Cached PA schedule for current sprint |
| `pr-schedule.json` | Cached PR review schedule for current sprint |
| `backlog-prefs.json` | Saved backlog sprint selections |
| `.mcp.json` | MCP server config with Jira token (gitignored) |
