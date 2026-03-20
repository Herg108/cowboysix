from __future__ import annotations

"""
HLTV Live Scorebot Tracker

Connects to HLTV's Socket.IO scorebot and emits parsed match events.
Protocol: Socket.IO v3 over WebSocket (EIO=3).

Socket.IO EIO=3 wire format over raw WebSocket:
  - "0"  = connect (open)
  - "2"  = ping  →  respond with "3" (pong)
  - "40" = socket.io connect (namespace /)
  - "42" = event message, payload is JSON array: ["eventName", data]
  - "3"  = pong

We use raw websockets instead of python-socketio because HLTV's scorebot
has quirks that are easier to handle at the wire level.
"""

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

import websockets
import websockets.exceptions

import config
from event_log import append_event


@dataclass
class MatchState:
    match_id: str = ""
    team1_name: str = ""
    team2_name: str = ""
    team1_score: int = 0
    team2_score: int = 0
    team1_side: str = ""  # "CT" or "T"
    team2_side: str = ""
    current_round: int = 0
    map_name: str = ""
    map_number: int = 1
    round_phase: str = ""  # "live", "freezetime", "over"
    bomb_planted: bool = False
    team1_alive: int = 5
    team2_alive: int = 5
    last_event: str = ""
    last_event_time_ms: int = 0
    round_history: list = field(default_factory=list)


EventCallback = Callable[[str, dict, MatchState], Coroutine[Any, Any, None]]


class HLTVTracker:
    def __init__(self, match_id: str, on_event: EventCallback | None = None):
        self.match_id = match_id
        self.state = MatchState(match_id=match_id)
        self.on_event = on_event
        self._ws = None
        self._running = False
        self._ping_task = None

    async def connect(self):
        self._running = True
        while self._running:
            try:
                await self._connect_and_listen()
            except (
                websockets.exceptions.ConnectionClosed,
                websockets.exceptions.InvalidStatusCode,
                ConnectionError,
                OSError,
            ) as e:
                if self._running:
                    print(f"[HLTV] Connection lost: {e}. Reconnecting in {config.HLTV_RECONNECT_DELAY_S}s...")
                    await asyncio.sleep(config.HLTV_RECONNECT_DELAY_S)

    async def _connect_and_listen(self):
        async with websockets.connect(
            config.HLTV_SCOREBOT_URL,
            additional_headers={"Origin": "https://www.hltv.org"},
            ping_interval=None,  # we handle pings manually per EIO protocol
        ) as ws:
            self._ws = ws
            print(f"[HLTV] Connected to scorebot for match {self.match_id}")

            async for raw_msg in ws:
                if not self._running:
                    break
                await self._handle_raw_message(str(raw_msg))

    async def _handle_raw_message(self, msg: str):
        # EIO=3 protocol handling
        if msg == "2":
            # Ping → send pong
            await self._ws.send("3")
            return

        if msg.startswith("0"):
            # Engine.IO open packet — contains session info as JSON
            # After open, send Socket.IO connect for default namespace
            await self._ws.send("40")
            return

        if msg == "40":
            # Socket.IO connected to namespace /
            # Now subscribe to the match
            # The payload must be a JSON string (not object) — double-encoded
            inner = json.dumps({"token": "", "listId": str(self.match_id)})
            await self._ws.send(f'42["readyForMatch",{json.dumps(inner)}]')
            print(f"[HLTV] Subscribed to match {self.match_id}")
            return

        if msg.startswith("42"):
            # Socket.IO event
            try:
                payload = json.loads(msg[2:])
                if isinstance(payload, list) and len(payload) >= 2:
                    event_name = payload[0]
                    event_data = payload[1] if len(payload) > 1 else {}
                    await self._handle_event(event_name, event_data)
            except json.JSONDecodeError:
                pass
            return

    async def _handle_event(self, event_name: str, data: Any):
        ts_ms = int(time.time() * 1000)
        self.state.last_event = event_name
        self.state.last_event_time_ms = ts_ms

        if isinstance(data, str):
            try:
                data = json.loads(data)
            except (json.JSONDecodeError, TypeError):
                pass

        if event_name == "log":
            # HLTV wraps game events inside a "log" event.
            # On first connect: data is a JSON string of an array (replay of all past events).
            # During match: data is a JSON string of a single event object.
            await self._handle_log(data, ts_ms)
            return
        elif event_name == "scoreboard":
            self._parse_scoreboard(data)
        elif event_name == "roundEnd":
            self._parse_round_end(data)
        elif event_name == "roundStart":
            self.state.round_phase = "live"
            self.state.bomb_planted = False
            self.state.team1_alive = 5
            self.state.team2_alive = 5
        elif event_name == "kill":
            self._parse_kill(data)
        elif event_name == "bombPlanted":
            self.state.bomb_planted = True
        elif event_name == "bombDefused":
            self.state.bomb_planted = False
        elif event_name == "mapChange":
            if isinstance(data, dict):
                self.state.map_name = data.get("map", self.state.map_name)
                self.state.map_number = data.get("mapNumber", self.state.map_number)
        elif event_name == "matchStarted":
            if isinstance(data, dict):
                self.state.map_name = data.get("map", "")

        # Log event
        log_entry = {
            "ts_ms": ts_ms,
            "event": event_name,
            "match_id": self.match_id,
            "score": f"{self.state.team1_score}-{self.state.team2_score}",
            "round": self.state.current_round,
            "data": data if isinstance(data, (dict, list, str, int, float, bool)) else str(data),
        }
        append_event(config.MATCH_EVENTS_LOG, log_entry)

        if self.on_event:
            await self.on_event(event_name, log_entry, self.state)

    async def _handle_log(self, data: Any, ts_ms: int):
        """Handle the 'log' event which wraps game events.

        On first connect, data is a JSON string containing an array of all
        past events (replay). During live play, data is a JSON string of
        a single event object with an 'event' field like 'kill', 'roundEnd', etc.
        """
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except (json.JSONDecodeError, TypeError):
                return

        if isinstance(data, list):
            # Replay of past events on connect — process each one
            # but only log, don't fire callbacks (these are historical)
            print(f"[HLTV] Received replay of {len(data)} past events")
            for entry in data:
                if isinstance(entry, dict):
                    sub_event = entry.get("event", "")
                    if sub_event:
                        await self._handle_event(sub_event, entry)
        elif isinstance(data, dict):
            # Single live event
            sub_event = data.get("event", "")
            if sub_event:
                await self._handle_event(sub_event, data)

    def _parse_scoreboard(self, data: Any):
        if not isinstance(data, dict):
            return
        teams = data.get("teams", [])
        if len(teams) >= 2:
            t1, t2 = teams[0], teams[1]
            self.state.team1_name = t1.get("name", self.state.team1_name)
            self.state.team2_name = t2.get("name", self.state.team2_name)
            self.state.team1_score = int(t1.get("score", self.state.team1_score))
            self.state.team2_score = int(t2.get("score", self.state.team2_score))
            self.state.team1_side = t1.get("side", self.state.team1_side)
            self.state.team2_side = t2.get("side", self.state.team2_side)

        self.state.current_round = int(data.get("currentRound", self.state.current_round))
        self.state.map_name = data.get("mapName", self.state.map_name)
        self.state.bomb_planted = data.get("bombPlanted", False)

    def _parse_round_end(self, data: Any):
        if not isinstance(data, dict):
            return
        self.state.round_phase = "over"

        # Update scores — HLTV log format uses counterTerroristScore/terroristScore
        ct_score = data.get("counterTerroristScore")
        t_score = data.get("terroristScore")
        if ct_score is not None and t_score is not None:
            # Map CT/T scores to team1/team2 based on current sides
            if self.state.team1_side == "CT":
                self.state.team1_score = int(ct_score)
                self.state.team2_score = int(t_score)
            elif self.state.team1_side == "TERRORIST":
                self.state.team1_score = int(t_score)
                self.state.team2_score = int(ct_score)
            else:
                # Sides not known yet — use CT as team1 for now
                self.state.team1_score = int(ct_score)
                self.state.team2_score = int(t_score)

        # Also handle the teams array format (from scoreboard-style events)
        teams = data.get("teams", [])
        if len(teams) >= 2:
            self.state.team1_score = int(teams[0].get("score", self.state.team1_score))
            self.state.team2_score = int(teams[1].get("score", self.state.team2_score))

        winner = data.get("winner", "")
        win_type = data.get("winType", "")
        self.state.round_history.append({
            "round": self.state.current_round,
            "winner": winner,
            "win_type": win_type,
            "score": f"{self.state.team1_score}-{self.state.team2_score}",
            "ts_ms": self.state.last_event_time_ms,
        })

    def _parse_kill(self, data: Any):
        if not isinstance(data, dict):
            return
        # Track alive counts based on kill events
        # Log format uses "CT" or "TERRORIST" for victimSide
        victim_side = data.get("victimSide", "")
        if victim_side:
            if victim_side == self.state.team1_side:
                self.state.team1_alive = max(0, self.state.team1_alive - 1)
            elif victim_side == self.state.team2_side:
                self.state.team2_alive = max(0, self.state.team2_alive - 1)
            # Also handle if sides aren't mapped yet
            elif victim_side in ("CT", "TERRORIST"):
                pass  # Can't map to team without side info

    def stop(self):
        self._running = False
