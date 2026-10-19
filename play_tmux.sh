#!/bin/bash
# Opens a tmux session: server pane + one pane per player, each as a different Linux user.
# Usage: ./play_tmux.sh [player names...]   (default: alice bob carol dave erin)
# Use the SAME names you gave to setup_users.sh.
DIR=$(cd "$(dirname "$0")" && pwd)
PLAYERS=${@:-alice bob carol dave erin}

sudo -v || exit 1
# Pre-flight checks (classmate feedback: unclear errors with custom names / home dirs)
for p in $PLAYERS; do
  if ! id "$p" &>/dev/null; then
    echo "Error: Linux user '$p' does not exist."
    echo "Create the players first:  sudo ./setup_users.sh $PLAYERS"
    exit 1
  fi
  if ! sudo -u "$p" test -r "$DIR/moonfall_client.py"; then
    echo "Error: user '$p' cannot read $DIR."
    echo "Home directories are private on Ubuntu. Run the game from /opt instead:"
    echo "  sudo cp -r \"$DIR\" /opt/moonfall && sudo chmod -R a+rX /opt/moonfall && cd /opt/moonfall"
    exit 1
  fi
done

tmux new-session -d -s moonfall "python3 $DIR/moonfall_server.py --night 45 --day 90; read"
for p in $PLAYERS; do
  tmux split-window -t moonfall "sudo -u $p python3 $DIR/moonfall_client.py; read"
  tmux select-layout -t moonfall tiled
done
tmux attach -t moonfall
