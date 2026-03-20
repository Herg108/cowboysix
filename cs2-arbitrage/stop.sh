#!/bin/bash
echo "Stopping all recorders..."
pkill -f live_price_recorder.py 2>/dev/null && echo "  Killed Polymarket recorders" || echo "  No Polymarket recorders running"
pkill -f hltv_live.py 2>/dev/null && echo "  Killed HLTV tracker" || echo "  No HLTV tracker running"
pkill -f 'chrome.*remote-debugging' 2>/dev/null && echo "  Killed Chrome debug instances" || echo "  No Chrome debug instances"
pkill -f 'undetected_chromedriver' 2>/dev/null && echo "  Killed chromedriver" || echo "  No chromedriver running"
echo "Done."
