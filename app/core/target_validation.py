"""Validation for outbound webhook destinations."""

import asyncio
import concurrent.futures
import inspect
import ipaddress
import socket
import threading
from collections.abc import Callable, Sequence
from typing import Any
from urllib.parse import urlsplit


BLOCKED_EXPLICIT_IPS = {
    ipaddress.ip_address("169.254.169.254"),  # Common cloud metadata endpoint
    ipaddress.ip_address("100.100.100.200"),  # Alibaba Cloud metadata endpoint
    ipaddress.ip_address("fd00:ec2::254"),  # AWS IPv6 metadata endpoint
}
DEFAULT_MAX_TARGET_URL_LENGTH = 2_048
DEFAULT_DNS_LOOKUP_TIMEOUT_SECONDS = 2.0
ALLOWED_SCHEMES = {"http", "https"}


class TargetValidationError(ValueError):
    """Base error for target URL validation failures."""


class InvalidTargetError(TargetValidationError):
    """Target URL violates syntax, scheme, credentials, allowlist, or IP policy."""


class TransientDNSError(TargetValidationError):
    """Transient failure or timeout during DNS resolution."""


_dns_executor: concurrent.futures.ThreadPoolExecutor | None = None
_dns_executor_lock = threading.Lock()


def _get_dns_executor() -> concurrent.futures.ThreadPoolExecutor:
    """Return a dedicated bounded ThreadPoolExecutor for DNS lookups.

    Using a dedicated pool isolates DNS resolution from the default event-loop
    executor, preventing stalled lookups from exhausting worker delivery threads.
    """
    global _dns_executor
    with _dns_executor_lock:
        if _dns_executor is None:
            _dns_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=8,
                thread_name_prefix="dns_resolver",
            )
        return _dns_executor


def normalize_hostname(hostname: str) -> str:
    """Return a canonical ASCII hostname or IP literal."""
    if not isinstance(hostname, str):
        raise InvalidTargetError("Target host must be a string")

    candidate = hostname.strip().rstrip(".")
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    if not candidate or any(char.isspace() for char in candidate):
        raise InvalidTargetError("Target host must not be empty or contain whitespace")

    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        pass

    try:
        ascii_hostname = candidate.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise InvalidTargetError("Target host is not a valid hostname") from exc

    if len(ascii_hostname) > 253:
        raise InvalidTargetError("Target hostname is too long")

    labels = ascii_hostname.split(".")
    if any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or not all(char.isalnum() or char == "-" for char in label)
        for label in labels
    ):
        raise InvalidTargetError("Target host is not a valid hostname")

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
        raise TransientDNSError(f"Could not resolve target hostname '{hostname}': {exc}") from exc
    except OSError as exc:
        raise TransientDNSError(f"DNS resolution failed for target hostname '{hostname}': {exc}") from exc

    resolved = sorted({item[4][0] for item in address_info if item[4]})
    if not resolved:
        raise InvalidTargetError(f"No IP addresses resolved for target hostname '{hostname}'")
    return resolved


async def resolve_hostname_async(
    hostname: str,
    port: int,
    *,
    dns_timeout: float = DEFAULT_DNS_LOOKUP_TIMEOUT_SECONDS,
    resolver: Callable[..., Any] | None = None,
    cancel_event: threading.Event | None = None,
) -> list[str]:
    """Resolve hostname asynchronously with a strict resource-bounded timeout.

    Enforces the DNS deadline on the caller so worker execution capacity is
    freed immediately without blocking worker delivery slots. If the resolver
    is an async coroutine, it is directly cancelled when the deadline expires.
    If the resolver is synchronous, it runs on a dedicated bounded
    ThreadPoolExecutor to isolate worker processing threads, and signals
    cooperative cancellation via `cancel_event` where supported.
    """
    target_resolver = resolver or resolve_hostname

    if asyncio.iscoroutinefunction(target_resolver):
        try:
            async with asyncio.timeout(dns_timeout):
                result = await target_resolver(hostname, port)
        except TimeoutError as exc:
            raise TransientDNSError(
                f"DNS resolution timed out for target hostname '{hostname}' after {dns_timeout}s"
            ) from exc
        except (TargetValidationError, ValueError):
            raise
        except (socket.gaierror, OSError) as exc:
            raise TransientDNSError(
                f"DNS resolution failed for target hostname '{hostname}': {exc}"
            ) from exc
        except Exception as exc:
            raise TransientDNSError(
                f"DNS resolution failed for target hostname '{hostname}': {exc}"
            ) from exc
    else:
        sig = inspect.signature(target_resolver)
        accepts_cancel = "cancel_event" in sig.parameters
        accepts_timeout = "timeout" in sig.parameters or "dns_timeout" in sig.parameters

        if cancel_event is None:
            cancel_event = threading.Event()

        def _invoke_sync() -> list[str]:
            kwargs: dict[str, Any] = {}
            if accepts_cancel:
                kwargs["cancel_event"] = cancel_event
            if accepts_timeout:
                kwargs["timeout"] = dns_timeout
            return target_resolver(hostname, port, **kwargs)

        executor = _get_dns_executor()
        future = executor.submit(_invoke_sync)
        try:
            async with asyncio.timeout(dns_timeout):
                result = await asyncio.wrap_future(future)
        except TimeoutError as exc:
            cancel_event.set()
            future.cancel()
            raise TransientDNSError(
                f"DNS resolution timed out for target hostname '{hostname}' after {dns_timeout}s"
            ) from exc
        except (TargetValidationError, ValueError):
            raise
        except (socket.gaierror, OSError) as exc:
            raise TransientDNSError(
                f"DNS resolution failed for target hostname '{hostname}': {exc}"
            ) from exc
        except Exception as exc:
            raise TransientDNSError(
                f"DNS resolution failed for target hostname '{hostname}': {exc}"
            ) from exc

    if not isinstance(result, (list, tuple, set)) or not result:
        raise InvalidTargetError(f"No IP addresses resolved for target hostname '{hostname}'")
    return list(result)


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
        raise InvalidTargetError(f"DNS returned an invalid IP address: '{ip_value}'") from exc

    reason = blocked_ip_reason(address)
    if reason:
        raise InvalidTargetError(f"Target URL points to a forbidden {reason}: {address}")


def _parse_and_validate_target_url(
    url: str,
    environment: str = "development",
    allowed_target_hosts: Sequence[str] | None = None,
    max_url_length: int = DEFAULT_MAX_TARGET_URL_LENGTH,
) -> tuple[str, int, ipaddress.IPv4Address | ipaddress.IPv6Address | None]:
    if not isinstance(url, str) or not url:
        raise InvalidTargetError("Target URL must be a non-empty string")
    if len(url) > max_url_length:
        raise InvalidTargetError(f"Target URL exceeds maximum length of {max_url_length} characters")
    if "\\" in url or any(char.isspace() or ord(char) < 32 for char in url):
        raise InvalidTargetError("Target URL contains forbidden whitespace or control characters")

    normalized_environment = str(environment).strip().lower()
    if normalized_environment not in {"development", "test", "production"}:
        raise InvalidTargetError("Unknown environment; refusing target validation")

    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise InvalidTargetError("Target URL is malformed") from exc

    scheme = parsed.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise InvalidTargetError(f"Unsupported URL scheme '{scheme}'. Only HTTP and HTTPS are allowed")
    if normalized_environment == "production" and scheme != "https":
        raise InvalidTargetError("HTTPS is required in production")
    if not parsed.netloc or hostname is None:
        raise InvalidTargetError("Target URL is missing a valid hostname")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise InvalidTargetError("Target URL must not contain credentials")
    if parsed.fragment:
        raise InvalidTargetError("Target URL must not contain a fragment")

    normalized_host = normalize_hostname(hostname)
    normalized_allowlist = {
        normalize_hostname(allowed_host) for allowed_host in (allowed_target_hosts or [])
    }
    if normalized_environment == "production" and not normalized_allowlist:
        raise InvalidTargetError("Production requires a non-empty target host allowlist")
    if normalized_allowlist and normalized_host not in normalized_allowlist:
        raise InvalidTargetError(f"Target host '{normalized_host}' is not allowlisted")

    try:
        literal_address = ipaddress.ip_address(normalized_host)
    except ValueError:
        literal_address = None

    target_port = port or (443 if scheme == "https" else 80)
    return normalized_host, target_port, literal_address


def validate_delivery_target(
    url: str,
    *,
    environment: str = "development",
    allowed_target_hosts: Sequence[str] | None = None,
    resolve_dns: bool = True,
    resolver: Callable[..., list[str]] | None = None,
    max_url_length: int = DEFAULT_MAX_TARGET_URL_LENGTH,
    dns_timeout: float = DEFAULT_DNS_LOOKUP_TIMEOUT_SECONDS,
) -> str:
    """Validate an outbound URL's syntax, policy, DNS results, and IP scope."""
    normalized_host, target_port, literal_address = _parse_and_validate_target_url(
        url,
        environment=environment,
        allowed_target_hosts=allowed_target_hosts,
        max_url_length=max_url_length,
    )

    if literal_address is not None:
        _validate_ip(str(literal_address))
    elif resolve_dns:
        target_resolver = resolver or resolve_hostname
        sig = inspect.signature(target_resolver)
        accepts_cancel = "cancel_event" in sig.parameters
        accepts_timeout = "timeout" in sig.parameters or "dns_timeout" in sig.parameters

        cancel_event = threading.Event()

        def _invoke_sync() -> list[str]:
            kwargs: dict[str, Any] = {}
            if accepts_cancel:
                kwargs["cancel_event"] = cancel_event
            if accepts_timeout:
                kwargs["timeout"] = dns_timeout
            return target_resolver(normalized_host, target_port, **kwargs)

        executor = _get_dns_executor()
        future = executor.submit(_invoke_sync)
        try:
            resolved_ips = future.result(timeout=dns_timeout)
        except TimeoutError as exc:
            cancel_event.set()
            future.cancel()
            raise TransientDNSError(
                f"DNS resolution timed out for target hostname '{normalized_host}' after {dns_timeout}s"
            ) from exc
        except (TargetValidationError, ValueError):
            raise
        except (socket.gaierror, OSError) as exc:
            raise TransientDNSError(
                f"DNS resolution failed for target hostname '{normalized_host}': {exc}"
            ) from exc
        except Exception as exc:
            raise TransientDNSError(
                f"DNS resolution failed for target hostname '{normalized_host}': {exc}"
            ) from exc

        if not resolved_ips:
            raise InvalidTargetError(f"No IP addresses resolved for target hostname '{normalized_host}'")
        for resolved_ip in resolved_ips:
            _validate_ip(resolved_ip)

    return url


async def validate_delivery_target_async(
    url: str,
    *,
    environment: str = "development",
    allowed_target_hosts: Sequence[str] | None = None,
    resolve_dns: bool = True,
    resolver: Callable[..., Any] | None = None,
    max_url_length: int = DEFAULT_MAX_TARGET_URL_LENGTH,
    dns_timeout: float = DEFAULT_DNS_LOOKUP_TIMEOUT_SECONDS,
    cancel_event: threading.Event | None = None,
) -> str:
    """Validate an outbound URL asynchronously with bounded DNS resolution."""
    normalized_host, target_port, literal_address = _parse_and_validate_target_url(
        url,
        environment=environment,
        allowed_target_hosts=allowed_target_hosts,
        max_url_length=max_url_length,
    )

    if literal_address is not None:
        _validate_ip(str(literal_address))
    elif resolve_dns:
        resolved_ips = await resolve_hostname_async(
            normalized_host,
            target_port,
            dns_timeout=dns_timeout,
            resolver=resolver,
            cancel_event=cancel_event,
        )
        for resolved_ip in resolved_ips:
            _validate_ip(resolved_ip)

    return url
