"""
Live HLTV Score Tracker using undetected-chromedriver + CDP.

Opens the HLTV match page in a real Chrome browser (bypasses Cloudflare),
captures WebSocket frames via Chrome DevTools Protocol performance logs,
and logs all scorebot events with ms timestamps.

Scrapes map picks from the HLTV page to determine map numbers.
Persists across the entire series — only stops when the series is over.

Usage:
    python3 hltv_live.py https://www.hltv.org/matches/2391757/match --output data/2026-03-20/falcons_vs_navi
"""

import argparse
import json
import time
from pathlib import Path

import undetected_chromedriver as uc


def append_jsonl(filepath: Path, obj: dict):
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "a") as f:
        f.write(json.dumps(obj) + "\n")
        f.flush()


def scrape_map_picks(driver):
    """Scrape map veto section from HLTV page.
    Returns (map_lookup, completed_scores, best_of) or (None, None, None) if not found.

    map_lookup: {"mirage": 1, "ancient": 2, "anubis": 3}
    completed_scores: [(16, 7), None, None]  -- None means not played yet
    best_of: 3
    """
    try:
        result = driver.execute_script("""
            // Get best_of from the veto text
            const vetoBox = document.querySelector('.veto-box, .standard-box');
            let bestOf = 3;
            if (vetoBox) {
                const text = vetoBox.textContent;
                const boMatch = text.match(/Best of (\\d+)/i);
                if (boMatch) bestOf = parseInt(boMatch[1]);
            }

            // Get map holders - these are the actual maps to be played (not bans)
            const holders = document.querySelectorAll('.mapholder');
            const maps = [];
            for (const h of holders) {
                const nameEl = h.querySelector('.mapname');
                if (!nameEl) continue;
                const name = nameEl.textContent.trim();
                if (!name || name === 'TBA') continue;

                // Get scores if available
                const scoreEls = h.querySelectorAll('.results-team-score');
                let scores = null;
                if (scoreEls.length >= 2) {
                    const s1 = parseInt(scoreEls[0].textContent);
                    const s2 = parseInt(scoreEls[1].textContent);
                    if (!isNaN(s1) && !isNaN(s2)) {
                        scores = [s1, s2];
                    }
                }
                maps.push({name: name, scores: scores});
            }
            // Get team names from the page header
            const teamEls = document.querySelectorAll('.teamName');
            let team1 = null, team2 = null;
            if (teamEls.length >= 2) {
                team1 = teamEls[0].textContent.trim();
                team2 = teamEls[1].textContent.trim();
            }
            return {maps: maps, bestOf: bestOf, team1: team1, team2: team2};
        """)

        if not result or not result.get("maps"):
            return None, None, None

        maps = result["maps"]
        best_of = result.get("bestOf", 3)

        if len(maps) == 0:
            return None, None, None

        # Build lookup: normalize map name -> map number
        # HLTV page shows "Mirage", scorebot sends "de_mirage"
        map_lookup = {}
        completed_scores = []
        for i, m in enumerate(maps):
            name = m["name"].lower().strip()
            map_lookup[name] = i + 1
            completed_scores.append(tuple(m["scores"]) if m["scores"] else None)

        page_teams = (result.get("team1"), result.get("team2"))
        return map_lookup, completed_scores, best_of, page_teams

    except Exception as e:
        print(f"[WARN] Failed to scrape map picks: {e}")
        return None, None, None, (None, None)


class HLTVTracker:
    def __init__(self, output_dir: str, best_of: int = 3):
        self.out = Path(output_dir)
        self.out.mkdir(parents=True, exist_ok=True)

        self.current_map = None
        self.map_count = 0  # current map number
        self.logfile = None
        self.last_score = None
        self.last_round = None
        self.team1_name = None
        self.round_has_first_kill = False
        self.ct_players = set()
        self.t_players = set()

        # Series tracking
        self.best_of = best_of
        self.map_picks = None   # {"mirage": 1, "ancient": 2, ...}
        self.map_wins = {}      # {"TeamA": 2, "TeamB": 1}
        self.series_over = False

    def _write_map_state(self, active_map=None, status="waiting"):
        """Write current map state to map_state.json for the Polymarket recorder."""
        state = {
            "status": status,  # "waiting", "live", "map_ended", "series_over"
            "active_map": active_map,  # e.g. "map2"
            "ts": time.time(),
        }
        (self.out / "map_state.json").write_text(json.dumps(state))

    def set_map_picks(self, map_lookup, completed_scores, best_of, page_teams=(None, None)):
        """Set map picks from page scrape."""
        self.map_picks = map_lookup
        self.best_of = best_of
        self.page_team1, self.page_team2 = page_teams
        print(f"\n[VETO] Maps: {map_lookup}")
        print(f"[VETO] Best of {best_of}")

        # Seed map_wins from completed scores and check if series is already over
        if completed_scores:
            team1_wins = 0
            team2_wins = 0
            for i, score in enumerate(completed_scores):
                if score is not None:
                    s1, s2 = score
                    if s1 > s2:
                        team1_wins += 1
                        print(f"  Map {i+1}: {s1}-{s2} (team1 won)")
                    elif s2 > s1:
                        team2_wins += 1
                        print(f"  Map {i+1}: {s1}-{s2} (team2 won)")

            wins_needed = (best_of + 1) // 2
            if team1_wins >= wins_needed or team2_wins >= wins_needed:
                print(f"\n[ALREADY OVER] Series already finished ({team1_wins}-{team2_wins})")
                self.series_over = True
                self._write_map_state(None, "series_over")

            self.completed_scores = completed_scores

    def _resolve_map_number(self, map_name: str) -> int:
        """Look up map number from scraped picks, or fall back to counter."""
        if self.map_picks:
            # scorebot sends "de_mirage", picks have "mirage"
            short = map_name.lower().replace("de_", "").strip()
            if short in self.map_picks:
                return self.map_picks[short]
            # Try fuzzy match
            for pick_name, num in self.map_picks.items():
                if short in pick_name or pick_name in short:
                    return num

        # Fallback: increment counter
        self.map_count += 1
        return self.map_count

    def _on_new_map(self, map_name: str):
        """Reset per-map state when a new map starts."""
        if map_name != self.current_map:
            map_num = self._resolve_map_number(map_name)
            self.map_count = map_num
            map_dir = self.out / f"map{map_num}"
            map_dir.mkdir(parents=True, exist_ok=True)
            self.logfile = map_dir / "hltv_events.jsonl"
            if self.current_map is not None:
                print(f"\n{'='*60}")
                print(f"New map detected: {map_name} -> map{map_num}/")
                print(f"{'='*60}\n")
            else:
                print(f"Map: {map_name} -> map{map_num}/")
            self.current_map = map_name
            self.last_score = None
            self.last_round = None
            self.team1_name = None
            self._write_map_state(f"map{map_num}", "live")

    def process_frame(self, payload: str, chrome_ts: float):
        """Process a WebSocket frame from the scorebot."""
        if not payload.startswith("42"):
            return None

        try:
            data = json.loads(payload[2:])
            if not isinstance(data, list) or len(data) < 2:
                return None

            event_name = data[0]
            event_data = data[1]

            if isinstance(event_data, str):
                event_data = json.loads(event_data)

            ts_ms = int(time.time() * 1000)
            ts_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts_ms / 1000))

            if event_name == "scoreboard":
                return self._handle_scoreboard(event_data, ts_ms, ts_iso)
            elif event_name == "log":
                return self._handle_log(event_data, ts_ms, ts_iso)

        except (json.JSONDecodeError, TypeError, KeyError):
            pass

        return None

    def _handle_scoreboard(self, data: dict, ts_ms: int, ts_iso: str):
        t_name = data.get("terroristTeamName", "?")
        ct_name = data.get("ctTeamName", "?")
        t_score = data.get("tTeamScore", 0)
        ct_score = data.get("ctTeamScore", 0)
        current_round = data.get("currentRound", 0)
        map_name = data.get("mapName", "?")
        bomb_planted = data.get("bombPlanted", False)

        # Detect map changes
        self._on_new_map(map_name)

        # Lock team1 to starting T side per map
        if self.team1_name is None:
            self.team1_name = t_name
            # Seed map_wins from completed scores now that we know team names
            if hasattr(self, 'completed_scores') and self.completed_scores and not self.map_wins:
                # Match page team names to scorebot team names
                page_t1 = getattr(self, 'page_team1', None)
                page_t2 = getattr(self, 'page_team2', None)
                scorebot_teams = {t_name, ct_name}
                team_map = {}
                for page_name, label in [(page_t1, "page_team1"), (page_t2, "page_team2")]:
                    if page_name:
                        pn = page_name.lower().replace(" ", "")
                        for st in scorebot_teams:
                            if st.lower().replace(" ", "") == pn or pn in st.lower().replace(" ", "") or st.lower().replace(" ", "") in pn:
                                team_map[label] = st
                                break
                if "page_team1" in team_map and "page_team2" in team_map:
                    for i, score in enumerate(self.completed_scores):
                        if score is not None:
                            s1, s2 = score
                            if s1 > s2:
                                w = team_map["page_team1"]
                                self.map_wins[w] = self.map_wins.get(w, 0) + 1
                            elif s2 > s1:
                                w = team_map["page_team2"]
                                self.map_wins[w] = self.map_wins.get(w, 0) + 1
                    if self.map_wins:
                        print(f"  Seeded series score from completed maps: {self.map_wins}")

        # Map to consistent team1/team2
        if self.team1_name == t_name:
            t1_name, t1_score = t_name, t_score
            t2_name, t2_score = ct_name, ct_score
        else:
            t1_name, t1_score = ct_name, ct_score
            t2_name, t2_score = t_name, t_score

        # Track player rosters and team names for kill attribution
        self.ct_players = set(p.get("nick", p.get("name", "")) for p in data.get("CT", []))
        self.t_players = set(p.get("nick", p.get("name", "")) for p in data.get("TERRORIST", []))
        self.ct_team_name = ct_name
        self.t_team_name = t_name

        current_score = (t1_score, t2_score)
        score_changed = current_score != self.last_score
        round_changed = current_round != self.last_round

        # Reset first kill flag when round changes
        if round_changed:
            self.round_has_first_kill = False

        if score_changed or round_changed:
            t_alive = sum(1 for p in data.get("TERRORIST", []) if p.get("alive", False))
            ct_alive = sum(1 for p in data.get("CT", []) if p.get("alive", False))

            entry = {
                "ts_ms": ts_ms,
                "ts_iso": ts_iso,
                "type": "scoreboard",
                "event": "score_change" if score_changed else "round_update",
                "round": current_round,
                "map": map_name,
                "team1": t1_name,
                "team1_score": t1_score,
                "team2": t2_name,
                "team2_score": t2_score,
                "t_side": t_name,
                "ct_side": ct_name,
                "bomb_planted": bomb_planted,
                "t_alive": t_alive,
                "ct_alive": ct_alive,
            }

            append_jsonl(self.logfile, entry)
            self.last_score = current_score
            self.last_round = current_round

            status = f"💣" if bomb_planted else ""
            alive = f"[{t_alive}v{ct_alive}]" if t_alive + ct_alive < 10 else ""
            print(
                f"[{ts_iso}] R{current_round}: "
                f"{t1_name} {t1_score} - {t2_score} {t2_name} "
                f"({map_name}) {alive} {status}"
            )

            # Detect map end
            hi = max(t1_score, t2_score)
            lo = min(t1_score, t2_score)
            if lo <= 11:
                target = 13
            else:
                ot_num = ((lo - 11) + 2) // 3
                target = 13 + 3 * ot_num
            if hi == target and t1_score != t2_score:
                winner = t1_name if t1_score > t2_score else t2_name
                ot_str = " (OT)" if hi > 13 else ""
                print(f"\n[MAP DONE] {winner} wins {t1_score}-{t2_score}{ot_str}!")

                # Track series score
                self.map_wins[winner] = self.map_wins.get(winner, 0) + 1
                wins_needed = (self.best_of + 1) // 2

                if max(self.map_wins.values()) >= wins_needed:
                    print(f"\n[SERIES OVER] {winner} wins the series!")
                    for team, wins in self.map_wins.items():
                        print(f"  {team}: {wins} map(s)")
                    self.series_over = True
                    self._write_map_state(None, "series_over")
                    return "stop"
                else:
                    print(f"  Series: {self.map_wins}")
                    print(f"  Waiting for next map...")
                    self._write_map_state(None, "map_ended")
                    self.current_map = None  # reset so _on_new_map fires again
                    return None

        return None

    def _handle_log(self, data: dict, ts_ms: int, ts_iso: str):
        if self.current_map is None:
            return None

        log_entries = data.get("log", [])

        for log_entry in log_entries:
            if "RoundStarted" in log_entry or "RoundStart" in log_entry:
                self.round_has_first_kill = False
                entry = {
                    "ts_ms": ts_ms,
                    "ts_iso": ts_iso,
                    "type": "round_start",
                }
                append_jsonl(self.logfile, entry)
                print(f"[{ts_iso}] ROUND START")

            elif "RoundEnd" in log_entry:
                re = log_entry["RoundEnd"]
                entry = {
                    "ts_ms": ts_ms,
                    "ts_iso": ts_iso,
                    "type": "round_end",
                    "ct_score": re.get("counterTerroristScore"),
                    "t_score": re.get("terroristScore"),
                    "winner": re.get("winner"),
                    "win_type": re.get("winType"),
                }
                append_jsonl(self.logfile, entry)
                print(f"[{ts_iso}] ROUND END: {re.get('winner')} wins ({re.get('winType')})")

            elif "Kill" in log_entry:
                kill = log_entry["Kill"]
                killer_name = kill.get("killerName", "")
                is_first_kill = not self.round_has_first_kill
                self.round_has_first_kill = True

                victim_name = kill.get("victimName", "")
                if victim_name in self.ct_players:
                    victim_team = getattr(self, 'ct_team_name', '?')
                elif victim_name in self.t_players:
                    victim_team = getattr(self, 't_team_name', '?')
                else:
                    victim_team = "?"

                if victim_team == getattr(self, 'ct_team_name', ''):
                    killer_side = "TERRORIST"
                    killer_team = getattr(self, 't_team_name', '?')
                elif victim_team == getattr(self, 't_team_name', ''):
                    killer_side = "CT"
                    killer_team = getattr(self, 'ct_team_name', '?')
                elif killer_name in self.ct_players:
                    # Victim unknown, but we know the killer's side
                    killer_side = "CT"
                    killer_team = getattr(self, 'ct_team_name', '?')
                elif killer_name in self.t_players:
                    killer_side = "TERRORIST"
                    killer_team = getattr(self, 't_team_name', '?')
                else:
                    killer_side = "?"
                    killer_team = "?"

                entry = {
                    "ts_ms": ts_ms,
                    "ts_iso": ts_iso,
                    "type": "kill",
                    "killer": killer_name,
                    "killer_side": killer_side,
                    "killer_team": killer_team,
                    "victim": kill.get("victimName"),
                    "weapon": kill.get("weapon"),
                    "headshot": kill.get("headShot", False),
                    "first_kill": is_first_kill,
                }
                append_jsonl(self.logfile, entry)
                fk_tag = " [FK]" if is_first_kill else ""
                hs_tag = " (HS)" if kill.get("headShot", False) else ""
                print(f"[{ts_iso}] KILL: {killer_name} → {kill.get('victimName', '?')} [{kill.get('weapon', '?')}]{hs_tag}{fk_tag}")

            elif "BombPlanted" in log_entry:
                entry = {
                    "ts_ms": ts_ms,
                    "ts_iso": ts_iso,
                    "type": "bomb_planted",
                }
                append_jsonl(self.logfile, entry)

        return None


def main():
    parser = argparse.ArgumentParser(description="Live HLTV score tracker")
    parser.add_argument("url", help="HLTV match URL")
    parser.add_argument("--output", required=True, help="Base output directory (e.g. data/2026-03-20/falcons_vs_navi)")
    parser.add_argument("--best-of", type=int, default=3, help="Best of N series (1, 3, or 5)")
    parser.add_argument("--polymarket-url", default=None, help="Polymarket URL to open in a tab")
    parser.add_argument("--interval", type=float, default=0.5, help="Poll interval in seconds")
    args = parser.parse_args()

    tracker = HLTVTracker(args.output, best_of=args.best_of)

    print(f"Opening HLTV: {args.url}")
    print(f"Base output: {args.output}")
    print(f"Best of {args.best_of}")
    print()

    options = uc.ChromeOptions()
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    driver = uc.Chrome(headless=False, options=options, version_main=146)
    driver.get(args.url)
    print("Waiting for page to load...")
    time.sleep(10)

    title = driver.title
    if "Just a moment" in title:
        print("[ERROR] Blocked by Cloudflare. Try again.")
        driver.quit()
        return

    print(f"[OK] Page loaded: {title}")

    # Open extra tabs for convenience
    if args.polymarket_url:
        driver.execute_cdp_cmd("Target.createTarget", {"url": args.polymarket_url})
    driver.execute_cdp_cmd("Target.createTarget", {"url": "http://localhost:8888/live.html"})

    # Try initial map pick scrape
    map_lookup, completed_scores, best_of, page_teams = scrape_map_picks(driver)
    if map_lookup:
        tracker.set_map_picks(map_lookup, completed_scores, best_of, page_teams)
        if tracker.series_over:
            print("Nothing to record. Exiting.")
            driver.quit()
            return
    else:
        print("[INFO] Map picks not available yet, will retry on reload...")

    print("Waiting for scorebot connection (reloading every 60s)...\n")

    try:
        scorebot_found = False
        last_reload = time.time()
        driver.get_log("performance")  # drain initial logs
        while not scorebot_found:
            logs = driver.get_log("performance")
            for entry in logs:
                msg = json.loads(entry["message"])["message"]
                if msg.get("method") == "Network.webSocketFrameReceived":
                    payload = msg.get("params", {}).get("response", {}).get("payloadData", "")
                    if payload and ("scoreboard" in payload.lower() or "mapName" in payload):
                        scorebot_found = True
                        chrome_ts = msg.get("params", {}).get("timestamp", 0)
                        tracker.process_frame(payload, chrome_ts)
                        break
            if not scorebot_found:
                if time.time() - last_reload >= 60:
                    # Re-scrape map picks on reload if not found yet
                    if not tracker.map_picks:
                        ml, cs, bo, pt = scrape_map_picks(driver)
                        if ml:
                            tracker.set_map_picks(ml, cs, bo, pt)
                            if tracker.series_over:
                                print("Nothing to record. Exiting.")
                                driver.quit()
                                return
                    print(f"[{time.strftime('%H:%M:%S')}] No scorebot yet, reloading...")
                    driver.refresh()
                    time.sleep(5)
                    driver.get_log("performance")
                    last_reload = time.time()
                time.sleep(args.interval)

        # One more scrape attempt after scorebot connects (page fully loaded)
        if not tracker.map_picks:
            ml, cs, bo, pt = scrape_map_picks(driver)
            if ml:
                tracker.set_map_picks(ml, cs, bo, pt)
                if tracker.series_over:
                    print("Nothing to record. Exiting.")
                    driver.quit()
                    return

        print("Scorebot connected! Listening for events...\n")

        done = False
        while not done:
            logs = driver.get_log("performance")

            for entry in logs:
                msg = json.loads(entry["message"])["message"]
                method = msg.get("method", "")

                if method == "Network.webSocketFrameReceived":
                    params = msg.get("params", {})
                    payload = params.get("response", {}).get("payloadData", "")
                    chrome_ts = params.get("timestamp", 0)

                    if payload and len(payload) > 10:
                        result = tracker.process_frame(payload, chrome_ts)
                        if result == "stop":
                            done = True
                            break

            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\nStopped by user.")
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        import subprocess
        subprocess.run(["pkill", "-f", "chrome.*remote-debugging"], capture_output=True)
        subprocess.run(["pkill", "-f", "undetected_chromedriver"], capture_output=True)

    print(f"\nData saved to {tracker.out}")


if __name__ == "__main__":
    main()
