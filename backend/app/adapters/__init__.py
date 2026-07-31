"""Optional external-tooling adapters.

Currently one: the licensed Rockwell workstation adapter, which is **off by
default** and is the only route to ``ValidationLevel.ROCKWELL_IMPORT_VERIFIED``.
With it off — the normal case — every generated package stays an *import-ready
candidate* at ``ROUND_TRIP_PARSED``, and every call here answers with a
structured "unavailable, and here is why" instead of raising.

No adapter in this package connects to a controller. There is no upload,
download, online edit, mode change, force or firmware operation anywhere in it —
absent, not disabled. See :mod:`app.adapters.workstation` for the reasoning.

Obtain an adapter with :func:`~app.adapters.workstation.get_adapter`; it is the
only sanctioned constructor, and it returns the disabled one unless an
administrator has enabled and correctly configured a real workstation.
"""

from __future__ import annotations

from app.adapters.subprocess_runner import (
    MAX_CAPTURED_OUTPUT_CHARS,
    CommandResult,
    ExecutableAllowlist,
    ExecutableNotAllowedError,
    WorkingDirectoryRefusedError,
    resolve_isolated_directory,
    run_allowlisted,
    scrubbed_environment,
)
from app.adapters.workstation import (
    CAPABILITY_NAME,
    DISABLED_DETAIL,
    VERIFIABLE_CANDIDATE_STATUSES,
    AdapterHealth,
    DiagnosticsReport,
    DisabledWorkstationAdapter,
    InstalledProduct,
    LocalWorkstationAdapter,
    ProductInventory,
    ProfileSupportReport,
    RockwellWorkstationAdapter,
    TargetProfileSupport,
    VerificationCommand,
    VerificationDiagnostic,
    VerificationOutcome,
    VerificationRefusedError,
    WorkstationAdapterDisabledError,
    capability_row,
    capability_rows,
    get_adapter,
)

__all__ = [
    "CAPABILITY_NAME",
    "DISABLED_DETAIL",
    "MAX_CAPTURED_OUTPUT_CHARS",
    "VERIFIABLE_CANDIDATE_STATUSES",
    "AdapterHealth",
    "CommandResult",
    "DiagnosticsReport",
    "DisabledWorkstationAdapter",
    "ExecutableAllowlist",
    "ExecutableNotAllowedError",
    "InstalledProduct",
    "LocalWorkstationAdapter",
    "ProductInventory",
    "ProfileSupportReport",
    "RockwellWorkstationAdapter",
    "TargetProfileSupport",
    "VerificationCommand",
    "VerificationDiagnostic",
    "VerificationOutcome",
    "VerificationRefusedError",
    "WorkingDirectoryRefusedError",
    "WorkstationAdapterDisabledError",
    "capability_row",
    "capability_rows",
    "get_adapter",
    "resolve_isolated_directory",
    "run_allowlisted",
    "scrubbed_environment",
]
