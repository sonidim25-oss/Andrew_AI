# Andrew egress firewall deployment draft

This repository contains a proposal only. Do not apply it until the target host has been privately inventoried and the owner reviews the exact diff and rollback plan. Never commit host firewall snapshots or network inventories.

Before drafting a host-specific policy, use `inspect-current.sh` to capture a private, root-only snapshot. Review current nftables, UFW, Docker, VPN, and routing state. The inspector is read-only. Do not use `nft flush ruleset`, restore a whole-host snapshot, or edit existing tables.

A future policy must use a separate Andrew-only table, scoped to the numeric UID of the dedicated egress service. It should allow only DNS to the configured local resolver and TCP 80/443 to validated public global-unicast destinations; deny private, loopback, link-local, metadata, multicast, reserved, and other non-public ranges before any allow rule. Drop other egress for that UID. The worker and controller get no direct network exception.

The application proxy checks are not a replacement for an independently enforced firewall boundary. If the required policy cannot be proven without modifying existing host rules, stop and move the worker to a separate VM or host. `rollback-andrew-policy.sh` removes only the Andrew-specific table.

The systemd proxy and runner units in this repository are source files only. They are not installed or enabled by these scripts.
