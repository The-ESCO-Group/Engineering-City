"""Launching one allowlisted local program, and refusing everything else.

The optional Rockwell workstation adapter is the only caller. It exists to drive
*official Rockwell tooling* that an administrator has installed and named, on a
machine that holds the licence. This module is the narrow place where that
actually happens, so every constraint lives here rather than being repeated at
call sites.

The rules, in the order they are applied:

* **The executable must be on the allowlist, by resolved absolute path.** Not by
  name, not by prefix, not by the string the caller supplied. Both the allowlist
  entries and the requested path are fully resolved before they are compared, so
  neither a ``..`` segment nor a symlink can name one program and start another.
  A relative path is refused outright: it would resolve against whatever the
  working directory happened to be, which is not naming a program at all.
* **The working directory must be inside the isolated root**, proved with the
  existing containment helper, and must not be inside any directory the caller
  declares off limits. The adapter declares ``data/projects/`` off limits: a
  verification run must never be able to write over a real project.
* **There is a timeout, and exceeding it kills the process.** A licensed
  workstation is usually a site's only one, and an import that has stopped on a
  dialog nobody can see would otherwise hold it forever.
* **Output is captured, bounded and returned**, never streamed to a log store.

``shell=True`` does not appear in this module and must never be added. Neither
must any form of ``os.system``, ``popen`` or shell string interpolation: the
argument list is passed to the operating system as a list, so a value that
contains a space, a quote or an ampersand is an argument and cannot become a
command.

Nothing here connects to a controller. There is no upload, download, online
edit, mode change, force or firmware path in this module or in anything it can
reach — absent, not disabled.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from app.core.errors import AppError, FileRejectedError
from app.core.logging import get_logger
from app.security.paths import resolve_within

logger = get_logger(__name__)

#: Captured output is an *excerpt*. The full log stays on the workstation; a
#: database row is not a log store, and an import that emits fifty megabytes of
#: progress chatter must not be able to push that into the application database.
MAX_CAPTURED_OUTPUT_CHARS = 20_000

#: Environment variables whose *name* suggests a credential are dropped before
#: the child process starts. The adapter launches vendor software, which has no
#: business inheriting this application's signing key or a model API token.
_SECRET_NAME_MARKERS: tuple[str, ...] = (
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "PASSWD",
    "API_KEY",
    "APIKEY",
    "CREDENTIAL",
    "PRIVATE_KEY",
    "JWT",
    "SESSION",
)

#: The whole application configuration namespace goes too. None of it is
#: meaningful to Rockwell tooling and some of it is a secret.
_DROPPED_NAME_PREFIXES: tuple[str, ...] = ("ESCO_",)


class ExecutableNotAllowedError(AppError):
    """The requested program is not the one an administrator allowlisted.

    Deliberately its own error rather than a generic validation failure: this is
    the refusal that stops "run this verification job" from becoming "run this
    arbitrary program", and it needs to be visible as such in a log and in an
    API response.
    """

    status_code = 403
    code = "executable_not_allowed"


class WorkingDirectoryRefusedError(AppError):
    """The requested working directory is not the configured isolated one.

    Raised in particular when a path resolves inside the project store. A
    verification run writes files; if it can be pointed at ``data/projects/`` it
    can destroy the very package it was asked to check.
    """

    status_code = 400
    code = "working_directory_refused"


def scrubbed_environment(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The parent environment minus anything credential-shaped.

    Not an allowlist, because vendor installers depend on a long tail of
    variables (``SystemRoot``, ``TEMP``, ``PATH``, per-product roots) and an
    allowlist would silently break them. A deny-list of credential-shaped names
    is the honest trade: it removes what this application put there, and it is
    documented rather than assumed.
    """
    source = os.environ if base is None else base
    scrubbed: dict[str, str] = {}
    for name, value in source.items():
        upper = name.upper()
        if upper.startswith(_DROPPED_NAME_PREFIXES):
            continue
        if any(marker in upper for marker in _SECRET_NAME_MARKERS):
            continue
        scrubbed[name] = value
    return scrubbed


@dataclass(frozen=True, slots=True)
class ExecutableAllowlist:
    """The resolved absolute paths of the only programs that may be launched.

    Entries are resolved **once, at construction**, and the resolved paths are
    what a request is compared against. That ordering is the point: if an
    allowlisted path is later replaced by a symlink to something else, the
    request now resolves somewhere the stored entry does not, and it is refused.
    """

    entries: tuple[Path, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.entries)

    @classmethod
    def from_paths(cls, paths: Iterable[str | Path]) -> ExecutableAllowlist:
        """Resolve and de-duplicate ``paths``, refusing any relative entry."""
        resolved: list[Path] = []
        for raw in paths:
            text = str(raw).strip()
            if not text:
                continue
            candidate = Path(text)
            if not candidate.is_absolute():
                raise ExecutableNotAllowedError(
                    "An allowlist entry must be an absolute path; "
                    f"{text!r} would resolve against the working directory rather "
                    f"than naming one specific program.",
                    details={"entry": text},
                )
            resolved.append(candidate.resolve())
        # ``dict.fromkeys`` keeps administrator order, which is what an operator
        # sees echoed back in the capability report.
        return cls(entries=tuple(dict.fromkeys(resolved)))

    def resolve(self, requested: str | Path) -> Path:
        """Return the resolved path of ``requested``, or refuse it.

        The comparison is equality between fully resolved absolute paths, and
        nothing else. It is deliberately not ``Path.samefile``: that resolves the
        allowlist entry *again, now*, so replacing an allowlisted program with a
        symlink to a different one would make the two "the same file" and the
        swap would be waved through. Comparing against the path captured at
        construction is what makes the swap visible.

        The resolved path is also what is handed to the operating system, so the
        gap between checking and launching is the path lookup itself rather than
        a second, unchecked resolution.
        """
        text = str(requested).strip()
        if not text:
            raise ExecutableNotAllowedError(
                "No executable was named, so there is nothing to check against the allowlist."
            )
        if "\x00" in text:
            raise ExecutableNotAllowedError(
                "The executable path contains a null byte and was refused.",
                details={"requested": text.replace("\x00", "\\0")},
            )

        candidate = Path(text)
        if not candidate.is_absolute():
            raise ExecutableNotAllowedError(
                f"{text!r} is a relative path. The allowlist names specific programs by "
                f"absolute path; a relative path would name a different program "
                f"depending on the working directory.",
                details={"requested": text},
            )

        resolved = candidate.resolve()

        if not self.entries:
            raise ExecutableNotAllowedError(
                "The workstation executable allowlist is empty, so no program may be "
                "launched at all.",
                details={"requested": str(resolved)},
            )

        if not resolved.is_file():
            raise ExecutableNotAllowedError(
                f"{resolved} is not an existing file, so it cannot be the allowlisted "
                f"program it claims to be.",
                details={"requested": text, "resolved": str(resolved)},
            )

        if resolved in self.entries:
            return resolved

        logger.warning(
            "workstation adapter refused a non-allowlisted executable",
            extra={
                "requested": text,
                "resolved": str(resolved),
                "allowlist_size": len(self.entries),
            },
        )
        raise ExecutableNotAllowedError(
            f"{resolved} is not on the workstation executable allowlist. The adapter "
            f"may only launch programs an administrator named explicitly, and the "
            f"check is made on the resolved path so that a symlink or a '..' segment "
            f"cannot substitute a different program.",
            details={"requested": text, "resolved": str(resolved)},
        )


def resolve_isolated_directory(
    root: Path,
    *parts: str | Path,
    forbidden_roots: Sequence[Path] = (),
) -> Path:
    """Resolve a working directory inside ``root``, or refuse it.

    Containment is delegated to :func:`app.security.paths.resolve_within`, which
    performs the check on the *resolved* path, so a symlink planted inside the
    scratch root cannot be used to escape it. An absolute ``part`` is not a way
    out either: joining it produces a path outside ``root``, and the containment
    check then refuses it.

    ``forbidden_roots`` is checked in both directions. A scratch root *inside*
    the project store would let a run write over a project; a scratch root that
    *contains* the project store would let a run delete every project while
    "cleaning up after itself".
    """
    if not root.is_absolute():
        raise WorkingDirectoryRefusedError(
            f"The workstation working directory must be an absolute path; {root} is not.",
            details={"root": str(root)},
        )
    root_resolved = root.resolve()

    for forbidden in forbidden_roots:
        forbidden_resolved = forbidden.resolve()
        if root_resolved == forbidden_resolved or forbidden_resolved in root_resolved.parents:
            raise WorkingDirectoryRefusedError(
                f"The workstation working directory {root_resolved} is inside "
                f"{forbidden_resolved}. A verification run writes files, and it must "
                f"never be able to write over a production project.",
                details={"root": str(root_resolved), "forbidden": str(forbidden_resolved)},
            )
        if root_resolved in forbidden_resolved.parents:
            raise WorkingDirectoryRefusedError(
                f"The workstation working directory {root_resolved} contains {forbidden_resolved}. "
                f"Cleaning up a scratch directory would then remove the project store.",
                details={"root": str(root_resolved), "forbidden": str(forbidden_resolved)},
            )

    try:
        resolved = resolve_within(root_resolved, *parts)
    except FileRejectedError as exc:
        raise WorkingDirectoryRefusedError(
            f"The requested working directory is outside the configured isolated "
            f"directory {root_resolved}.",
            details={"root": str(root_resolved), "requested": "/".join(str(p) for p in parts)},
        ) from exc

    for forbidden in forbidden_roots:
        forbidden_resolved = forbidden.resolve()
        if resolved == forbidden_resolved or forbidden_resolved in resolved.parents:
            raise WorkingDirectoryRefusedError(
                f"{resolved} is inside {forbidden_resolved} and was refused.",
                details={"resolved": str(resolved), "forbidden": str(forbidden_resolved)},
            )
    return resolved


@dataclass(frozen=True, slots=True)
class CommandResult:
    """What one allowlisted run did, with its output bounded.

    ``timed_out`` is kept separate from ``returncode`` on purpose. A program that
    was killed for exceeding its timeout did not fail the check it was running —
    nothing is known about that check — and reporting it as a failure would be an
    invented result.
    """

    executable: str
    arguments: tuple[str, ...]
    working_directory: str
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool
    timeout_seconds: float

    @property
    def succeeded(self) -> bool:
        return not self.timed_out and self.returncode == 0

    @property
    def output_excerpt(self) -> str:
        """Both streams, labelled, bounded to :data:`MAX_CAPTURED_OUTPUT_CHARS`."""
        parts: list[str] = []
        if self.stdout:
            parts.append(f"[stdout]\n{self.stdout}")
        if self.stderr:
            parts.append(f"[stderr]\n{self.stderr}")
        return _truncate("\n".join(parts))


def _truncate(text: str, limit: int = MAX_CAPTURED_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    dropped = len(text) - limit
    return f"{text[:limit]}\n... [{dropped} further characters omitted]"


def _as_text(value: object) -> str:
    """Decode whatever the subprocess machinery handed back.

    On a timeout the partially captured output arrives as ``bytes`` on some
    platforms and ``str`` on others, so this is not defensive padding.
    """
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def run_allowlisted(
    executable: str | Path,
    arguments: Sequence[str] = (),
    *,
    allowlist: ExecutableAllowlist,
    working_directory: Path,
    timeout_seconds: float,
    environment: Mapping[str, str] | None = None,
) -> CommandResult:
    """Run one allowlisted program and return what it did.

    Never raises for a non-zero exit or a timeout — both are results, and the
    caller records them. Raises only when the request itself is refused:
    :class:`ExecutableNotAllowedError` or :class:`WorkingDirectoryRefusedError`.
    """
    resolved_executable = allowlist.resolve(executable)

    argument_list = [str(argument) for argument in arguments]
    for argument in argument_list:
        if "\x00" in argument:
            raise ExecutableNotAllowedError(
                "An argument contains a null byte and was refused.",
                details={"executable": str(resolved_executable)},
            )

    if timeout_seconds <= 0:
        raise WorkingDirectoryRefusedError(
            f"A timeout of {timeout_seconds} seconds is not a timeout. A run without one "
            f"can hold the site's licensed workstation indefinitely.",
            code="invalid_timeout",
        )

    if not working_directory.is_absolute():
        raise WorkingDirectoryRefusedError(
            f"The working directory must be absolute; {working_directory} is not.",
            details={"working_directory": str(working_directory)},
        )
    resolved_directory = working_directory.resolve()
    if not resolved_directory.is_dir():
        raise WorkingDirectoryRefusedError(
            f"The isolated working directory {resolved_directory} does not exist. It is "
            f"created deliberately rather than implicitly, so a typo cannot scatter "
            f"scratch directories across the filesystem.",
            details={"working_directory": str(resolved_directory)},
        )

    child_environment = dict(environment) if environment is not None else scrubbed_environment()

    logger.info(
        "workstation adapter launching allowlisted program",
        extra={
            "executable": str(resolved_executable),
            "argument_count": len(argument_list),
            "working_directory": str(resolved_directory),
            "timeout_seconds": timeout_seconds,
        },
    )

    started = time.monotonic()
    timed_out = False
    try:
        # shell=False is explicit and must stay that way: the argument list is
        # handed to the operating system as a list, so no value in it can become
        # a command. The executable has already been proved to be one of the
        # administrator's allowlisted absolute paths, which is what S603 asks
        # the author to establish.
        completed = subprocess.run(  # noqa: S603
            [str(resolved_executable), *argument_list],
            cwd=str(resolved_directory),
            env=child_environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            shell=False,
            check=False,
        )
        returncode = completed.returncode
        stdout = _as_text(completed.stdout)
        stderr = _as_text(completed.stderr)
    except subprocess.TimeoutExpired as exc:
        # ``subprocess.run`` has already killed the child by the time this is
        # raised. Grandchildren the vendor tooling spawned are not reaped by
        # that kill; the adapter records the working directory so a stuck run
        # can be finished off by hand on the workstation.
        timed_out = True
        returncode = -1
        stdout = _as_text(exc.stdout)
        stderr = _as_text(exc.stderr)
        logger.warning(
            "workstation adapter killed a run that exceeded its timeout",
            extra={
                "executable": str(resolved_executable),
                "working_directory": str(resolved_directory),
                "timeout_seconds": timeout_seconds,
            },
        )
    except OSError as exc:
        timed_out = False
        returncode = -1
        stdout = ""
        stderr = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "workstation adapter could not start the allowlisted program",
            extra={"executable": str(resolved_executable), "error": str(exc)},
        )

    duration = time.monotonic() - started

    return CommandResult(
        executable=str(resolved_executable),
        arguments=tuple(argument_list),
        working_directory=str(resolved_directory),
        returncode=returncode,
        stdout=_truncate(stdout),
        stderr=_truncate(stderr),
        duration_seconds=round(duration, 3),
        timed_out=timed_out,
        timeout_seconds=float(timeout_seconds),
    )
