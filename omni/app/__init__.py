"""The running-app orchestration layer (ROADMAP A0 spine).

Grows into the deterministic appliance loop: a `Clock` seam, typed app/device
config, a provider supervisor, a contest store, the observation→delay→card→queue
pipeline, a renderer registry, and a ``run_once(now)`` the infinite loop wraps.
First in: the clock.
"""

from __future__ import annotations

from omni.app.clock import Clock, FakeClock, SystemClock

__all__ = ["Clock", "SystemClock", "FakeClock"]
