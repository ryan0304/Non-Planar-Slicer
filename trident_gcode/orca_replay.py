"""Replay parsed OrcaSlicer moves onto this app's own GcodeWriter.

Orca's own text is never trusted directly. Every extruding move is replayed
via GcodeWriter.extrude_to() with a derived line_width_override computed
from Orca's own (E-delta, 3D segment length), so the writer's *existing*
extrusion_for_segment() formula and clamp bands reproduce Orca's intended
volume by construction -- never by injecting a raw E value. Orca's F passes
through as an ordinary speed= request, re-clamped by the writer's own
_allowed_speed()/volumetric-flow cap against *our* PrinterProfile, not
Orca's assumptions. Retraction amounts, temperatures, and fan speed are
never read from Orca's text at all -- they come from the same
PrinterProfile/FilamentSettings values the non-planar phase already uses.
Every move still passes through GcodeWriter's own _check_bounds() and
non-finite guards exactly once, from exactly one code path, regardless of
whether it originated here or in a native generator.
"""
from __future__ import annotations

import math

from .gcode import GcodeWriter
from .orca_gcode_parser import OrcaMove
from .profile import PrinterProfile

_EPS_XYZ = 1e-4   # mm -- "did XY/Z actually change" tolerance
_EPS_E = 1e-4     # mm -- "is this E delta actually zero" tolerance


def replay_moves_onto_writer(
    writer: GcodeWriter,
    moves: list[OrcaMove],
    *,
    profile: PrinterProfile,
) -> dict:
    """Replay a parsed Orca move list onto *writer*, returning a report dict."""
    extrude_count = 0
    travel_count = 0
    retract_count = 0
    unretract_count = 0
    filament_mm = 0.0
    retracted = False   # tracks the extruder's retraction state across the replay

    for move in moves:
        wx, wy, wz = writer.position
        same_xyz = (
            abs(move.x - wx) < _EPS_XYZ
            and abs(move.y - wy) < _EPS_XYZ
            and abs(move.z - wz) < _EPS_XYZ
        )
        e = move.e_delta

        if e is not None and e <= -_EPS_E and same_xyz:
            writer.retract()
            retract_count += 1
            retracted = True
            continue

        if e is not None and e >= _EPS_E and same_xyz:
            writer.unretract()
            unretract_count += 1
            retracted = False
            continue

        if e is not None and e >= _EPS_E:
            dx, dy, dz = move.x - wx, move.y - wy, move.z - wz
            length_3d = math.hypot(dx, dy, dz)
            if abs(dz) < 1e-6:
                lh_override = writer.layer_height
            else:
                lo, hi = 0.25 * writer.layer_height, 1.5 * writer.layer_height
                lh_override = min(max(abs(dz), lo), hi)
            orca_volume = e * profile.filament_area
            lw_override = orca_volume / max(length_3d * lh_override, 1e-9)
            writer.extrude_to(
                move.x, move.y, move.z,
                speed=move.feed_mm_s,
                layer_height_override=lh_override,
                line_width_override=lw_override,
            )
            extrude_count += 1
            filament_mm += e
            retracted = False
            continue

        if not same_xyz:
            # No meaningful E on this move (or a negative delta with XY/Z
            # also changing, which Orca does not normally emit -- retracts
            # are their own zero-length move) -- an ordinary travel.
            writer.travel(move.x, move.y, move.z)
            travel_count += 1
            continue

        # same_xyz and no meaningful E: a genuine no-op line (e.g. a bare
        # feed-rate change with no move). Nothing to replay.

    return {
        "extrude_count": extrude_count,
        "travel_count": travel_count,
        "retract_count": retract_count,
        "unretract_count": unretract_count,
        "filament_mm": filament_mm,
        "ends_retracted": retracted,
    }
