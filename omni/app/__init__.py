"""The running-app orchestration layer (ROADMAP A0 spine).

Grows into the deterministic appliance loop: a `Clock` seam, typed app/device
config, a provider supervisor, a contest store, the observation→delay→card→queue
pipeline, a renderer registry, and a ``run_once(now)`` the infinite loop wraps.
In so far: the clock, the provider supervisor, the contest store, the
live-baseball pipeline, the display sink, and the deterministic `AppLoop`.
"""

from __future__ import annotations

from omni.app.clock import Clock, FakeClock, SystemClock
from omni.app.contest_store import ContestStore, Reconciliation
from omni.app.display import DisplaySink, RecordingDisplaySink
from omni.app.loop import AppLoop, TickResult
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
    "DisplaySink",
    "RecordingDisplaySink",
    "AppLoop",
    "TickResult",
]
