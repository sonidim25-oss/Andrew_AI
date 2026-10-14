#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run with sudo: sudo bash $0" >&2
  exit 1
fi

if nft list table inet andrew_agent >/dev/null 2>&1; then
  nft delete table inet andrew_agent
  echo "Removed only inet andrew_agent. Other nftables tables and all configuration files were left untouched."
else
  echo "inet andrew_agent is absent; nothing to roll back."
fi
