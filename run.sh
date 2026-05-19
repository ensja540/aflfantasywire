#!/bin/bash
# Run the fetcher once, then every 15 minutes
# Usage: ./run.sh
# To run once: python fetch_data.py

cd "$(dirname "$0")"
echo "Starting AFLFantasyWire data fetcher..."
echo "Fetching every 15 minutes. Press Ctrl+C to stop."
echo ""

while true; do
    python3 fetch_data.py
    echo "Next fetch in 15 minutes... ($(date -v+15M '+%H:%M') or press Ctrl+C)"
    sleep 900
done
