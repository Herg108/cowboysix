#!/bin/bash
echo "Stopping all recorders..."
pkill -INT -f live_price_recorder.py 2>/dev/null && echo "  Stopped Polymarket recorders (saving static chart...)" || echo "  No Polymarket recorders running"
sleep 2  # give it time to write static chart
pkill -INT -f hltv_live.py 2>/dev/null && echo "  Stopped HLTV tracker" || echo "  No HLTV tracker running"
pkill -f 'chrome.*remote-debugging' 2>/dev/null && echo "  Killed Chrome debug instances" || echo "  No Chrome debug instances"
pkill -f 'undetected_chromedriver' 2>/dev/null && echo "  Killed chromedriver" || echo "  No chromedriver running"
python3 build_index.py 2>/dev/null && echo "  Site index rebuilt" || echo "  Could not rebuild index"
echo "Done."
