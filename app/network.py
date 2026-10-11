import asyncio
import ipaddress
import socket
from typing import Optional, Tuple
from urllib.parse import urlparse
import httpx
from app.config import settings
from app.limiter import RateLimiter, OriginBlockedError, OriginThrottledError, get_origin


def is_public_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast:
            return False
        # Support RFC 6052 Well-Known Prefix (64:ff9b::/96) used in NAT64 networks
        if isinstance(ip, ipaddress.IPv6Address) and ip in ipaddress.IPv6Network("64:ff9b::/96"):
            ipv4_int = int(ip) & 0xFFFFFFFF
            ipv4 = ipaddress.IPv4Address(ipv4_int)
            return not (ipv4.is_private or ipv4.is_loopback or ipv4.is_link_local or ipv4.is_reserved or ipv4.is_multicast)
        if ip.is_reserved:
            return False
        return True
    except ValueError:
        return False


def validate_public_url(url: str):
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ("http", "https"):
        raise ValueError(f"Unsupported URL scheme: {parsed.scheme}. Only HTTP/HTTPS permitted.")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL must have a valid hostname.")
    if hostname.lower() in ("localhost", "127.0.0.1", "::1"):
        raise ValueError(f"Blocked loopback destination: {hostname}")

    # Resolve hostname to check IPs
    try:
        addr_info = socket.getaddrinfo(hostname, None)
        for family, _, _, _, sockaddr in addr_info:
            ip_str = sockaddr[0]
            if not is_public_ip(ip_str):
                raise ValueError(f"Blocked private/loopback IP address {ip_str} for host {hostname}")
    except socket.gaierror:
        # If DNS cannot resolve, let httpx fail normally
        pass


class NetworkClient:
    def __init__(self, limiter: RateLimiter):
        self.limiter = limiter
        self.user_agent = "SmartScraper/0.1 (+https://github.com/vick/smartscraper; Public Document Assistant)"

    async def fetch(
        self,
        url: str,
        cancel_event: Optional[asyncio.Event] = None,
        max_retries: Optional[int] = None,
        timeout: Optional[float] = None,
        follow_redirects: bool = True
    ) -> httpx.Response:
        validate_public_url(url)
        retries = max_retries if max_retries is not None else settings.MAX_HTTP_RETRIES
        req_timeout = timeout if timeout is not None else settings.HTTP_TIMEOUT_SECONDS
        origin = get_origin(url)

        attempt = 0
        while attempt <= retries:
            attempt += 1
            if cancel_event and cancel_event.is_set():
                raise asyncio.CancelledError("Crawl cancelled before request execution")

            sem, orig_limiter = await self.limiter.acquire(url, cancel_event)
            try:
                headers = {"User-Agent": self.user_agent}
                async with httpx.AsyncClient(
                    timeout=req_timeout,
                    follow_redirects=follow_redirects
                ) as client:
                    resp = await client.get(url, headers=headers)

                    # Validate redirect target if redirected
                    if resp.history:
                        for prev in resp.history:
                            if prev.has_redirect_location:
                                validate_public_url(str(prev.headers.get("location", "")))

                    # Handle block status (403 or captcha)
                    if resp.status_code == 403 or "captcha" in resp.text.lower()[:2000]:
                        self.limiter.record_block(origin, f"HTTP {resp.status_code} / block detected")
                        raise OriginBlockedError(origin, f"Blocked with status {resp.status_code}")

                    # Handle throttling (429 or 503)
                    if resp.status_code in (429, 503):
                        retry_after = resp.headers.get("Retry-After")
                        cooldown = self.limiter.record_throttle(origin, retry_after)
                        if attempt > retries:
                            resp.raise_for_status()
                        # Cool down before next retry
                        await asyncio.sleep(min(cooldown, 30.0))
                        continue

                    resp.raise_for_status()
                    self.limiter.record_success(origin)
                    return resp

            except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as net_err:
                if attempt > retries:
                    raise
                await asyncio.sleep(2.0 * attempt)
            finally:
                self.limiter.release(orig_limiter)

        raise httpx.RequestError(f"Failed to fetch {url} after {retries} retries", request=None)
