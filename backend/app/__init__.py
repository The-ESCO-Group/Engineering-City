"""ESCO Engineering City backend.

A local-first, evidence-based engineering review platform. The application never
connects to an industrial controller, never modifies a running PLC, and never
presents model output as approved engineering.
"""

from __future__ import annotations

__version__ = "0.1.0"

#: Version of the published API + event contracts under ``contracts/``.
#: Bumped only for breaking changes; the frontend generates its client from it.
CONTRACT_VERSION = "1.0.0"
