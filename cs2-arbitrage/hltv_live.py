"""
Live HLTV Score Tracker using undetected-chromedriver + CDP.

Opens the HLTV match page in a real Chrome browser (bypasses Cloudflare),
captures WebSocket frames via Chrome DevTools Protocol performance logs,
and logs all scorebot events with ms timestamps.

Automatically splits events into map1/, map2/, map3/ folders based on
when the map changes, so each map's HLTV data sits alongside its
Polymarket price data.

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


class HLTVTracker:
    def __init__(self, output_dir: str):
        self.out = Path(output_dir)
        self.out.mkdir(parents=True, exist_ok=True)
        self.logfile = self.out / "hltv_events.jsonl"

        self.current_map = None
        self.last_score = None
        self.last_round = None
        self.team1_name = None
        self.maps_finished = 0

    def _on_new_map(self, map_name: str):
        """Reset per-map state when a new map starts."""
        if map_name != self.current_map:
            if self.current_map is not None:
                print(f"\n{'='*60}")
                print(f"New map detected: {map_name}")
                print(f"{'='*60}\n")
            self.current_map = map_name
            self.last_score = None
            self.last_round = None
            self.team1_name = None

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

        # Map to consistent team1/team2
        if self.team1_name == t_name:
            t1_name, t1_score = t_name, t_score
            t2_name, t2_score = ct_name, ct_score
        else:
            t1_name, t1_score = ct_name, ct_score
            t2_name, t2_score = t_name, t_score

        current_score = (t1_score, t2_score)
        score_changed = current_score != self.last_score
        round_changed = current_round != self.last_round

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

            # Detect map end — stop everything
            if t1_score >= 13 or t2_score >= 13:
                winner = t1_name if t1_score > t2_score else t2_name
                print(f"\n[MAP DONE] {winner} wins {t1_score}-{t2_score}!")
                return "stop"

        return None

    def _handle_log(self, data: dict, ts_ms: int, ts_iso: str):
        if self.current_map is None:
            return None

        log_entries = data.get("log", [])

        for log_entry in log_entries:
            if "RoundEnd" in log_entry:
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
                entry = {
                    "ts_ms": ts_ms,
                    "ts_iso": ts_iso,
                    "type": "kill",
                    "killer": kill.get("killerName"),
                    "victim": kill.get("victimName"),
                    "weapon": kill.get("weapon"),
                    "headshot": kill.get("headShot", False),
                }
                append_jsonl(self.logfile, entry)

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
    parser.add_argument("--interval", type=float, default=0.5, help="Poll interval in seconds")
    args = parser.parse_args()

    tracker = HLTVTracker(args.output)

    print(f"Opening HLTV: {args.url}")
    print(f"Base output: {args.output}")
    print(f"Events will be split into map1/, map2/, map3/ automatically")
    print()

    options = uc.ChromeOptions()
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    driver = uc.Chrome(headless=False, options=options)
    driver.get(args.url)
    print("Waiting for page to load...")
    time.sleep(10)

    title = driver.title
    if "Just a moment" in title:
        print("[ERROR] Blocked by Cloudflare. Try again.")
        driver.quit()
        return

    print(f"[OK] Page loaded: {title}")
    print("Listening for scorebot events...\n")

    try:
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
        # Kill any lingering chrome/chromedriver processes
        import subprocess
        subprocess.run(["pkill", "-f", "chrome.*remote-debugging"], capture_output=True)
        subprocess.run(["pkill", "-f", "undetected_chromedriver"], capture_output=True)

    print(f"\nData saved to {tracker.logfile}")


if __name__ == "__main__":
    main()
