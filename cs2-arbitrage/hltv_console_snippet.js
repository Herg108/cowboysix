// Paste this into the HLTV Console on a live match page.
// After match: run copy(JSON.stringify(window._hltvLog)) and save to file.

(function() {
  window._hltvLog = [];
  let lastT1 = -1, lastT2 = -1, t1Name = '', lastRound = 0;

  function handle(msg) {
    if (typeof msg !== 'string' || !msg.startsWith('42')) return;
    try {
      const payload = JSON.parse(msg.substring(2));
      if (!Array.isArray(payload) || payload.length < 2) return;
      let data = payload[1];
      if (typeof data === 'string') data = JSON.parse(data);
      if (payload[0] !== 'scoreboard') return;

      const ts = Date.now();
      const r = data.currentRound || 0;
      const tN = data.terroristTeamName || '?';
      const ctN = data.ctTeamName || '?';
      const tS = data.tTeamScore || 0;
      const ctS = data.ctTeamScore || 0;

      if (!t1Name) t1Name = tN;
      const s1 = (t1Name === tN) ? tS : ctS;
      const s2 = (t1Name === tN) ? ctS : tS;
      const n1 = (t1Name === tN) ? tN : ctN;
      const n2 = (t1Name === tN) ? ctN : tN;

      if (s1 !== lastT1 || s2 !== lastT2 || r !== lastRound) {
        const e = {ts_ms: ts, ts_iso: new Date(ts).toISOString(), round: r, team1: n1, team1_score: s1, team2: n2, team2_score: s2, map: data.mapName || '?'};
        window._hltvLog.push(e);
        lastT1 = s1; lastT2 = s2; lastRound = r;
        console.log(`[SCORE] R${r}: ${n1} ${s1} - ${s2} ${n2} @ ${e.ts_iso}`);
      }
    } catch(e) {}
  }

  // Override the native WebSocket message dispatch
  const origDispatch = EventTarget.prototype.dispatchEvent;
  EventTarget.prototype.dispatchEvent = function(event) {
    if (this instanceof WebSocket && event.type === 'message') {
      handle(event.data);
    }
    return origDispatch.call(this, event);
  };

  console.log('[HLTV-LOG] Score logger active. After match: copy(JSON.stringify(window._hltvLog))');
})();
