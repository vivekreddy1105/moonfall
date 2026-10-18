#!/bin/bash
# Creates the 'moonfall' group, a shared socket directory, and test player accounts.
# Usage: sudo ./setup_users.sh [player names...]   (default: alice bob carol dave erin)
set -e
PLAYERS=${@:-alice bob carol dave erin}
groupadd -f moonfall
usermod -aG moonfall "${SUDO_USER:-$USER}"
for p in $PLAYERS; do
  id "$p" &>/dev/null || useradd -m -s /bin/bash "$p"
  usermod -aG moonfall "$p"
done
# setgid dir: socket inherits group 'moonfall'; sticky bit: users can't delete others' files
install -d -m 3770 -g moonfall /tmp/moonfall
GAME_DIR=$(cd "$(dirname "$0")" && pwd)
chmod -R o+rX "$GAME_DIR"   # players must be able to read the code
echo "Done. Log out/in (or run 'newgrp moonfall') so your group membership applies."
