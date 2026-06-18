"""Typed renderable cards: a generic `ScoreboardCard` plus per-sport payloads.

A card is what the queue schedules and a renderer draws. It owns its own timing,
priority, dedupe identity, and which panel profiles it supports — renderers never
fetch data or parse provider JSON.
"""
