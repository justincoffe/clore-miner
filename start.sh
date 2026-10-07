#!/bin/sh
# clore-miner entrypoint: sshd (key from SSH_PUBKEY, optional ROOT_PASSWORD) + the agent, which runs the miner.
# Safe to run twice (image CMD and Clore's order `command`): each part starts only if it isn't running.
mkdir -p /root/.ssh /var/log/miner /run/sshd
chmod 700 /root/.ssh
if [ -n "$SSH_PUBKEY" ]; then
  grep -qF "$SSH_PUBKEY" /root/.ssh/authorized_keys 2>/dev/null || echo "$SSH_PUBKEY" >> /root/.ssh/authorized_keys
  chmod 600 /root/.ssh/authorized_keys
fi
if [ -n "$ROOT_PASSWORD" ]; then
  echo "root:$ROOT_PASSWORD" | chpasswd
  sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication yes/; s/^#\?PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config
fi
pgrep -x sshd >/dev/null 2>&1 || /usr/sbin/sshd
if pgrep -f "/opt/clore-miner/agent.py" >/dev/null 2>&1; then
  exit 0
fi
exec python3 /opt/clore-miner/agent.py
