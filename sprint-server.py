"""
Sprint server — local HTTP proxy that syncs drag-and-drop sprint moves to Jira.

Usage:
    python sprint-server.py

Runs on http://localhost:5000
Reads config from .mcp.json in the same directory.

API:
    GET  /api/health                     — liveness + service connectivity check
    GET  /api/sp?issues=KEY1,KEY2,...    — fetch story points for issues
    GET  /oauth/reauth                   — launch auth-confluence.py in headed Chrome
    POST /api/move                       — move issue to sprint
    Body: { "issue_key": "FDATA-24666", "sprint_id": 156907 }
"""

import json
import os
import re
import subprocess
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

# ── Config ────────────────────────────────────────────────────────────────────

PORT       = 5000
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MCP_JSON   = os.path.join(SCRIPT_DIR, '.mcp.json')


def load_config():
    with open(MCP_JSON, 'r') as f:
        cfg = json.load(f)
    env = cfg['mcpServers']['mcp-jira']['env']
    return env['JIRA_URL'], env['JIRA_PERSONAL_TOKEN']


JIRA_URL, JIRA_TOKEN = load_config()


# ── Confluence session auth ───────────────────────────────────────────────────

def get_confluence_session_token() -> str:
    """Read session_token fresh from .mcp.json on every call."""
    try:
        with open(MCP_JSON, 'r') as f:
            cfg = json.load(f)
        return cfg.get('confluence', {}).get('session_token', '')
    except Exception:
        return ''


def check_confluence_auth() -> tuple[bool, str, str, bool]:
    """
    Calls GET /wiki/rest/api/user/current with the session cookie.
    Returns (ok, display_name, error_message, needs_auth).
    needs_auth=True  > user must click Re-authenticate.
    """
    token = get_confluence_session_token()
    if not token:
        return False, '', 'not authenticated', True

    req = urllib.request.Request(
        'https://autodesk.atlassian.net/wiki/rest/api/user/current',
        method='GET',
        headers={
            'Cookie': f'cloud.session.token={token}',
            'Accept': 'application/json',
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        name = data.get('displayName') or data.get('username') or 'unknown'
        return True, name, '', False
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False, '', 'session expired', True
        return False, '', f'HTTP {e.code}', False
    except Exception as e:
        return False, '', str(e), False


# ── Jira priorities cache ─────────────────────────────────────────────────────

_PRIORITIES_CACHE: list[dict] | None = None


def _strip_pri_name(name: str) -> str:
    """Strip leading number prefix from Jira priority names (e.g. '3. Major', '1 - Critical')."""
    return re.sub(r'^\d+[\s.\-]+\s*', '', name)


def _download_priority_icon(icon_url: str, pri_name: str) -> str:
    """Download a Jira priority icon and save to icons/ dir. Returns local relative path."""
    icons_dir = os.path.join(SCRIPT_DIR, 'icons')
    os.makedirs(icons_dir, exist_ok=True)
    safe_name = re.sub(r'[^a-z0-9]', '-', pri_name.lower()).strip('-')
    # Detect extension from URL
    ext = '.png'
    if '.svg' in icon_url:
        ext = '.svg'
    elif '.gif' in icon_url:
        ext = '.gif'
    local_name = f'priority-{safe_name}{ext}'
    local_path = os.path.join(icons_dir, local_name)
    if os.path.exists(local_path):
        return f'icons/{local_name}'
    try:
        req = urllib.request.Request(icon_url, headers={'Authorization': f'Bearer {JIRA_TOKEN}'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read()
        with open(local_path, 'wb') as f:
            f.write(data)
        return f'icons/{local_name}'
    except Exception as e:
        print(f'  x Failed to download priority icon {icon_url}: {e}')
        return ''


def fetch_jira_priorities() -> list[dict]:
    """Fetch all priority levels from Jira. Cached after first call."""
    global _PRIORITIES_CACHE
    if _PRIORITIES_CACHE is not None:
        return _PRIORITIES_CACHE
    req = urllib.request.Request(
        f'{JIRA_URL}/rest/api/2/priority',
        method='GET',
        headers={'Authorization': f'Bearer {JIRA_TOKEN}'}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = json.loads(resp.read().decode('utf-8'))
        result = []
        seen_names = set()
        for p in raw:
            name = _strip_pri_name(p.get('name', ''))
            if name in seen_names:
                continue
            seen_names.add(name)
            icon_url = p.get('iconUrl', '')
            local_icon = _download_priority_icon(icon_url, name) if icon_url else ''
            result.append({
                'id': p.get('id', ''),
                'name': name,
                'iconUrl': icon_url,
                'localIcon': local_icon,
            })
        _PRIORITIES_CACHE = result
        print(f'  > Fetched {len(result)} Jira priorities')
        return result
    except Exception as e:
        print(f'  x Failed to fetch priorities: {e}')
        return []


# ── Jira API calls ────────────────────────────────────────────────────────────

def check_jira_health() -> tuple[bool, str]:
    req = urllib.request.Request(
        f'{JIRA_URL}/rest/api/2/serverInfo',
        method='GET',
        headers={'Authorization': f'Bearer {JIRA_TOKEN}'}
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status < 300, ''
    except urllib.error.HTTPError as e:
        return False, f'HTTP {e.code}'
    except Exception as e:
        return False, str(e)


def check_docker() -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ['docker', 'info', '--format', '{{.ServerVersion}}'],
            capture_output=True, timeout=5
        )
        if result.returncode == 0:
            return True, ''
        return False, result.stderr.decode('utf-8', errors='replace').strip().splitlines()[0]
    except FileNotFoundError:
        return False, 'docker not found in PATH'
    except subprocess.TimeoutExpired:
        return False, 'timed out'
    except Exception as e:
        return False, str(e)


def get_story_points(issue_keys: list[str]) -> tuple[int, dict]:
    params = urllib.parse.urlencode({
        'jql': 'key in (' + ','.join(issue_keys) + ')',
        'fields': 'customfield_10130',
        'maxResults': 200,
    })
    req = urllib.request.Request(
        f'{JIRA_URL}/rest/api/2/search?{params}',
        method='GET',
        headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {JIRA_TOKEN}'}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode('utf-8'))
        return 200, {i['key']: i.get('fields', {}).get('customfield_10130')
                     for i in body.get('issues', [])}
    except urllib.error.HTTPError as e:
        e.read(); return e.code, {}
    except Exception:
        return 0, {}


def get_issues_for_sprint(sprint_id: int) -> list[dict]:
    """Fetch all issues in a sprint with SP, assignee, and metadata."""
    params = urllib.parse.urlencode({
        'jql': f'sprint = {sprint_id} ORDER BY created ASC',
        'fields': 'customfield_10130,assignee,summary,issuetype,status,priority',
        'maxResults': 500,
    })
    req = urllib.request.Request(
        f'{JIRA_URL}/rest/api/2/search?{params}',
        method='GET',
        headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {JIRA_TOKEN}'}
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode('utf-8'))
        result = []
        for i in body.get('issues', []):
            fields   = i.get('fields', {})
            status_name = (fields.get('status') or {}).get('name', '')
            if status_name.lower() in ('closed', 'resolved', 'done'):
                continue
            issue_type = (fields.get('issuetype') or {}).get('name', '')
            if issue_type.lower() == 'epic':
                continue
            assignee = fields.get('assignee') or {}
            pri      = fields.get('priority') or {}
            pri_name = _strip_pri_name(pri.get('name', ''))
            result.append({
                'key':              i['key'],
                'summary':          fields.get('summary', ''),
                'sp':               fields.get('customfield_10130'),
                'assignee_display': assignee.get('displayName') or '',
                'assignee_name':    assignee.get('name') or '',
                'type':             (fields.get('issuetype') or {}).get('name', 'Story'),
                'status':           (fields.get('status')    or {}).get('name', ''),
                'priority':         pri_name,
                'priority_id':      pri.get('id', ''),
                'priority_icon':    pri.get('iconUrl', ''),
                'sprint_id':        sprint_id,
            })
        return result
    except Exception:
        return []


def _sp_to_estimate(sp: float | None) -> str:
    """Convert story points (1 SP = 1 day) to a Jira time string, e.g. 3→'3d', 0.5→'4h'."""
    if not sp:
        return ''
    days  = int(sp)
    hours = round((sp - days) * 8)
    if days and hours:
        return f'{days}d {hours}h'
    if days:
        return f'{days}d'
    return f'{hours}h'


def get_time_spent(issue_key: str) -> int:
    """Returns timeSpentSeconds for the issue. Returns 0 on error or no logged work."""
    req = urllib.request.Request(
        f'{JIRA_URL}/rest/api/2/issue/{issue_key}?fields=timetracking',
        method='GET',
        headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {JIRA_TOKEN}'}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode('utf-8'))
        tt = (body.get('fields') or {}).get('timetracking') or {}
        return tt.get('timeSpentSeconds') or 0
    except Exception:
        return 0


def _secs_to_estimate(secs: int) -> str:
    """Convert seconds to a Jira time string (1 day = 8 h). Returns '0d' for zero."""
    if secs <= 0:
        return '0d'
    days  = secs // 28800
    hours = (secs % 28800) // 3600
    if days and hours:
        return f'{days}d {hours}h'
    if days:
        return f'{days}d'
    return f'{hours}h'


def update_issue_fields(issue_key: str, fields: dict) -> tuple[int, str]:
    req = urllib.request.Request(
        f'{JIRA_URL}/rest/api/2/issue/{issue_key}',
        data=json.dumps({'fields': fields}).encode('utf-8'),
        method='PUT',
        headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {JIRA_TOKEN}'}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, 'ok'
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', errors='replace')
    except Exception as e:
        return 0, str(e)


def find_user_name(display_name: str) -> tuple[str, str]:
    """Search Jira Server for a user by display name.
    Returns (username, error_message). On success error_message is empty."""
    # Jira Server uses 'username' param; Jira Cloud uses 'query'
    params = urllib.parse.urlencode({'username': display_name, 'maxResults': 5})
    req = urllib.request.Request(
        f'{JIRA_URL}/rest/api/2/user/search?{params}',
        method='GET',
        headers={'Authorization': f'Bearer {JIRA_TOKEN}'}
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            users = json.loads(resp.read().decode('utf-8'))
        if not users:
            return '', f"No Jira user found matching '{display_name}'"
        # Prefer exact display name match, else take first result
        for u in users:
            if u.get('displayName', '').lower() == display_name.lower():
                return u['name'], ''
        return users[0]['name'], ''
    except urllib.error.HTTPError as e:
        return '', f'User search HTTP {e.code}'
    except Exception as e:
        return '', str(e)


def move_issue_to_sprint(issue_key: str, sprint_id: int) -> tuple[int, str]:
    req = urllib.request.Request(
        f'{JIRA_URL}/rest/agile/1.0/sprint/{sprint_id}/issue',
        data=json.dumps({'issues': [issue_key]}).encode('utf-8'),
        method='POST',
        headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {JIRA_TOKEN}'}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, 'ok'
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', errors='replace')
    except Exception as e:
        return 0, str(e)


# ── Team config ──────────────────────────────────────────────────────────────

_DEFAULT_CONFIG = {
    'board_id': 0,
    'board_url': '',
    'team': [],
    'efficiency': {
        'default': 70,
    },
    'confluence_account_ids': {},
    'unscheduled_buffer': 5,
}

_TEAM_CONFIG_PATH = os.path.join(SCRIPT_DIR, 'team-config.json')
_team_config_cache = None


def load_team_config() -> dict:
    """Read team-config.json, falling back to defaults if missing."""
    global _team_config_cache
    if _team_config_cache is not None:
        return _team_config_cache
    try:
        with open(_TEAM_CONFIG_PATH, 'r') as f:
            cfg = json.load(f)
        # Merge with defaults so new keys are always present
        merged = {**_DEFAULT_CONFIG, **cfg}
        _team_config_cache = merged
    except Exception:
        _team_config_cache = dict(_DEFAULT_CONFIG)
    return _team_config_cache


def save_team_config(cfg: dict) -> None:
    """Write team-config.json and update cache."""
    global _team_config_cache
    with open(_TEAM_CONFIG_PATH, 'w') as f:
        json.dump(cfg, f, indent=2)
    _team_config_cache = cfg


def get_board_id() -> int:
    return load_team_config().get('board_id', 17259)


def get_team() -> list[str]:
    return load_team_config().get('team', [])


def get_efficiency_map() -> dict:
    return load_team_config().get('efficiency', {'default': 70})


def get_account_id_map() -> dict:
    return load_team_config().get('confluence_account_ids', {})


def invalidate_team_config_cache() -> None:
    """Force next load_team_config() to re-read from disk."""
    global _team_config_cache
    _team_config_cache = None


# ── Sprint info helpers ───────────────────────────────────────────────────────

_HOLIDAYS_CACHE: set[date] | None = None
_HOLIDAYS_LIST_CACHE: list[dict] | None = None


def load_holidays() -> tuple[set[date], list[dict]]:
    """Read holidays-ca-qc.json, return (set of dates, raw list). Cached after first call."""
    global _HOLIDAYS_CACHE, _HOLIDAYS_LIST_CACHE
    if _HOLIDAYS_CACHE is not None:
        return _HOLIDAYS_CACHE, _HOLIDAYS_LIST_CACHE
    path = os.path.join(SCRIPT_DIR, 'holidays-ca-qc.json')
    try:
        with open(path, 'r') as f:
            raw = json.load(f)
        entries = raw if isinstance(raw, list) else raw.get('holidays', [])
        _HOLIDAYS_LIST_CACHE = entries
        _HOLIDAYS_CACHE = {date.fromisoformat(h['date']) for h in entries}
    except Exception:
        _HOLIDAYS_CACHE = set()
        _HOLIDAYS_LIST_CACHE = []
    return _HOLIDAYS_CACHE, _HOLIDAYS_LIST_CACHE


def compute_working_days(start_str: str, end_str: str) -> tuple[int, list[dict]]:
    """Count Mon-Fri in [start, end) excluding holidays. Returns (count, holidays_in_range)."""
    holidays, holidays_list = load_holidays()
    start = date.fromisoformat(start_str[:10])
    end = date.fromisoformat(end_str[:10])
    count = 0
    holidays_in_range = []
    d = start
    while d < end:
        if d.weekday() < 5:  # Mon-Fri
            if d in holidays:
                # Find the holiday name
                ds = d.isoformat()
                name = next((h['name'] for h in holidays_list if h['date'] == ds), 'Holiday')
                holidays_in_range.append({'date': ds, 'name': name})
            else:
                count += 1
        d += timedelta(days=1)
    return count, holidays_in_range


def load_absences() -> dict:
    """Read absences.json if it exists. Returns {person: days} mapping."""
    path = os.path.join(SCRIPT_DIR, 'absences.json')
    try:
        with open(path, 'r') as f:
            raw = json.load(f)
        absences = raw.get('absences', {})
        return {name: info.get('days', 0) for name, info in absences.items()}
    except Exception:
        return {}


def load_absence_detail() -> dict:
    """Read absences.json with date details. Returns {person: {days, dates}}."""
    path = os.path.join(SCRIPT_DIR, 'absences.json')
    try:
        with open(path, 'r') as f:
            raw = json.load(f)
        absences = raw.get('absences', {})
        result = {}
        for name, info in absences.items():
            result[name] = {
                'days': info.get('days', 0),
                'dates': info.get('dates', []),
            }
        return result
    except Exception:
        return {}


def load_pa_schedule() -> dict:
    """Read pa-schedule.json if it exists. Returns {person: days} mapping."""
    path = os.path.join(SCRIPT_DIR, 'pa-schedule.json')
    try:
        with open(path, 'r') as f:
            raw = json.load(f)
        pa = raw.get('pa', {})
        return {name: (v['days'] if isinstance(v, dict) else v) for name, v in pa.items()}
    except Exception:
        return {}


def load_pa_schedule_full() -> dict:
    """Read pa-schedule.json with date details. Returns {person: {days, dates}}."""
    path = os.path.join(SCRIPT_DIR, 'pa-schedule.json')
    try:
        with open(path, 'r') as f:
            raw = json.load(f)
        pa = raw.get('pa', {})
        result = {}
        for name, v in pa.items():
            if isinstance(v, dict):
                result[name] = v
            else:
                result[name] = {'days': v, 'dates': []}
        return result
    except Exception:
        return {}


def _parse_pa_date(text: str) -> date | None:
    """Parse PA schedule date in 'M/D/YYYY' or 'D Mon YYYY' format."""
    from datetime import datetime
    text = text.strip()
    for fmt in ('%m/%d/%Y', '%d %b %Y'):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None




def _get_pa_page_id() -> str:
    """Extract the Confluence page ID from the configured PA URL."""
    cfg = load_team_config()
    pa_url = cfg.get('pa_confluence_url', '')
    # Try /pages/<id> pattern
    m = re.search(r'/pages/(\d+)', pa_url)
    if m:
        return m.group(1)
    # Try Confluence short URL /wiki/x/<shortcode> — resolve via redirect
    if '/wiki/x/' in pa_url or '/wiki/spaces/' in pa_url:
        try:
            token = get_confluence_session_token()
            req = urllib.request.Request(pa_url, method='GET', headers={
                'Cookie': f'cloud.session.token={token}' if token else '',
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                final_url = resp.url
            m = re.search(r'/pages/(\d+)', final_url)
            if m:
                return m.group(1)
        except Exception:
            pass
    return ''


def _match_name_to_team(raw_name: str, team: list[str]) -> str | None:
    """Match a raw name from Confluence to a team member (exact, case-insensitive).
    Also handles mojibake (double-encoded UTF-8) by trying latin-1 → utf-8 decode."""
    raw_lower = raw_name.lower().strip()
    for t in team:
        if t.lower() == raw_lower:
            return t
    # Try fixing mojibake: if the name was double-encoded, decode latin-1 → utf-8
    try:
        fixed = raw_name.encode('latin-1').decode('utf-8').lower().strip()
        for t in team:
            if t.lower() == fixed:
                return t
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass
    return None


def _normalize_account_map(account_map: dict, team: list[str]) -> dict:
    """Re-validate account map values against the team list.
    Fixes stale or mojibake entries (e.g. double-encoded Unicode).
    Also persists corrections back to team-config.json."""
    fixed = {}
    needs_save = False
    for aid, name in account_map.items():
        if name in team:
            fixed[aid] = name
        else:
            matched = _match_name_to_team(name, team)
            if matched:
                fixed[aid] = matched
                needs_save = True
            else:
                fixed[aid] = name
    if needs_save:
        cfg = load_team_config()
        aid_map = cfg.get('confluence_account_ids', {})
        for aid, name in fixed.items():
            if aid in aid_map and aid_map[aid] != name and name in team:
                aid_map[aid] = name
        cfg['confluence_account_ids'] = aid_map
        save_team_config(cfg)
        invalidate_team_config_cache()
        print(f'  > Fixed mojibake in confluence_account_ids')
    return fixed


def _discover_account_ids_from_view(page_id: str, token: str, team: list[str]) -> dict:
    """Fetch Confluence page in 'view' format to discover account ID → display name mappings.
    The view format renders user mentions as readable names alongside their account IDs."""
    try:
        req = urllib.request.Request(
            f'https://autodesk.atlassian.net/wiki/api/v2/pages/{page_id}?body-format=view',
            method='GET',
            headers={
                'Cookie': f'cloud.session.token={token}',
                'Accept': 'application/json',
            }
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        view_html = data.get('body', {}).get('view', {}).get('value', '')
        # View format renders user mentions as: <a ... data-account-id="..." ...>Display Name</a>
        # or: <span ... data-account-id="..." ...>Display Name</span>
        user_link_pattern = re.compile(
            r'data-account-id="([^"]+)"[^>]*>([^<]+)<',
            re.DOTALL
        )
        discovered = {}
        for m in user_link_pattern.finditer(view_html):
            aid = m.group(1)
            display = m.group(2).strip()
            if aid not in discovered and display:
                matched = _match_name_to_team(display, team)
                if matched:
                    discovered[aid] = matched
                else:
                    # Store raw name for potential partial matching later
                    discovered[aid] = display
        print(f'  > View format discovered {len(discovered)} user references')
        return discovered
    except Exception as e:
        print(f'  > View format fetch failed: {e}')
        return {}


def fetch_pa_from_confluence(sprint_start_str: str, sprint_end_str: str) -> dict:
    """Fetch PA schedule from Confluence, parse it, return {person: days} for the sprint.
    sprint_end is exclusive."""
    token = get_confluence_session_token()
    if not token:
        raise RuntimeError('No Confluence session token')

    page_id = _get_pa_page_id()
    if not page_id:
        raise RuntimeError('No PA Confluence page configured (set pa_confluence_url in settings)')

    req = urllib.request.Request(
        f'https://autodesk.atlassian.net/wiki/api/v2/pages/{page_id}?body-format=storage',
        method='GET',
        headers={
            'Cookie': f'cloud.session.token={token}',
            'Accept': 'application/json',
        }
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode('utf-8'))

    body = data.get('body', {}).get('storage', {}).get('value', '')
    s_start = date.fromisoformat(sprint_start_str)
    s_end = date.fromisoformat(sprint_end_str)

    # Storage format uses namespaced tags (ac:link, ri:user) so use regex
    tr_pattern = re.compile(r'<tr[^>]*>(.*?)</tr>', re.DOTALL)
    time_pattern = re.compile(r'<time\s+datetime="(\d{4}-\d{2}-\d{2})"')
    user_pattern = re.compile(r'ri:account-id="([^"]+)"')
    # For plain text dates (older rows without <time> tags)
    td_text_pattern = re.compile(r'<td[^>]*><p[^>]*>(\d{1,2}\s+\w{3}\s+\d{4})</p></td>')

    account_map = dict(get_account_id_map())  # copy so we can augment
    team = get_team()
    account_map = _normalize_account_map(account_map, team)

    # If account map is incomplete, try to discover mappings from the view format
    # Collect all account IDs from the storage body first
    all_account_ids = set(user_pattern.findall(body))
    unmapped = all_account_ids - set(account_map.keys())
    discovered_names = {}
    if unmapped:
        print(f'  > {len(unmapped)} unmapped account IDs, fetching view format to discover names...')
        discovered_names = _discover_account_ids_from_view(page_id, token, team)
        # Merge discovered into account_map for this run
        for aid, name in discovered_names.items():
            if aid not in account_map:
                account_map[aid] = name

    pa_days = {}
    for tr_match in tr_pattern.finditer(body):
        tr_html = tr_match.group(1)

        # Extract date from this row
        pa_date = None
        time_m = time_pattern.search(tr_html)
        if time_m:
            try:
                pa_date = date.fromisoformat(time_m.group(1))
            except ValueError:
                pass
        if pa_date is None:
            text_m = td_text_pattern.search(tr_html)
            if text_m:
                pa_date = _parse_pa_date(text_m.group(1))
        if pa_date is None:
            continue

        # Check if date falls within sprint [start, end)
        if pa_date < s_start or pa_date >= s_end:
            continue

        # Extract users from this row using account ID map
        for account_id in user_pattern.findall(tr_html):
            name = account_map.get(account_id)
            if name and name in team:
                if name not in pa_days:
                    pa_days[name] = {'days': 0, 'dates': []}
                pa_days[name]['days'] += 1
                pa_days[name]['dates'].append(pa_date.isoformat())

    # Auto-save discovered account ID mappings for future use
    if discovered_names:
        cfg = load_team_config()
        aid_map = cfg.get('confluence_account_ids', {})
        changed = False
        for aid, nm in discovered_names.items():
            if aid not in aid_map and nm in team:
                aid_map[aid] = nm
                changed = True
        if changed:
            cfg['confluence_account_ids'] = aid_map
            save_team_config(cfg)
            invalidate_team_config_cache()
            print(f'  > Auto-saved Confluence account IDs: {discovered_names}')

    return pa_days


def save_pa_schedule(sprint_start: str, sprint_end: str, pa: dict) -> None:
    """Write pa-schedule.json cache."""
    path = os.path.join(SCRIPT_DIR, 'pa-schedule.json')
    with open(path, 'w') as f:
        json.dump({
            'sprint_start': sprint_start,
            'sprint_end': sprint_end,
            'pa': pa,
        }, f, indent=2)


def check_pa_freshness() -> dict:
    """Check whether pa-schedule.json is fresh for the current sprint."""
    path = os.path.join(SCRIPT_DIR, 'pa-schedule.json')
    sprint, _ = get_future_sprint_info_cached(get_board_id())
    if not sprint:
        return {'fresh': False, 'reason': 'no_sprint', 'message': 'Cannot detect sprint'}
    sprint_start = sprint['startDate'][:10]
    sprint_end = sprint['endDate'][:10]
    base = {'sprint_start': sprint_start, 'sprint_end': sprint_end}

    if not os.path.exists(path):
        return {**base, 'fresh': False, 'reason': 'missing', 'message': 'No pa-schedule.json found'}

    age_hours = (time.time() - os.path.getmtime(path)) / 3600
    try:
        with open(path, 'r') as f:
            data = json.load(f)
        file_start = data.get('sprint_start', '')
    except Exception:
        return {**base, 'fresh': False, 'reason': 'corrupt', 'message': 'Cannot read pa-schedule.json'}

    if file_start != sprint_start:
        return {**base, 'fresh': False, 'reason': 'wrong_sprint',
                'message': 'Cached for ' + file_start + ', need ' + sprint_start}

    if age_hours >= 24:
        return {**base, 'fresh': False, 'reason': 'stale',
                'message': 'pa-schedule.json is ' + str(round(age_hours)) + 'h old (>24h)'}

    return {**base, 'fresh': True, 'age_hours': round(age_hours, 1)}


# ── PR (Pull Request Review) Schedule ──────────────────────────────────────

def load_pr_schedule() -> dict:
    """Read pr-schedule.json if it exists. Returns {person: weighted_days} mapping.
    Applies pr_duty_weight from config (rotations stored as count=1 each)."""
    path = os.path.join(SCRIPT_DIR, 'pr-schedule.json')
    weight = load_team_config().get('pr_duty_weight', 0.5)
    try:
        with open(path, 'r') as f:
            raw = json.load(f)
        pr = raw.get('pr', {})
        return {name: (v['days'] if isinstance(v, dict) else v) * weight for name, v in pr.items()}
    except Exception:
        return {}


def load_pr_schedule_full() -> dict:
    """Read pr-schedule.json with date details. Returns {person: {days, dates}}.
    Applies pr_duty_weight from config to the days value."""
    path = os.path.join(SCRIPT_DIR, 'pr-schedule.json')
    weight = load_team_config().get('pr_duty_weight', 0.5)
    try:
        with open(path, 'r') as f:
            raw = json.load(f)
        pr = raw.get('pr', {})
        result = {}
        for name, v in pr.items():
            if isinstance(v, dict):
                result[name] = {'days': v['days'] * weight, 'dates': v.get('dates', [])}
            else:
                result[name] = {'days': v * weight, 'dates': []}
        return result
    except Exception:
        return {}


def _get_pr_page_id() -> str:
    """Extract the Confluence page ID from the configured PR URL."""
    cfg = load_team_config()
    pr_url = cfg.get('pr_confluence_url', '')
    m = re.search(r'/pages/(\d+)', pr_url)
    if m:
        return m.group(1)
    if '/wiki/x/' in pr_url or '/wiki/spaces/' in pr_url:
        try:
            token = get_confluence_session_token()
            req = urllib.request.Request(pr_url, method='GET', headers={
                'Cookie': f'cloud.session.token={token}' if token else '',
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                final_url = resp.url
            m = re.search(r'/pages/(\d+)', final_url)
            if m:
                return m.group(1)
        except Exception:
            pass
    return ''


def fetch_pr_from_confluence(sprint_start_str: str, sprint_end_str: str) -> dict:
    """Fetch PR review schedule from Confluence, return {person: {days, dates}} for the sprint.
    Stores rotation count (1 per duty); weight is applied at read time via pr_duty_weight config.
    sprint_end is exclusive. Only rows where Team column contains 'Gemini' are included."""
    token = get_confluence_session_token()
    if not token:
        raise RuntimeError('No Confluence session token')

    page_id = _get_pr_page_id()
    if not page_id:
        raise RuntimeError('No PR Confluence page configured (set pr_confluence_url in settings)')

    req = urllib.request.Request(
        f'https://autodesk.atlassian.net/wiki/api/v2/pages/{page_id}?body-format=storage',
        method='GET',
        headers={
            'Cookie': f'cloud.session.token={token}',
            'Accept': 'application/json',
        }
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode('utf-8'))

    body = data.get('body', {}).get('storage', {}).get('value', '')
    s_start = date.fromisoformat(sprint_start_str)
    s_end = date.fromisoformat(sprint_end_str)

    tr_pattern = re.compile(r'<tr[^>]*>(.*?)</tr>', re.DOTALL)
    td_pattern = re.compile(r'<td[^>]*>(.*?)</td>', re.DOTALL)
    time_pattern = re.compile(r'<time\s+datetime="(\d{4}-\d{2}-\d{2})"')
    user_pattern = re.compile(r'ri:account-id="([^"]+)"')

    account_map = dict(get_account_id_map())
    team = get_team()
    account_map = _normalize_account_map(account_map, team)

    # Discover unmapped account IDs if needed
    all_account_ids = set(user_pattern.findall(body))
    unmapped = all_account_ids - set(account_map.keys())
    discovered_names = {}
    if unmapped:
        print(f'  > PR: {len(unmapped)} unmapped account IDs, fetching view format...')
        discovered_names = _discover_account_ids_from_view(page_id, token, team)
        for aid, name in discovered_names.items():
            if aid not in account_map:
                account_map[aid] = name

    pr_days = {}
    for tr_match in tr_pattern.finditer(body):
        tr_html = tr_match.group(1)
        tds = td_pattern.findall(tr_html)
        if len(tds) < 3:
            continue

        # First column is Team — only include "Gemini" rows
        team_cell = re.sub(r'<[^>]+>', '', tds[0]).strip().lower()
        if 'gemini' not in team_cell:
            continue

        # Extract date from the row
        pr_date = None
        time_m = time_pattern.search(tr_html)
        if time_m:
            try:
                pr_date = date.fromisoformat(time_m.group(1))
            except ValueError:
                pass
        if pr_date is None:
            # Try plain text date
            date_text = re.sub(r'<[^>]+>', '', tds[2]).strip()
            pr_date = _parse_pa_date(date_text)
        if pr_date is None:
            continue

        # Check if date falls within sprint [start, end)
        if pr_date < s_start or pr_date >= s_end:
            continue

        # Extract users from the Person column (second <td>)
        for account_id in user_pattern.findall(tds[1]):
            name = account_map.get(account_id)
            if name and name in team:
                if name not in pr_days:
                    pr_days[name] = {'days': 0, 'dates': []}
                pr_days[name]['days'] += 1  # count rotations; weight applied at read time
                pr_days[name]['dates'].append(pr_date.isoformat())

    # Auto-save discovered account ID mappings
    if discovered_names:
        cfg = load_team_config()
        aid_map = cfg.get('confluence_account_ids', {})
        changed = False
        for aid, nm in discovered_names.items():
            if aid not in aid_map and nm in team:
                aid_map[aid] = nm
                changed = True
        if changed:
            cfg['confluence_account_ids'] = aid_map
            save_team_config(cfg)
            invalidate_team_config_cache()
            print(f'  > PR: Auto-saved Confluence account IDs: {discovered_names}')

    return pr_days


def save_pr_schedule(sprint_start: str, sprint_end: str, pr: dict) -> None:
    """Write pr-schedule.json cache."""
    path = os.path.join(SCRIPT_DIR, 'pr-schedule.json')
    with open(path, 'w') as f:
        json.dump({
            'sprint_start': sprint_start,
            'sprint_end': sprint_end,
            'pr': pr,
        }, f, indent=2)


def check_pr_freshness() -> dict:
    """Check whether pr-schedule.json is fresh for the current sprint."""
    path = os.path.join(SCRIPT_DIR, 'pr-schedule.json')
    sprint, _ = get_future_sprint_info_cached(get_board_id())
    if not sprint:
        return {'fresh': False, 'reason': 'no_sprint', 'message': 'Cannot detect sprint'}
    sprint_start = sprint['startDate'][:10]
    sprint_end = sprint['endDate'][:10]
    base = {'sprint_start': sprint_start, 'sprint_end': sprint_end}

    if not os.path.exists(path):
        return {**base, 'fresh': False, 'reason': 'missing', 'message': 'No pr-schedule.json found'}

    age_hours = (time.time() - os.path.getmtime(path)) / 3600
    try:
        with open(path, 'r') as f:
            data = json.load(f)
        file_start = data.get('sprint_start', '')
    except Exception:
        return {**base, 'fresh': False, 'reason': 'corrupt', 'message': 'Cannot read pr-schedule.json'}

    if file_start != sprint_start:
        return {**base, 'fresh': False, 'reason': 'wrong_sprint',
                'message': 'Cached for ' + file_start + ', need ' + sprint_start}

    if age_hours >= 24:
        return {**base, 'fresh': False, 'reason': 'stale',
                'message': 'pr-schedule.json is ' + str(round(age_hours)) + 'h old (>24h)'}

    return {**base, 'fresh': True, 'age_hours': round(age_hours, 1)}


def get_future_sprint_info(board_id: int) -> tuple[dict | None, dict]:
    """Fetch future sprints from Jira Agile API.
    Returns (sprint_to_plan, backlog_sprints_map) where sprint_to_plan is
    {id, name, startDate, endDate} or None, and backlog_sprints_map is {id_str: name}."""
    req = urllib.request.Request(
        f'{JIRA_URL}/rest/agile/1.0/board/{board_id}/sprint?state=future',
        method='GET',
        headers={'Authorization': f'Bearer {JIRA_TOKEN}'}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        print(f'  x Failed to fetch future sprints: {e}')
        return None, {}

    sprints = body.get('values', [])
    pattern = re.compile(r'^\d{4}-\d{2}-\d{2} Gemini$')
    sprint_to_plan = None
    backlog = {}

    for s in sprints:
        name = s.get('name', '')
        if pattern.match(name):
            if sprint_to_plan is None:
                sprint_to_plan = s
            # If multiple match, take the earliest start date
            elif s.get('startDate', '') < sprint_to_plan.get('startDate', ''):
                sprint_to_plan = s
        else:
            backlog[str(s['id'])] = name

    return sprint_to_plan, backlog


_SPRINT_INFO_CACHE = None
_SPRINT_INFO_TIME = 0

def get_future_sprint_info_cached(board_id: int) -> tuple[dict | None, dict]:
    """Cached wrapper — avoids hammering Jira during polling (5-min TTL)."""
    global _SPRINT_INFO_CACHE, _SPRINT_INFO_TIME
    now = time.time()
    if _SPRINT_INFO_CACHE is not None and (now - _SPRINT_INFO_TIME) < 300:
        return _SPRINT_INFO_CACHE
    result = get_future_sprint_info(board_id)
    _SPRINT_INFO_CACHE = result
    _SPRINT_INFO_TIME = now
    return result


def check_absence_freshness() -> dict:
    """Check whether absences.json is fresh for the current sprint."""
    path = os.path.join(SCRIPT_DIR, 'absences.json')
    sprint, _ = get_future_sprint_info_cached(get_board_id())
    if not sprint:
        return {'fresh': False, 'reason': 'no_sprint', 'message': 'Cannot detect sprint'}
    sprint_start = sprint['startDate'][:10]
    sprint_end = sprint['endDate'][:10]
    base = {'sprint_start': sprint_start, 'sprint_end': sprint_end}

    if not os.path.exists(path):
        return {**base, 'fresh': False, 'reason': 'missing', 'message': 'No absences.json found'}

    age_hours = (time.time() - os.path.getmtime(path)) / 3600
    try:
        with open(path, 'r') as f:
            data = json.load(f)
        file_start = data.get('sprint_start', '')
    except Exception:
        return {**base, 'fresh': False, 'reason': 'corrupt', 'message': 'Cannot read absences.json'}

    if file_start != sprint_start:
        return {**base, 'fresh': False, 'reason': 'wrong_sprint',
                'message': 'Cached for ' + file_start + ', need ' + sprint_start}

    if age_hours >= 24:
        return {**base, 'fresh': False, 'reason': 'stale',
                'message': 'absences.json is ' + str(round(age_hours)) + 'h old (>24h)'}

    return {**base, 'fresh': True, 'age_hours': round(age_hours, 1)}


# ── HTTP handler ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(f'  {self.address_string()} {fmt % args}')

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin',  '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def _respond(self, code, data):
        body = json.dumps(data).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type',   'application/json')
        self.send_header('Content-Length', len(body))
        self._cors()
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionAbortedError, BrokenPipeError):
            pass

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        # ── /api/health ──
        if parsed.path == '/api/health':
            qs   = urllib.parse.parse_qs(parsed.query)
            skip = set(qs.get('skip', [''])[0].split(','))

            jira_ok = jira_err = None
            if 'jira' not in skip:
                jira_ok, jira_err = check_jira_health()

            docker_ok = docker_err = None
            if 'docker' not in skip:
                docker_ok, docker_err = check_docker()

            conf_ok = conf_user = conf_err = conf_needs_auth = None
            if 'confluence' not in skip:
                conf_ok, conf_user, conf_err, conf_needs_auth = check_confluence_auth()

            self._respond(200, {
                'server': 'ok',
                'jira':      jira_ok,   'jira_error':      jira_err,
                'docker':    docker_ok, 'docker_error':    docker_err,
                'confluence': conf_ok,  'confluence_error': conf_err,
                'confluence_user':       conf_user,
                'confluence_needs_auth': conf_needs_auth,
            })
            return

        # ── /oauth/reauth ── launch auth-confluence.py in headed Chrome
        if parsed.path == '/oauth/reauth':
            auth_script = os.path.join(SCRIPT_DIR, 'auth-confluence.py')
            try:
                subprocess.Popen(['python', auth_script], cwd=SCRIPT_DIR)
                print('  > Launched auth-confluence.py')
                self._respond(200, {'ok': True})
            except Exception as e:
                self._respond(500, {'ok': False, 'error': str(e)})
            return

        # ── /api/config ── return team config (re-reads file for freshness)
        if parsed.path == '/api/config':
            invalidate_team_config_cache()
            self._respond(200, load_team_config())
            return

        # ── /api/team-status ── check if team is configured (re-reads file, not cache)
        if parsed.path == '/api/team-status':
            invalidate_team_config_cache()  # Force re-read in case fetch-team.py updated it
            cfg = load_team_config()
            team = cfg.get('team', [])
            self._respond(200, {
                'configured': len(team) > 0, 'count': len(team), 'team': team,
                'pa_enabled': cfg.get('pa_enabled', False),
                'pa_confluence_url': cfg.get('pa_confluence_url', ''),
                'pr_enabled': cfg.get('pr_enabled', False),
                'pr_confluence_url': cfg.get('pr_confluence_url', ''),
                'pr_duty_weight': cfg.get('pr_duty_weight', 0.5),
            })
            return

        # ── /api/fetch-team ── launch fetch-team.py in headed Chrome
        if parsed.path == '/api/fetch-team':
            team_script = os.path.join(SCRIPT_DIR, 'fetch-team.py')
            try:
                subprocess.Popen(['python', team_script], cwd=SCRIPT_DIR)
                print('  > Launched fetch-team.py')
                self._respond(200, {'ok': True})
            except Exception as e:
                self._respond(500, {'ok': False, 'error': str(e)})
            return

        # ── /api/absence-status ── check if absences.json is fresh
        if parsed.path == '/api/absence-status':
            result = check_absence_freshness()
            self._respond(200, result)
            return

        # ── /api/fetch-absences ── launch fetch-absences.py in headed Chrome
        if parsed.path == '/api/fetch-absences':
            sprint, _ = get_future_sprint_info_cached(get_board_id())
            if not sprint:
                self._respond(500, {'ok': False, 'error': 'Cannot detect sprint dates'})
                return
            start = sprint['startDate'][:10]
            end = sprint['endDate'][:10]
            abs_script = os.path.join(SCRIPT_DIR, 'fetch-absences.py')
            try:
                subprocess.Popen(['python', abs_script, start, end], cwd=SCRIPT_DIR)
                print(f'  > Launched fetch-absences.py {start} {end}')
                self._respond(200, {'ok': True, 'sprint_start': start, 'sprint_end': end})
            except Exception as e:
                self._respond(500, {'ok': False, 'error': str(e)})
            return

        # ── /api/pa-status ── check if pa-schedule.json is fresh
        if parsed.path == '/api/pa-status':
            result = check_pa_freshness()
            self._respond(200, result)
            return

        # ── /api/fetch-pa ── fetch PA schedule from Confluence and cache it
        if parsed.path == '/api/fetch-pa':
            sprint, _ = get_future_sprint_info_cached(get_board_id())
            if not sprint:
                self._respond(500, {'ok': False, 'error': 'Cannot detect sprint dates'})
                return
            start = sprint['startDate'][:10]
            end = sprint['endDate'][:10]
            try:
                pa = fetch_pa_from_confluence(start, end)
                save_pa_schedule(start, end, pa)
                print(f'  > PA schedule fetched: {pa}')
                self._respond(200, {'ok': True, 'pa': pa})
            except Exception as e:
                print(f'  x PA fetch failed: {e}')
                self._respond(500, {'ok': False, 'error': str(e)})
            return

        # ── /api/pr-status ── check if pr-schedule.json is fresh
        if parsed.path == '/api/pr-status':
            result = check_pr_freshness()
            self._respond(200, result)
            return

        # ── /api/fetch-pr ── fetch PR schedule from Confluence and cache it
        if parsed.path == '/api/fetch-pr':
            sprint, _ = get_future_sprint_info_cached(get_board_id())
            if not sprint:
                self._respond(500, {'ok': False, 'error': 'Cannot detect sprint dates'})
                return
            start = sprint['startDate'][:10]
            end = sprint['endDate'][:10]
            try:
                pr = fetch_pr_from_confluence(start, end)
                save_pr_schedule(start, end, pr)
                print(f'  > PR schedule fetched: {pr}')
                self._respond(200, {'ok': True, 'pr': pr})
            except Exception as e:
                print(f'  x PR fetch failed: {e}')
                self._respond(500, {'ok': False, 'error': str(e)})
            return

        # ── /api/sprint-issues ──
        if parsed.path == '/api/sprint-issues':
            qs           = urllib.parse.parse_qs(parsed.query)
            sprints_param = qs.get('sprints', [''])[0]
            sprint_ids   = [int(s) for s in sprints_param.split(',') if s.strip().lstrip('-').isdigit()]
            issues_by_sprint = {}
            for sid in sprint_ids:
                issues = get_issues_for_sprint(sid)
                print(f'  > Sprint {sid}: {len(issues)} issues')
                issues_by_sprint[str(sid)] = issues
            # Include Jira priorities (fetched + cached on first call)
            priorities = fetch_jira_priorities()
            self._respond(200, {'sprints': issues_by_sprint, 'priorities': priorities})
            return

        # ── /api/sprint-info ──
        if parsed.path == '/api/sprint-info':
            sprint, backlog = get_future_sprint_info_cached(get_board_id())
            if not sprint:
                self._respond(404, {'error': 'No future Gemini sprint found'})
                return
            start_str = sprint.get('startDate', '')[:10]
            end_str = sprint.get('endDate', '')[:10]
            working_days, holidays_in_sprint = compute_working_days(start_str, end_str)
            cfg = load_team_config()
            team = cfg.get('team', [])
            abs_data = load_absences()
            absences = {name: abs_data.get(name, 0) for name in team}
            abs_detail = load_absence_detail()
            pa_data = load_pa_schedule()
            pa = {name: pa_data.get(name, 0) for name in team}
            pa_full = load_pa_schedule_full()
            pr_data = load_pr_schedule()
            pr = {name: pr_data.get(name, 0) for name in team}
            pr_full = load_pr_schedule_full()
            print(f'  > Sprint info: {sprint["name"]} ({start_str} to {end_str}), {working_days} working days')
            self._respond(200, {
                'sprint_id': sprint['id'],
                'sprint_name': sprint['name'],
                'start_date': start_str,
                'end_date': end_str,
                'working_days': working_days,
                'holidays_in_sprint': holidays_in_sprint,
                'absences': absences,
                'absence_detail': {name: abs_detail.get(name, {'days': 0, 'dates': []}) for name in team},
                'pa': pa,
                'pa_detail': {name: pa_full.get(name, {'days': 0, 'dates': []}) for name in team},
                'pr': pr,
                'pr_detail': {name: pr_full.get(name, {'days': 0, 'dates': []}) for name in team},
                'backlog_sprints': backlog,
                'team': team,
                'efficiency': get_efficiency_map(),
                'pa_enabled': cfg.get('pa_enabled', False),
                'pa_confluence_url': cfg.get('pa_confluence_url', ''),
                'pr_enabled': cfg.get('pr_enabled', False),
                'pr_confluence_url': cfg.get('pr_confluence_url', ''),
                'pr_duty_weight': cfg.get('pr_duty_weight', 0.5),
                'unscheduled_buffer': cfg.get('unscheduled_buffer', 5),
            })
            return

        # ── /api/sp ──
        # ── /api/backlog-prefs ── read saved backlog selections
        if parsed.path == '/api/backlog-prefs':
            path = os.path.join(SCRIPT_DIR, 'backlog-prefs.json')
            try:
                with open(path, 'r') as f:
                    prefs = json.load(f)
            except Exception:
                prefs = {}
            self._respond(200, prefs)
            return

        if parsed.path != '/api/sp':
            self._respond(404, {'error': 'Not found'})
            return

        qs         = urllib.parse.parse_qs(parsed.query)
        keys_param = qs.get('issues', [''])[0]
        if not keys_param.strip():
            self._respond(400, {'error': 'Missing issues parameter'})
            return

        issue_keys = [k.strip() for k in keys_param.split(',') if k.strip()]
        print(f'  > Fetching SP for {len(issue_keys)} issues ...')
        status, sp_map = get_story_points(issue_keys)
        if status == 200:
            print(f'  ok SP fetched for {len(sp_map)} issues')
            self._respond(200, sp_map)
        else:
            print(f'  x SP fetch failed (HTTP {status})')
            self._respond(502, {'error': f'Jira returned HTTP {status}'})

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body   = self.rfile.read(length)

        # ── /api/config ── save team config
        if self.path == '/api/config':
            try:
                data = json.loads(body)
                cfg = load_team_config()
                # Only update allowed fields
                for key in ('board_id', 'board_url', 'team', 'efficiency', 'confluence_account_ids', 'pa_enabled', 'pa_confluence_url', 'pr_enabled', 'pr_confluence_url', 'pr_duty_weight', 'unscheduled_buffer'):
                    if key in data:
                        cfg[key] = data[key]
                save_team_config(cfg)
                # Invalidate sprint cache since board_id may have changed
                global _SPRINT_INFO_CACHE
                _SPRINT_INFO_CACHE = None
                self._respond(200, {'ok': True})
            except Exception as e:
                self._respond(500, {'ok': False, 'error': str(e)})
            return

        # ── /api/efficiency ── save a single person's efficiency
        if self.path == '/api/efficiency':
            try:
                data = json.loads(body)
                name = data['name']
                value = int(data['value'])
                cfg = load_team_config()
                if 'efficiency' not in cfg:
                    cfg['efficiency'] = {'default': 70}
                if value == cfg['efficiency'].get('default', 70):
                    # Remove override if it matches default
                    cfg['efficiency'].pop(name, None)
                else:
                    cfg['efficiency'][name] = value
                save_team_config(cfg)
                self._respond(200, {'ok': True})
            except Exception as e:
                self._respond(500, {'ok': False, 'error': str(e)})
            return

        # ── /api/backlog-prefs ── save backlog selections
        if self.path == '/api/backlog-prefs':
            try:
                data = json.loads(body)
                path = os.path.join(SCRIPT_DIR, 'backlog-prefs.json')
                with open(path, 'w') as f:
                    json.dump(data, f, indent=2)
                self._respond(200, {'ok': True})
            except Exception as e:
                self._respond(500, {'ok': False, 'error': str(e)})
            return

        # ── /api/edit ──
        if self.path == '/api/edit':
            try:
                data      = json.loads(body)
                issue_key = data['issue_key']
            except Exception as e:
                self._respond(400, {'error': f'Bad request: {e}'}); return

            fields = {}
            if 'sp' in data:
                sp_val   = data['sp']
                sp_float = float(sp_val) if sp_val is not None else None
                fields['customfield_10130'] = sp_float
                if sp_float is None:
                    # Clearing SP: clear all three
                    fields['timetracking'] = {'originalEstimate': '', 'remainingEstimate': ''}
                else:
                    # SP=0: originalEstimate='4h'; SP>0: derived from SP value
                    orig_est    = '4h' if sp_float == 0 else _sp_to_estimate(sp_float)
                    sp_secs     = 14400 if sp_float == 0 else int(sp_float * 8 * 3600)
                    logged_secs = get_time_spent(issue_key)
                    rem_secs    = max(0, sp_secs - logged_secs)
                    fields['timetracking'] = {
                        'originalEstimate': orig_est,
                        'remainingEstimate': _secs_to_estimate(rem_secs),
                    }
            if 'assignee' in data:
                name = (data['assignee'] or '').strip()
                if name:
                    username, err = find_user_name(name)
                    if err:
                        print(f'  x User lookup failed for "{name}": {err}')
                        self._respond(400, {'error': f'Cannot assign: {err}'}); return
                    print(f'  > Resolved "{name}" -> "{username}"')
                    fields['assignee'] = {'name': username}
                else:
                    fields['assignee'] = None  # unassign
            if 'priority' in data:
                pri_id = data.get('priority_id', '')
                if pri_id:
                    fields['priority'] = {'id': pri_id}
                else:
                    fields['priority'] = {'name': data['priority']}

            if not fields:
                self._respond(400, {'error': 'No fields to update'}); return

            print(f'  > Editing {issue_key}: {list(fields.keys())}')
            status, msg = update_issue_fields(issue_key, fields)
            if status in (200, 201, 204):
                print(f'  ok {issue_key} updated')
                self._respond(200, {'ok': True})
            else:
                print(f'  x {issue_key} edit failed (HTTP {status}): {msg[:120]}')
                self._respond(502, {'ok': False, 'error': msg[:200]})
            return

        # ── /api/move ──
        if self.path != '/api/move':
            self._respond(404, {'error': 'Not found'})
            return

        try:
            data      = json.loads(body)
            issue_key = data['issue_key']
            sprint_id = int(data['sprint_id'])
        except Exception as e:
            self._respond(400, {'error': f'Bad request: {e}'})
            return

        print(f'  > Moving {issue_key} to sprint {sprint_id} ...')
        status, msg = move_issue_to_sprint(issue_key, sprint_id)

        if status in (200, 201, 204):
            print(f'  ok {issue_key} moved (HTTP {status})')
            self._respond(200, {'ok': True, 'issue_key': issue_key, 'sprint_id': sprint_id})
        else:
            print(f'  x {issue_key} failed (HTTP {status}): {msg[:120]}')
            self._respond(502, {'ok': False, 'error': msg[:200]})


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    token = get_confluence_session_token()
    server = HTTPServer(('localhost', PORT), Handler)
    print(f'Sprint server running on http://localhost:{PORT}')
    print(f'Jira:      {JIRA_URL}')
    print(f'Confluence: {"session token stored" if token else "not authenticated - open sprint-plan.html to authenticate"}')
    print('Open sprint-plan.html in Chrome, then drag issues to sync with Jira.')
    print('Ctrl+C to stop.\n')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nStopped.')
