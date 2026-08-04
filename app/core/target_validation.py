"""Validation for outbound webhook destinations."""

import ipaddress
import socket
from collections.abc import Callable, Sequence
from urllib.parse import urlsplit


BLOCKED_EXPLICIT_IPS = {
    ipaddress.ip_address("169.254.169.254"),  # Common cloud metadata endpoint
    ipaddress.ip_address("100.100.100.200"),  # Alibaba Cloud metadata endpoint
    ipaddress.ip_address("fd00:ec2::254"),  # AWS IPv6 metadata endpoint
}
DEFAULT_MAX_TARGET_URL_LENGTH = 2_048
ALLOWED_SCHEMES = {"http", "https"}


def normalize_hostname(hostname: str) -> str:
    """Return a canonical ASCII hostname or IP literal."""
    if not isinstance(hostname, str):
        raise ValueError("Target host must be a string")

    candidate = hostname.strip().rstrip(".")
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    if not candidate or any(char.isspace() for char in candidate):
        raise ValueError("Target host must not be empty or contain whitespace")

    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        pass

    try:
        ascii_hostname = candidate.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("Target host is not a valid hostname") from exc

    if len(ascii_hostname) > 253:
        raise ValueError("Target hostname is too long")

    labels = ascii_hostname.split(".")
    if any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or not all(char.isalnum() or char == "-" for char in label)
        for label in labels
    ):
        raise ValueError("Target host is not a valid hostname")

    return ascii_hostname


def resolve_hostname(hostname: str, port: int) -> list[str]:
    """Resolve every address for a hostname, failing closed on DNS errors."""
    try:
        address_info = socket.getaddrinfo(
            hostname,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise ValueError(f"Could not resolve target hostname '{hostname}'") from exc
    except OSError as exc:
        raise ValueError(f"DNS resolution failed for target hostname '{hostname}'") from exc

    resolved = sorted({item[4][0] for item in address_info if item[4]})
    if not resolved:
        raise ValueError(f"No IP addresses resolved for target hostname '{hostname}'")
    return resolved


def blocked_ip_reason(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Describe why an address is unsafe, or return ``None`` when it is public."""
    if address in BLOCKED_EXPLICIT_IPS:
        return "cloud metadata address"
    if address.is_loopback:
        return "loopback address"
    if address.is_link_local:
        return "link-local address"
    if address.is_unspecified:
        return "unspecified address"
    if address.is_private:
        return "private-network address"
    if address.is_multicast:
        return "multicast address"
    if address.is_reserved or not address.is_global:
        return "restricted address"
    return None


def _validate_ip(ip_value: str) -> None:
    try:
        address = ipaddress.ip_address(ip_value.split("%", 1)[0])
    except ValueError as exc:
        raise ValueError(f"DNS returned an invalid IP address: '{ip_value}'") from exc

    reason = blocked_ip_reason(address)
    if reason:
        raise ValueError(f"Target URL points to a forbidden {reason}: {address}")


def validate_delivery_target(
    url: str,
    *,
    environment: str = "development",
    allowed_target_hosts: Sequence[str] | None = None,
    resolve_dns: bool = True,
    resolver: Callable[[str, int], list[str]] | None = None,
    max_url_length: int = DEFAULT_MAX_TARGET_URL_LENGTH,
) -> str:
    """Validate an outbound URL's syntax, policy, DNS results, and IP scope."""
    if not isinstance(url, str) or not url:
        raise ValueError("Target URL must be a non-empty string")
    if len(url) > max_url_length:
        raise ValueError(f"Target URL exceeds maximum length of {max_url_length} characters")
    if "\\" in url or any(char.isspace() or ord(char) < 32 for char in url):
        raise ValueError("Target URL contains forbidden whitespace or control characters")

    normalized_environment = str(environment).strip().lower()
    if normalized_environment not in {"development", "test", "production"}:
        raise ValueError("Unknown environment; refusing target validation")

    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Target URL is malformed") from exc

    scheme = parsed.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise ValueError(f"Unsupported URL scheme '{scheme}'. Only HTTP and HTTPS are allowed")
    if normalized_environment == "production" and scheme != "https":
        raise ValueError("HTTPS is required in production")
    if not parsed.netloc or hostname is None:
        raise ValueError("Target URL is missing a valid hostname")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise ValueError("Target URL must not contain credentials")
    if parsed.fragment:
        raise ValueError("Target URL must not contain a fragment")

    normalized_host = normalize_hostname(hostname)
    normalized_allowlist = {
        normalize_hostname(allowed_host) for allowed_host in (allowed_target_hosts or [])
    }
    if normalized_environment == "production" and not normalized_allowlist:
        raise ValueError("Production requires a non-empty target host allowlist")
    if normalized_allowlist and normalized_host not in normalized_allowlist:
        raise ValueError(f"Target host '{normalized_host}' is not allowlisted")

    try:
        literal_address = ipaddress.ip_address(normalized_host)
    except ValueError:
        literal_address = None

    if literal_address is not None:
        _validate_ip(str(literal_address))
    elif resolve_dns:
        resolved_ips = (resolver or resolve_hostname)(
            normalized_host,
            port or (443 if scheme == "https" else 80),
        )
        for resolved_ip in resolved_ips:
            _validate_ip(resolved_ip)

    return url
