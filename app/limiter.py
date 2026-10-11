import asyncio
import email.utils
import random
import time
from typing import Dict, Optional, Tuple, Any
from urllib.parse import urlparse
from app.config import settings
from app.database import Database


class OriginBlockedError(Exception):
    def __init__(self, origin: str, reason: str = "Origin blocked (403 or CAPTCHA detected)"):
        super().__init__(f"{origin}: {reason}")
        self.origin = origin
        self.reason = reason


class OriginThrottledError(Exception):
    def __init__(self, origin: str, retry_after: float):
        super().__init__(f"{origin}: Throttled, cooldown until {retry_after}")
        self.origin = origin
        self.retry_after = retry_after


def get_origin(url: str) -> str:
    parsed = urlparse(url)
    scheme = parsed.scheme.lower() or "http"
    host = parsed.hostname or "localhost"
    port = parsed.port
    if not port:
        port = 443 if scheme == "https" else 80
    return f"{scheme}://{host}:{port}"


def parse_retry_after(header_value: Optional[str]) -> Optional[float]:
    if not header_value:
        return None
    val = header_value.strip()
    try:
        # Delta-seconds
        delta = float(val)
        return max(0.0, delta)
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(val)
        delta = dt.timestamp() - time.time()
        return max(0.0, delta)
    except Exception:
        return None


class OriginLimiter:
    def __init__(self, origin: str, db: Database):
        self.origin = origin
        self.db = db
        self.lock = asyncio.Lock()
        self.last_request_start: float = 0.0
        self.is_blocked: bool = False
        self.consecutive_throttles: int = 0
        self.block_reason: str = ""

    def get_cooldown_until(self) -> float:
        stored = self.db.get_origin_cooldown(self.origin)
        return stored if stored is not None else 0.0

    def set_cooldown(self, cooldown_seconds: float):
        cd_until = time.time() + cooldown_seconds
        self.db.set_origin_cooldown(self.origin, cd_until)

    def clear_cooldown(self):
        self.db.clear_origin_cooldown(self.origin)
        self.consecutive_throttles = 0


class RateLimiter:
    def __init__(self, db: Database):
        self.db = db
        self.global_sem = asyncio.Semaphore(settings.GLOBAL_HTTP_CONCURRENCY)
        self.origins: Dict[str, OriginLimiter] = {}
        self._lock = asyncio.Lock()

    async def get_origin_limiter(self, origin: str) -> OriginLimiter:
        async with self._lock:
            if origin not in self.origins:
                self.origins[origin] = OriginLimiter(origin, self.db)
            return self.origins[origin]

    async def acquire(self, url: str, cancel_event: Optional[asyncio.Event] = None) -> Tuple[asyncio.Semaphore, OriginLimiter]:
        origin = get_origin(url)
        limiter = await self.get_origin_limiter(origin)

        # Check block status
        if limiter.is_blocked:
            raise OriginBlockedError(origin, limiter.block_reason)

        # Acquire global semaphore first
        await self.global_sem.acquire()
        try:
            # Acquire per-origin lock (1 in-flight per origin)
            await limiter.lock.acquire()
        except Exception:
            self.global_sem.release()
            raise

        try:
            # Check cooldown
            cd_until = limiter.get_cooldown_until()
            now = time.time()
            if cd_until > now:
                wait_time = cd_until - now
                await self._sleep_cancellable(wait_time, cancel_event)

            # Minimum interval between request starts with jitter
            now = time.time()
            jitter = random.uniform(0.0, settings.REQUEST_JITTER_SECONDS)
            min_interval = settings.REQUEST_INTERVAL_SECONDS + jitter
            time_since_last = now - limiter.last_request_start
            if time_since_last < min_interval:
                needed = min_interval - time_since_last
                await self._sleep_cancellable(needed, cancel_event)

            limiter.last_request_start = time.time()
            return self.global_sem, limiter
        except Exception:
            limiter.lock.release()
            self.global_sem.release()
            raise

    def release(self, limiter: OriginLimiter):
        limiter.lock.release()
        self.global_sem.release()

    async def _sleep_cancellable(self, seconds: float, cancel_event: Optional[asyncio.Event]):
        if seconds <= 0:
            return
        step = 0.2
        end = time.time() + seconds
        while time.time() < end:
            if cancel_event and cancel_event.is_set():
                raise asyncio.CancelledError("Crawl cancelled during rate-limit cooldown/spacing")
            remaining = end - time.time()
            await asyncio.sleep(min(step, remaining))

    def record_throttle(self, origin: str, retry_after_header: Optional[str] = None) -> float:
        now = time.time()
        limiter = self.origins.get(origin)
        if not limiter:
            limiter = OriginLimiter(origin, self.db)
            self.origins[origin] = limiter

        limiter.consecutive_throttles += 1
        delta = parse_retry_after(retry_after_header)
        if delta is None:
            # Exponential backoff with jitter
            factor = 2 ** max(0, limiter.consecutive_throttles - 1)
            base = settings.BACKOFF_INITIAL_SECONDS * factor
            jitter = random.uniform(0.0, 5.0)
            delta = min(settings.BACKOFF_MAX_SECONDS, base + jitter)

        limiter.set_cooldown(delta)
        return delta

    def record_block(self, origin: str, reason: str = "HTTP 403 / CAPTCHA detected"):
        limiter = self.origins.get(origin)
        if not limiter:
            limiter = OriginLimiter(origin, self.db)
            self.origins[origin] = limiter
        limiter.is_blocked = True
        limiter.block_reason = reason

    def record_success(self, origin: str):
        limiter = self.origins.get(origin)
        if limiter:
            limiter.consecutive_throttles = 0
            limiter.clear_cooldown()

    def get_status(self) -> Dict[str, Any]:
        now = time.time()
        res = {}
        for origin, lim in self.origins.items():
            cd = lim.get_cooldown_until()
            res[origin] = {
                "in_cooldown": cd > now,
                "cooldown_remaining": max(0.0, round(cd - now, 1)),
                "is_blocked": lim.is_blocked,
                "block_reason": lim.block_reason,
                "consecutive_throttles": lim.consecutive_throttles,
            }
        return res
