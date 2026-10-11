import asyncio
import time
import pytest
from pathlib import Path
from app.database import Database
from app.limiter import (
    RateLimiter, parse_retry_after, get_origin, OriginBlockedError
)


def test_parse_retry_after():
    # Delta seconds
    assert parse_retry_after("120") == 120.0
    assert parse_retry_after("0") == 0.0

    # HTTP-date
    # Future date string
    http_date = "Sun, 11 Oct 2026 13:00:00 GMT"
    val = parse_retry_after(http_date)
    # val should be float or None
    assert val is not None

    # Invalid / empty
    assert parse_retry_after("") is None
    assert parse_retry_after(None) is None
    assert parse_retry_after("invalid-date") is None


def test_get_origin():
    assert get_origin("https://ww2.arb.ca.gov/approved-comments?page=0") == "https://ww2.arb.ca.gov:443"
    assert get_origin("http://example.com/api/test") == "http://example.com:80"
    assert get_origin("https://example.com:8443/docs") == "https://example.com:8443"


@pytest.mark.asyncio
async def test_limiter_cooldown_persistence(tmp_path: Path):
    db_file = tmp_path / "test_limiter.db"
    db = Database(db_file)
    limiter = RateLimiter(db)

    origin = "https://example.com:443"
    limiter.record_throttle(origin, "5")

    # Verify stored in DB
    cd = db.get_origin_cooldown(origin)
    assert cd is not None
    assert cd > time.time()

    # Create new limiter instance with same DB to test persistence across restarts
    limiter2 = RateLimiter(db)
    orig_lim = await limiter2.get_origin_limiter(origin)
    assert orig_lim.get_cooldown_until() == cd


@pytest.mark.asyncio
async def test_limiter_blocking_on_403(tmp_path: Path):
    db = Database(tmp_path / "block.db")
    limiter = RateLimiter(db)

    origin = "https://blocked-site.com:443"
    limiter.record_block(origin, "HTTP 403 Forbidden")

    status = limiter.get_status()
    assert status[origin]["is_blocked"] is True

    # Attempting to acquire should immediately raise OriginBlockedError
    with pytest.raises(OriginBlockedError):
        await limiter.acquire("https://blocked-site.com/subpage")


@pytest.mark.asyncio
async def test_limiter_cancellation_during_wait(tmp_path: Path):
    db = Database(tmp_path / "cancel.db")
    limiter = RateLimiter(db)

    url = "https://wait-site.com:443"
    # Set a 10s cooldown
    limiter.record_throttle("https://wait-site.com:443", "10")

    cancel_ev = asyncio.Event()

    async def _cancel_after_delay():
        await asyncio.sleep(0.1)
        cancel_ev.set()

    asyncio.create_task(_cancel_after_delay())

    with pytest.raises(asyncio.CancelledError):
        await limiter.acquire(url, cancel_event=cancel_ev)
