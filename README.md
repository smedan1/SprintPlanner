# SprintPlanner

A drag-and-drop sprint capacity planning tool that syncs with Jira in real time. Replaces manual Excel spreadsheets by automatically pulling sprint data, team availability, and holidays to calculate realistic story point capacity.

## Quick Start

### Prerequisites

- **Python 3.10+**
- **Playwright** for Python: `pip install playwright && playwright install chromium`
- **Docker Desktop** (for the Jira MCP server)
- **Chrome** (Playwright uses your local Chrome for Workday SSO)
- A **Jira personal access token** for your Jira instance

### 1. Configure Jira access

Create `.mcp.json` in the project root (this file is gitignored):

```json
{
  "mcpServers": {
    "mcp-jira": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "-e", "JIRA_URL",
        "-e", "JIRA_PERSONAL_TOKEN",
        "-e", "JIRA_SSL_VERIFY",
        "ghcr.io/sooperset/mcp-atlassian:latest"
      ],
      "env": {
        "JIRA_URL": "https://your-jira-instance.com",
        "JIRA_PERSONAL_TOKEN": "your-token-here",
        "JIRA_SSL_VERIFY": "true"
      }
    }
  }
}
```

To generate a Jira personal access token: Jira > Profile > Personal Access Tokens > Create token.

### 2. Start the server

```bash
python sprint-server.py
```

This starts a local HTTP server on http://localhost:5000 that bridges the UI to Jira.

### 3. Open the planner

Open `sprint-plan.html` in Chrome. A startup overlay checks that all services are healthy:

- **Server**: `sprint-server.py` is running
- **Jira**: reachable via the MCP Docker container
- **Docker**: Docker Desktop is running
- **Confluence**: authenticated (optional, for PA schedule)

### 4. First-time setup

On first launch with no team configured, the Settings modal opens automatically. Configure:

1. **Jira Project**: enter your project key (e.g. `FDATA`).

2. **Team**: select your team from the dropdown — it auto-populates from the Jira "Team" field values for that project. Selecting a team searches for matching boards automatically.

3. **Board**: if multiple boards match, pick the correct one from the list. If exactly one matches, it is auto-selected.

4. **Team Members**: click "Refresh from Workday" to auto-detect your team, or manually edit `team-config.json`.

5. **PA Schedule** (optional, off by default): enable if your team has a Promotion Analysis rotation. Provide the Confluence page URL containing the PA schedule table.

6. **PR Review** (optional, off by default): enable if your team participates in cross-team PR review rotations. Provide the Confluence page URL and choose the duty weight (half day or full day).

7. **Backlog Sprints**: select which backlog sprints to show (sprints without a start date, e.g. stretch goal or deferred backlog). These appear as additional sections you can drag tasks from.

8. Click **Save**.

> **Switching boards**: changing the board triggers a full UI refresh — all task rows are cleared, PA and PR schedules are re-fetched for the new board, and a warning is shown if absence data may be stale for the new sprint window.

### 5. Plan the sprint

- The capacity table shows each team member with their working days, deductions, efficiency, and calculated capacity
- **Drag tasks** from backlog sections into Sprint Commitment (or back)
- **Right-click tasks** to move them via a context menu (useful when the destination requires a lot of scrolling)
- **Expand epics** by clicking an epic row to see its child tasks inline. Children show their sprint membership as badges, and can be dragged or right-click moved to any section
- **Edit SP** directly in the table (click the SP cell); SP=0 means 4h of work and counts as 0.5 SP in capacity calculations
- **Click a person's name** in the capacity table to expand a detail row showing vacation date ranges, PA/PR schedule dates, and committed tasks (updates dynamically as you make changes)
- **Edit assignees** via dropdown in each row
- **Edit priority** via a custom dropdown with Jira priority icons
- **Edit efficiency %** per person (auto-saved to config)
- **Reorder backlog sections** by dragging their headers; order persists across reloads
- **Sort columns** by clicking Epic, Type, Assignee, or Priority headers (toggles ascending/descending)
- **Epic column** shows the parent epic name for each task, linked to Jira
- **Pending changes persist** across page reloads (stored in localStorage); multiple edits to the same task are merged into one entry
- **Discard All** reverts all pending changes and moves tasks back to their original backlogs
- Click **Apply to Jira** to sync all changes — a progress bar shows real-time status with per-item ✓/✗ feedback
- Click **Save** to export a standalone HTML snapshot for offline reference (no server needed to view)

## Configuration Reference

All settings are stored in `team-config.json`:

```json
{
  "project_key": "FDATA",
  "board_id": 12345,
  "board_url": "https://your-jira.com/secure/RapidBoard.jspa?rapidView=12345",
  "board_name": "Gemini",
  "team_name": "Gemini",
  "team": ["Alice", "Bob", "Charlie"],
  "efficiency": {
    "default": 70,
    "Charlie": 50
  },
  "pa_enabled": false,
  "pa_confluence_url": "",
  "pr_enabled": false,
  "pr_confluence_url": "",
  "pr_duty_weight": 0.5,
  "confluence_account_ids": {},
  "unscheduled_buffer": 5
}
```

| Field | Description |
|---|---|
| `project_key` | Jira project key (e.g. `FDATA`) |
| `board_id` | Jira board ID (set by the board picker in Settings) |
| `board_url` | Full Jira board URL |
| `board_name` | Jira board display name |
| `team_name` | Value from the Jira Team field (`customfield_19700`); used to pre-select the Team dropdown in Settings |
| `team` | Array of team member names |
| `efficiency.default` | Default efficiency % for all members (typically 70) |
| `efficiency.<name>` | Per-person override (e.g. 50 for part-time, 0 for fully allocated elsewhere) |
| `pa_enabled` | Whether PA column is shown in capacity table |
| `pa_confluence_url` | Confluence page URL with PA schedule (required if PA enabled) |
| `pr_enabled` | Whether PR review column is shown in capacity table |
| `pr_confluence_url` | Confluence page URL with PR review rotation (required if PR enabled) |
| `pr_duty_weight` | Deduction per PR rotation: `0.5` (half day, default) or `1` (full day) |
| `confluence_account_ids` | Mapping of Confluence account IDs to team member names (for PA parsing) |
| `unscheduled_buffer` | Default unscheduled buffer in SP (default 5); overridable in the UI per session |

## Capacity Formula

```
Available Days = Working Days - Deductions (Absence, PA, PR, KTLO, Events, Hackathon, Training, Spillover)
Person Capacity = Available Days × Efficiency%
Team Net Capacity = Sum of person capacities - Unscheduled buffer
```

- **Working Days**: Mon-Fri in the sprint window (typically 10 for a 2-week sprint), minus holidays. Holidays in the sprint are shown in the capacity card subtitle and in the AVAIL column tooltip.
- **Deductions**: subtracted from working days before efficiency is applied. Includes absences, PA/PR duties, KTLO, events, hackathon, training, and spillover.
- **Spillover**: remaining work from the current (active) sprint that will carry over. Read-only column computed from Jira `timetracking.remainingEstimate` on incomplete issues assigned to team members. Click a person's name to see the individual spillover tasks.
- **Efficiency %**: what fraction of available time goes to sprint work (default 70%)
- **SP=0**: special value meaning 4h of work; counts as 0.5 SP in capacity math (display still shows "0")
- **Unscheduled buffer**: team-level reserve for unplanned work (default 5 SP)
- **Headroom**: Gross Capacity minus Committed SP — shows how much total capacity remains uncommitted, including the unscheduled buffer. The progress bar fills relative to Net Available; green up to 90%, amber 90–100%, amber when eating into buffer, red when exceeding gross.

## Holidays

Holiday data is stored in `holidays-ca-qc.json`. To refresh for a new year:

1. Open Workday and search for "Holiday Calendar Report"
2. Set the year and export to Excel
3. Use Claude to parse the Excel file and update `holidays-ca-qc.json`

The report should include your country/region's holidays. Currently configured for Canada + Quebec.

## Workday Integration

Two Playwright scripts automate Workday data collection. Both open a headed Chrome window for SSO login:

| Script | What it does | Triggered by |
|---|---|---|
| `fetch-team.py` | Scrapes team member names from "Manage My Team" | Settings > "Refresh from Workday" |
| `fetch-absences.py` | Scrapes absence calendar for the sprint window | Startup overlay or automatic after team config |

These scripts use your local Chrome with SSO cookies. On first run, you'll need to complete the SSO login manually in the browser window that opens.

The absence scraper clicks each absence event block on the Workday calendar to open the "Absence Entries" popup, which reveals exact date ranges and duration per day. This avoids any pro-rating and gives accurate day counts even when absences span sprint boundaries.

## Confluence Integration (optional)

If your team uses a PA (Promotion Analysis) rotation tracked in Confluence:

1. Enable PA in Settings and provide the Confluence page URL
2. Set up Confluence OAuth: the startup overlay will prompt for authentication if needed
3. Map Confluence account IDs to team member names in `confluence_account_ids`

The server reads the PA schedule table from Confluence and assigns 1 day per person per PA duty that falls within the sprint window.

## File Structure

```
sprint-plan.html       # Interactive sprint planner UI
sprint-server.py       # Local HTTP server (Jira bridge)
fetch-absences.py      # Workday absence scraper
fetch-team.py          # Workday team list scraper
auth-confluence.py     # Confluence OAuth helper
team-config.json       # Team settings (not committed)
holidays-ca-qc.json          # Holiday calendar
.mcp.json              # Jira MCP config with token (gitignored)
absences.json          # Cached absence data (auto-generated)
pa-schedule.json       # Cached PA schedule (auto-generated)
pr-schedule.json       # Cached PR review schedule (auto-generated)
backlog-prefs.json     # Saved backlog selections, order, and per-section type filters (auto-generated)
issue-types.json       # Known issue types for the board
```
