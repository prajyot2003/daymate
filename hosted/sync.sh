#!/bin/sh
# Copy the shared modules into each hosted agent folder (Agentverse hosted projects are self-contained).
cd "$(dirname "$0")/.."
cp core.py tools.py messages.py answers.py hosted/daymate/
for a in weather places events; do cp tools.py messages.py answers.py "hosted/$a/"; done
echo "synced shared modules into hosted/*"
