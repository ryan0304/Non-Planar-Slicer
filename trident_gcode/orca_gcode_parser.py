"""Parse OrcaSlicer's G-code output into a normalized, trustworthy move list.

This module treats Orca's text output as untrusted input -- it comes from an
external binary, not from this app's own generators -- and parses it the same
way printer_validate.py parses a hand-edited printer.cfg: fail closed. An
explicit ALLOW-list of commands (not a block-list), a hard rejection of arc
moves (G2/G3, which this parser has no primitive for) and relative
positioning (G91, which nothing else in this app uses), and an explicit
math.isfinite() check on every numeric token -- because float("nan") and
float("inf") both *succeed* in Python and would otherwise sail straight
through every downstream min()/max() clamp, exactly the trap CLAUDE.md
documents for JSON's bare NaN/Infinity tokens, recreated here in G-code form.

Callers must pass Orca's G-code BODY only -- the toolpath moves, with Orca's
own machine start/end G-code left empty in the process profile that produced
it (see orca_slice.py::build_machine_json). This app supplies its own
PRINT_START/PRINT_END, temperatures, retraction, and fan curve from
PrinterProfile/FilamentSettings, so Orca's own heating/homing/fan commands
are never wanted here; the strict allow-list below is deliberately too
narrow to admit them, so a start/end G-code leak is caught immediately as an
"unrecognized command" error rather than silently replayed.

E is always returned as a per-move RELATIVE delta regardless of whether Orca
emitted M82 (absolute) or M83 (relative) extrusion for a given body -- the
parser normalizes ahead of time so replay code never has to track extrusion
mode itself.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .printer_validate import _command_text


class OrcaGcodeParseError(ValueError):
    """Raised when Orca's G-code output contains something this app will not trust."""


@dataclass(frozen=True)
class OrcaMove:
    x: float
    y: float
    z: float
    e_delta: float | None      # None = no E on this line (a pure travel/feed-rate line)
    feed_mm_s: float | None    # None = F unchanged from the previous move


# Explicit allow-list, not a block-list -- matches printer_validate.py's
# posture of erring toward rejection for anything not specifically vetted.
#
# Recognized but IGNORED (no state effect, no OrcaMove emitted): confirmed
# against a real OrcaSlicer install's actual output for a small klipper
# base --
#   G21                 units-in-mm declaration (this app only ever emits mm)
#   M73                  print-progress percentage, purely informational
#   M104/M109/M140/M190  nozzle/bed temp set (and set-and-wait) -- Orca
#                        inserts these for e.g. a first-layer-vs-steady-state
#                        temp transition; VALUES are never trusted, exactly
#                        like M106/M107's fan speed below -- this app's own
#                        GcodeWriter.header() is the only source of truth for
#                        temperatures (decision: never read from Orca's text)
#   SET_VELOCITY_LIMIT   Klipper per-layer accel/velocity hint -- this app
#                        computes its own feedrates via PrinterProfile, so
#                        Orca's own internal velocity planning is irrelevant
_IGNORED_COMMANDS = {
    "G21", "M73", "M104", "M106", "M107", "M109", "M140", "M190",
    "SET_VELOCITY_LIMIT",
}
_ALLOWED_COMMANDS = {"G0", "G1", "G90", "G91", "G92", "M82", "M83"} | _IGNORED_COMMANDS
_REJECTED_ARC_COMMANDS = {"G2", "G3"}


def _finite(raw: str, *, line_no: int, axis: str) -> float:
    try:
        v = float(raw)
    except ValueError:
        raise OrcaGcodeParseError(
            f"line {line_no}: {axis} value {raw!r} is not a number"
        ) from None
    # float() happily parses "nan"/"inf"/"-inf" -- those must be rejected
    # explicitly, the same lesson CLAUDE.md documents for json.loads.
    if not math.isfinite(v):
        raise OrcaGcodeParseError(
            f"line {line_no}: {axis} value {raw!r} is not a finite number"
        )
    return v


def parse_orca_gcode(text: str) -> list[OrcaMove]:
    """Parse Orca G-code BODY text into a normalized move list.

    Raises OrcaGcodeParseError on anything outside the explicit allow-list
    above, on G91, on G2/G3, or on any non-finite numeric token. Never
    guesses or best-effort-recovers -- see module docstring.
    """
    stripped = _command_text(text)
    moves: list[OrcaMove] = []

    x = y = z = 0.0
    absolute_e = True   # G-code convention before any M82/M83 is seen
    e_abs = 0.0          # running absolute E position, meaningful only in M82 mode
    feed: float | None = None

    for line_no, raw_line in enumerate(stripped.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        tokens = line.split()
        command = tokens[0].upper()

        if command in _REJECTED_ARC_COMMANDS:
            raise OrcaGcodeParseError(
                f"line {line_no}: arc moves ({command}) are not supported; "
                "disable arc fitting in the Orca process profile"
            )
        if command == "G91":
            raise OrcaGcodeParseError(
                f"line {line_no}: relative positioning (G91) is not supported"
            )
        if command not in _ALLOWED_COMMANDS:
            raise OrcaGcodeParseError(
                f"line {line_no}: unrecognized command {command!r}"
            )

        if command == "G90":
            continue  # absolute positioning is the only mode this parser supports; no-op
        if command == "M82":
            absolute_e = True
            continue
        if command == "M83":
            absolute_e = False
            continue
        if command in _IGNORED_COMMANDS:
            continue

        if command == "G92":
            # Coordinate-system reset, not a physical move: update whichever
            # axes are given, emit no OrcaMove.
            for tok in tokens[1:]:
                axis = tok[0].upper()
                val_raw = tok[1:]
                if not val_raw:
                    continue
                if axis == "X":
                    x = _finite(val_raw, line_no=line_no, axis="X")
                elif axis == "Y":
                    y = _finite(val_raw, line_no=line_no, axis="Y")
                elif axis == "Z":
                    z = _finite(val_raw, line_no=line_no, axis="Z")
                elif axis == "E":
                    e_abs = _finite(val_raw, line_no=line_no, axis="E")
            continue

        # G0 / G1: an actual (possibly zero-length) move.
        new_x, new_y, new_z = x, y, z
        e_here: float | None = None
        for tok in tokens[1:]:
            axis = tok[0].upper()
            val_raw = tok[1:]
            if not val_raw:
                continue
            if axis == "X":
                new_x = _finite(val_raw, line_no=line_no, axis="X")
            elif axis == "Y":
                new_y = _finite(val_raw, line_no=line_no, axis="Y")
            elif axis == "Z":
                new_z = _finite(val_raw, line_no=line_no, axis="Z")
            elif axis == "E":
                e_here = _finite(val_raw, line_no=line_no, axis="E")
            elif axis == "F":
                feed = _finite(val_raw, line_no=line_no, axis="F") / 60.0  # mm/min -> mm/s
            # Any other axis letter is intentionally ignored rather than rejected.

        e_delta: float | None = None
        if e_here is not None:
            if absolute_e:
                e_delta = e_here - e_abs
                e_abs = e_here
            else:
                e_delta = e_here

        moves.append(OrcaMove(x=new_x, y=new_y, z=new_z, e_delta=e_delta, feed_mm_s=feed))
        x, y, z = new_x, new_y, new_z

    return moves
