"""The running-app orchestration layer (ROADMAP A0 spine).

Grows into the deterministic appliance loop: a `Clock` seam, typed app/device
config, a provider supervisor, a contest store, the observation→delay→card→queue
pipeline, a renderer registry, and a ``run_once(now)`` the infinite loop wraps.
In so far: the clock, the provider supervisor, the contest store, and the
live-baseball pipeline.
"""

from __future__ import annotations

from omni.app.clock import Clock, FakeClock, SystemClock
from omni.app.contest_store import ContestStore, Reconciliation
from omni.app.pipeline import LiveBaseballPipeline, PipelineResult, StateFetcher
from omni.app.supervisor import BackoffPolicy, ProviderStatus, ProviderSupervisor, SupervisedSnapshot

__all__ = [
    "Clock",
    "SystemClock",
    "FakeClock",
    "ProviderSupervisor",
    "ProviderStatus",
    "BackoffPolicy",
    "SupervisedSnapshot",
    "ContestStore",
    "Reconciliation",
    "LiveBaseballPipeline",
    "PipelineResult",
    "StateFetcher",
]
