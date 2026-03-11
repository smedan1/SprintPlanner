"""
Confluence session authenticator.

Opens a headed Chrome window, navigates to Confluence, waits for you to
complete the Autodesk SSO login, then extracts the session cookie and saves
it to .mcp.json so sprint-server.py can make authenticated API calls.

Usage:
    python auth-confluence.py
"""

import json
import os
from playwright.sync_api import sync_playwright

SCRIPT_DIR     = os.path.dirname(os.path.abspath(__file__))
MCP_JSON       = os.path.join(SCRIPT_DIR, '.mcp.json')
CONFLUENCE_URL = 'https://autodesk.atlassian.net/wiki'
SESSION_COOKIE = 'cloud.session.token'


def main():
    print('Opening Chrome for Confluence SSO login…')
    print('Complete the login in the browser. The window will close automatically.\n')

    with sync_playwright() as p:
        browser = p.chromium.launch(channel='chrome', headless=False)
        context = browser.new_context()
        page    = context.new_page()

        page.goto(CONFLUENCE_URL)

        # Wait up to 2 minutes for SSO to complete and land back on Confluence
        print('Waiting for SSO redirect to complete…')
        page.wait_for_url('https://autodesk.atlassian.net/wiki/**', timeout=120_000)
        page.wait_for_load_state('networkidle', timeout=15_000)

        cookies = {c['name']: c['value'] for c in context.cookies()}
        token   = cookies.get(SESSION_COOKIE, '')
        browser.close()

    if not token:
        print(f'\nERROR: {SESSION_COOKIE!r} cookie not found after login.')
        print('Available cookies:', ', '.join(cookies.keys()) or '(none)')
        return

    with open(MCP_JSON, 'r') as f:
        cfg = json.load(f)
    cfg.setdefault('confluence', {})['session_token'] = token
    with open(MCP_JSON, 'w') as f:
        json.dump(cfg, f, indent=2)

    print(f'\n✓ Session token saved to .mcp.json')
    print('Return to sprint-plan.html — the Confluence check will now pass.')


if __name__ == '__main__':
    main()
