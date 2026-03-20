"""
HLTV Utilities

Helper functions for getting scorebot connection details from HLTV match pages.
"""

import re
import httpx


async def get_scorebot_info(match_url_or_id: str) -> dict:
    """Scrape the HLTV match page to get the scorebot listId and URL.

    The match URL ID (e.g. 2390814) is NOT the same as the scorebot listId.
    We need to scrape the match page to find:
    - data-scorebot-url (the WebSocket endpoint)
    - data-scorebot-id (the listId to subscribe to)

    Args:
        match_url_or_id: Either a full HLTV URL or just the match ID number.

    Returns:
        dict with 'list_id', 'scorebot_url', 'team1', 'team2'
    """
    # Normalize input
    if match_url_or_id.startswith("http"):
        url = match_url_or_id
    else:
        url = f"https://www.hltv.org/matches/{match_url_or_id}/match"

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.hltv.org/",
    }

    async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        html = resp.text

    result = {
        "list_id": None,
        "scorebot_url": None,
        "team1": None,
        "team2": None,
    }

    # Look for scorebot data attributes
    # Pattern: data-scorebot-id="12345"
    id_match = re.search(r'data-scorebot-id="(\d+)"', html)
    if id_match:
        result["list_id"] = id_match.group(1)

    # Pattern: data-scorebot-url="https://..."
    url_match = re.search(r'data-scorebot-url="([^"]+)"', html)
    if url_match:
        result["scorebot_url"] = url_match.group(1)

    # Try to get team names
    team_matches = re.findall(r'class="teamName"[^>]*>([^<]+)<', html)
    if len(team_matches) >= 2:
        result["team1"] = team_matches[0].strip()
        result["team2"] = team_matches[1].strip()

    return result


async def get_live_matches() -> list[dict]:
    """Get currently live matches from HLTV.

    Returns a list of dicts with match info.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://www.hltv.org/",
    }

    async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
        resp = await client.get("https://www.hltv.org/matches", headers=headers)
        resp.raise_for_status()
        html = resp.text

    # Look for live match links
    matches = []
    live_sections = re.findall(
        r'<a[^>]*href="(/matches/(\d+)/[^"]*)"[^>]*class="[^"]*liveMatch[^"]*"',
        html
    )
    for href, match_id in live_sections:
        matches.append({
            "match_id": match_id,
            "url": f"https://www.hltv.org{href}",
        })

    return matches
