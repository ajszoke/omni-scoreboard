"""Typed game events: a generic `GameEvent[EventType, Payload]` base plus
sport-specific event-type enums and payloads.

Event-type enums are intentionally per-sport (no giant universal enum): a
baseball "score" and a football "score" carry different downstream concerns.
"""
