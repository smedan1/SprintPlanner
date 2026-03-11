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

1. **Board URL**: paste your Jira board URL (e.g. `https://jira.autodesk.com/secure/RapidBoard.jspa?rapidView=12345`). The board ID is extracted automatically.

2. **Team Members**: click "Refresh from Workday" to auto-detect your team, or manually edit `team-config.json`.

3. **PA Schedule** (optional): enable if your team has a Promotion Analysis rotation. Provide the Confluence page URL containing the PA schedule table.

4. **Backlog Sprints**: select which backlog sprints to show (e.g. stretch goal sprints). These appear as additional sections you can drag tasks from.

5. Click **Save**.

### 5. Plan the sprint

- The capacity table shows each team member with their working days, deductions, efficiency, and calculated capacity
- **Drag tasks** from backlog sections into Sprint Commitment (or back)
- **Right-click tasks** to move them via a context menu (useful when the destination requires a lot of scrolling)
- **Edit SP** directly in the table (click the SP cell)
- **Edit assignees** via dropdown in each row
- **Edit priority** via a custom dropdown with Jira priority icons
- **Edit efficiency %** per person (auto-saved to config)
- **Reorder backlog sections** by dragging their headers; order persists across reloads
- **Pending changes persist** across page reloads (stored in localStorage)
- **Discard All** reverts all pending changes and moves tasks back to their original backlogs
- Click **Apply to Jira** to sync all changes

## Configuration Reference

All settings are stored in `team-config.json`:

```json
{
  "board_id": 12345,
  "board_url": "https://your-jira.com/secure/RapidBoard.jspa?rapidView=12345",
  "team": ["Alice", "Bob", "Charlie"],
  "efficiency": {
    "default": 70,
    "Charlie": 50
  },
  "pa_enabled": false,
  "pa_confluence_url": "",
  "confluence_account_ids": {},
  "unscheduled_buffer": 5
}
```

| Field | Description |
|---|---|
| `board_id` | Jira board ID (extracted from URL) |
| `board_url` | Full Jira board URL |
| `team` | Array of team member names |
| `efficiency.default` | Default efficiency % for all members (typically 70) |
| `efficiency.<name>` | Per-person override (e.g. 50 for part-time, 0 for fully allocated elsewhere) |
| `pa_enabled` | Whether PA column is shown in capacity table |
| `pa_confluence_url` | Confluence page URL with PA schedule (required if PA enabled) |
| `confluence_account_ids` | Mapping of Confluence account IDs to team member names (for PA parsing) |
| `unscheduled_buffer` | Default unscheduled buffer in SP (default 5); overridable in the UI per session |

## Capacity Formula

```
Person Capacity = (Working Days x Efficiency%) - Absence - Events - Hackathon - Training - PA - KTLO
Team Net Capacity = Sum of person capacities - Unscheduled buffer
```

- **Working Days**: Mon-Fri in the sprint window (typically 10 for a 2-week sprint), minus holidays
- **Efficiency %**: what fraction of available time goes to sprint work (default 70%)
- **Deductions**: subtracted after efficiency is applied (1 day of vacation = 1 full day deducted)
- **Unscheduled buffer**: team-level reserve for unplanned work (default 5 SP)

## Holidays

Holiday data is stored in `holidays.json`. To refresh for a new year:

1. Open Workday and search for "Holiday Calendar Report"
2. Set the year and export to Excel
3. Use Claude to parse the Excel file and update `holidays.json`

The report should include your country/region's holidays. Currently configured for Canada + Quebec.

## Workday Integration

Two Playwright scripts automate Workday data collection. Both open a headed Chrome window for SSO login:

| Script | What it does | Triggered by |
|---|---|---|
| `fetch-team.py` | Scrapes team member names from "Manage My Team" | Settings > "Refresh from Workday" |
| `fetch-absences.py` | Scrapes absence calendar for the sprint window | Startup overlay or automatic after team config |

These scripts use your local Chrome with SSO cookies. On first run, you'll need to complete the SSO login manually in the browser window that opens.

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
holidays.json          # Holiday calendar
.mcp.json              # Jira MCP config with token (gitignored)
absences.json          # Cached absence data (auto-generated)
pa-schedule.json       # Cached PA schedule (auto-generated)
backlog-prefs.json     # Saved backlog selections (auto-generated)
```
