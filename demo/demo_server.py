"""
SIGS-RS — Interactive Demonstration Server Entrypoint.

Enables running the complete gateway locally without requiring external
Docker, PostgreSQL, Redis, or Mosquitto services.
Uses native development in-memory database and in-memory sliding-window
emulation for zero-dependency local viva/demo.

Run with:
    uvicorn demo.demo_server:app --host 0.0.0.0 --port 8000 --reload
"""
from __future__ import annotations

import sigs_rs.main
from tests.mock_sliding_window import MockAsyncSlidingWindow

# Configure in-memory sliding window for hermetic demo execution
sigs_rs.main.SlidingWindow = MockAsyncSlidingWindow

# Create FastAPI application instance
app = sigs_rs.main.create_app()
