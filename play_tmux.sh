#!/bin/bash
# Opens a tmux session: server pane + one pane per player, each as a different Linux user.
DIR=$(cd "$(dirname "$0")" && pwd)
PLAYERS=${@:-alice bob carol dave erin}
tmux new-session -d -s moonfall "python3 $DIR/moonfall_server.py --night 200  --day 200; read"
for p in $PLAYERS; do
  tmux split-window -t moonfall "sudo -u $p python3 $DIR/moonfall_client.py; read"
  tmux select-layout -t moonfall tiled
done
tmux attach -t moonfall
