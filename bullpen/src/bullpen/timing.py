import time
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def frame_pacer(budget: float) -> Iterator[None]:
    """Hold the wrapped block to at least `budget` seconds.

    Used to ensure consistency (smooth transitions) between rendered frames.

    Stamps a monotonic clock on entry and, on exit, sleeps off any time
    remaining in the budget. If the block already overran it, no sleep occurs.
    """
    start = time.monotonic()

    yield

    elapsed = time.monotonic() - start
    if elapsed < budget:
        time.sleep(budget - elapsed)
