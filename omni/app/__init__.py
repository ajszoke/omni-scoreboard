"""The running-app orchestration layer (ROADMAP A0 spine).

Grows into the deterministic appliance loop: a `Clock` seam, typed app/device
config, a provider supervisor, a contest store, the observation→delay→card→queue
pipeline, a renderer registry, and a ``run_once(now)`` the infinite loop wraps.
In so far: the clock (UTC and monotonic readings), the provider supervisor, the
contest store, the live-baseball pipeline, the display sinks, the deterministic
`AppLoop` (separate decision and paint passes), and the `build_loop` / `run_forever`
runner that powers ``python -m omni.app``.
"""

from __future__ import annotations

from omni.core.clock import Clock, ClockReading, FakeClock, MonotonicTime, Pacer, SystemClock
from omni.app.contest_store import ContestStore, Reconciliation
from omni.app.display import DisplaySink, MatrixDevice, MatrixDisplaySink, RecordingDisplaySink
from omni.app.loop import AppLoop, Decision, Painted, Presentation, TickResult
from omni.app.pipeline import FeedFetcher, LiveBaseballPipeline, PipelineResult
from omni.app.runner import build_loop, run_forever
from omni.app.supervisor import BackoffPolicy, ProviderStatus, ProviderSupervisor, SupervisedSnapshot

__all__ = [
    "Clock",
    "ClockReading",
    "MonotonicTime",
    "Pacer",
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
    "FeedFetcher",
    "DisplaySink",
    "RecordingDisplaySink",
    "MatrixDevice",
    "MatrixDisplaySink",
    "AppLoop",
    "Decision",
    "Painted",
    "Presentation",
    "TickResult",
    "build_loop",
    "run_forever",
]
