"""Fixed-window rate limiting, in process.

Good enough for a single API task. Behind a load balancer with several tasks the
limit becomes per-task, so move this to Redis before scaling out -- the call
sites do not change, only this file.
"""
import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request


class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
        """Returns (allowed, seconds_until_reset)."""
        if limit <= 0:
            return True, 0
        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            q = self._hits[key]
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= limit:
                return False, int(q[0] + window_seconds - now) + 1
            q.append(now)
            if len(self._hits) > 20000:  # keep the dict from growing without bound
                for k in [k for k, v in list(self._hits.items())[:5000] if not v]:
                    self._hits.pop(k, None)
            return True, 0


limiter = RateLimiter()


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def enforce(request: Request, bucket: str, limit: int, window_seconds: int) -> None:
    allowed, retry_after = limiter.hit(f"{bucket}:{client_ip(request)}", limit, window_seconds)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail={
                "code": "RATE_LIMITED",
                "message": f"Too many requests. Try again in {retry_after} seconds.",
            },
            headers={"Retry-After": str(retry_after)},
        )
