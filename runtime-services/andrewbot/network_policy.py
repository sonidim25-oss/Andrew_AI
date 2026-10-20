"""Conservative public destination exclusions shared with firewall generation."""
import ipaddress


BLOCKED_IPV4 = (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
    "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24", "192.88.99.0/24", "192.168.0.0/16",
    "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4",
)
BLOCKED_IPV6 = ("2001::/23", "2001:db8::/32", "2002::/16", "3fff::/20")
GLOBAL_IPV6 = ipaddress.ip_network("2000::/3")


def excluded_networks(additional=()):
    return tuple(ipaddress.ip_network(value, strict=False) for value in (*BLOCKED_IPV4, *BLOCKED_IPV6, *additional))


def is_public(address, exclusions):
    return (address.is_global and not address.is_multicast and not address.is_unspecified
            and (address.version == 4 or address in GLOBAL_IPV6)
            and not any(address.version == network.version and address in network for network in exclusions))
