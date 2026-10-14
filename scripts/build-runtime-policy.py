#!/usr/bin/env python3
"""Generate Andrew's dedicated UID firewall table and proxy exclusions."""
import argparse
import ipaddress
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'prototype'))
from andrewbot.network_policy import BLOCKED_IPV4,BLOCKED_IPV6

def generate(runtime_uid,browser_uid,egress_uid,addresses,dns):
    if any(type(uid) is not int or uid<=0 for uid in (runtime_uid,browser_uid,egress_uid)):
        raise ValueError('dedicated non-root UIDs required')
    if len({runtime_uid,browser_uid,egress_uid}) != 3 or not dns:
        raise ValueError('distinct UIDs and explicit DNS required')
    exclusions=sorted({str(ipaddress.ip_network(f"{a['local']}/{a['prefixlen']}",strict=False))
                       for interface in addresses for a in interface.get('addr_info',[])})
    v4=[str(n) for n in map(ipaddress.ip_network,list(BLOCKED_IPV4)+exclusions) if n.version==4]
    v6=[str(n) for n in map(ipaddress.ip_network,list(BLOCKED_IPV6)+exclusions) if n.version==6]
    # IPv6 is allowed only from global unicast 2000::/3; transition and reserved
    # ranges remain explicitly blocked, matching application validation.
    lines=['delete table inet andrew_runtime','table inet andrew_runtime {',
           ' set blocked4 { type ipv4_addr; flags interval; auto-merge; elements = { '+', '.join(v4)+' } }',
           ' set blocked6 { type ipv6_addr; flags interval; auto-merge; elements = { '+', '.join(v6)+' } }',
           ' chain output { type filter hook output priority -10; policy accept;',
           f'  meta skuid {{ {runtime_uid}, {browser_uid} }} counter reject']
    for address in dns:
        ip=ipaddress.ip_address(address); family='ip6' if ip.version==6 else 'ip'
        lines += [f'  meta skuid {egress_uid} {family} daddr {ip} udp dport 53 accept',
                  f'  meta skuid {egress_uid} {family} daddr {ip} tcp dport 53 accept']
    lines += [f'  meta skuid {egress_uid} ip daddr @blocked4 counter reject',
              f'  meta skuid {egress_uid} ip6 daddr != 2000::/3 counter reject',
              f'  meta skuid {egress_uid} ip6 daddr @blocked6 counter reject',
              f'  meta skuid {egress_uid} tcp dport {{ 80, 443 }} accept',
              f'  meta skuid {egress_uid} counter reject',' }','}']
    return '\n'.join(lines)+'\n',{'blocked_networks':exclusions}

if __name__=='__main__':
    p=argparse.ArgumentParser(); p.add_argument('--runtime-uid',type=int,required=True); p.add_argument('--browser-uid',type=int,required=True)
    p.add_argument('--egress-uid',type=int,required=True); p.add_argument('--addresses',required=True); p.add_argument('--dns',required=True)
    p.add_argument('--firewall',required=True); p.add_argument('--proxy',required=True); a=p.parse_args()
    dns=[line.split()[1] for line in Path(a.dns).read_text().splitlines() if line.startswith('nameserver ')]
    if not dns: raise SystemExit('DNS resolver unavailable')
    nft,proxy=generate(a.runtime_uid,a.browser_uid,a.egress_uid,json.loads(Path(a.addresses).read_text()),dns)
    Path(a.firewall).write_text(nft); Path(a.proxy).write_text(json.dumps(proxy)+'\n')
