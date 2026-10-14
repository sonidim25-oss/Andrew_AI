#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run with sudo: sudo bash $0" >&2
  exit 1
fi

umask 077
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="/root/andrew-firewall-preflight-$STAMP"
install -d -o root -g root -m 0700 "$OUT"

nft -a list ruleset > "$OUT/nft-ruleset.txt" 2> "$OUT/nft-ruleset.err" || true
ufw status verbose > "$OUT/ufw-status.txt" 2>&1 || true
iptables-save > "$OUT/iptables-v4.txt" 2>&1 || true
ip6tables-save > "$OUT/iptables-v6.txt" 2>&1 || true
iptables -t nat -S > "$OUT/iptables-nat-v4.txt" 2>&1 || true
ip6tables -t nat -S > "$OUT/iptables-nat-v6.txt" 2>&1 || true
for path in /etc/nftables.conf /etc/ufw/ufw.conf /etc/ufw/before.rules /etc/ufw/after.rules /etc/ufw/user.rules /etc/ufw/before6.rules /etc/ufw/after6.rules /etc/ufw/user6.rules; do
  if [[ -r "$path" ]]; then
    cp --preserve=mode,ownership "$path" "$OUT/$(basename "$path")"
  fi
done
for unit in nftables ufw docker tailscaled ssh; do
  systemctl status "$unit" --no-pager > "$OUT/systemd-$unit.txt" 2>&1 || true
done
ip -details address show > "$OUT/ip-addresses.txt"
ip route show table all > "$OUT/ip-routes-v4.txt"
ip -6 route show table all > "$OUT/ip-routes-v6.txt"
if command -v docker >/dev/null; then
  docker network ls --no-trunc > "$OUT/docker-networks.txt" 2>&1 || true
fi
sha256sum "$OUT"/* > "$OUT/SHA256SUMS"
chmod -R go-rwx "$OUT"
echo "Firewall preflight captured in $OUT"
echo "No firewall policy was changed. Review nft-ruleset.txt and ufw-status.txt before drafting any Andrew rule."
