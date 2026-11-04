"""Accept the sanitized Nginx identity only over the local application socket."""
from ipaddress import ip_address


def client_ip(peer, headers):
    address = str(peer[0]) if peer else "unknown"
    try:
        parsed = ip_address(address)
    except ValueError:
        return address
    normalized = getattr(parsed, "ipv4_mapped", None) or parsed
    if not normalized.is_loopback:
        return str(normalized)
    values = headers.get_all("X-Real-IP", [])
    if len(values) != 1:
        return str(normalized)
    try:
        forwarded = ip_address(values[0].strip())
        if "%" in str(forwarded):
            return str(normalized)
        return str(getattr(forwarded, "ipv4_mapped", None) or forwarded)
    except ValueError:
        return str(normalized)
