"""Loop-fabric wall generator: knitted / chainmail vases.

The wall is NOT a solid bead — it is stacked rows of VERTICAL loop stitches,
like knitting.  Each spiral turn climbs ``row_mm`` (several millimetres), and
along the turn the strand continuously traces cursive loop-de-loops in the
wall plane: per stitch the nozzle advances along the perimeter while dipping
``up_mm`` down and back up, self-crossing so the extruded strand forms a
closed loop hanging BELOW the row line.  Because the loop height exceeds the
row pitch, every loop reaches past the row beneath and hooks around it — the
rows interlock into a fabric.

A short solid cuff (normal vase-mode turns) anchors the first row to the bed.

Stitch parametrisation (u in [0,1], one stitch spanning arc angle dtheta):

    theta(u) = theta0 + dtheta * (u - q * sin(2*pi*u))     q ~ 0.30
    z(u)     = line_z - up_mm * (1 - cos(2*pi*u)) / 2
    r(u)     = R(theta) + out_mm * (1 - cos(2*pi*u)) / 2

The sin term drives theta BACKWARD near the stitch ends (q*2*pi > 1), so the
path crosses itself at the row line — a genuine loop, not a zigzag.
"""
from __future__ import annotations

import math
from typing import Callable

from ..blobs import LoopSpec
from ..gcode import GcodeWriter
from ..paths import cage_scale

_SEGS_PER_STITCH = 12
_LOOP_Q = 0.30            # backtrack fraction: >1/(2*pi) guarantees self-crossing
_CUFF_HOOK_MM = 0.8       # how far row 0's loops dip below the cuff top edge
_MIN_ROW_MM = 0.5         # smallest row pitch this generator will build
_INTERLOCK_MM = 0.3       # loop must clear the row pitch by at least this much
# Least Z clearance a DIP-mode fabric can be built in: the row pitch cannot go
# below _MIN_ROW_MM and the loop has to reach _INTERLOCK_MM past it, while in
# dip mode the whole loop height is the nozzle's excursion below the line and
# so must fit under z_amp_max. Spike mode is exempt -- its stitch stands ABOVE
# the line, so only (loop_h - row_mm) is an excursion and it can interlock in
# any clearance at all.
_MIN_DIP_CLEARANCE_MM = _MIN_ROW_MM + _INTERLOCK_MM


def build_loop_fabric(
    writer: GcodeWriter,
    shape: Callable[[float], float],
    height: float,
    spec: LoopSpec,
    radius_envelope: Callable[[float], float] | None = None,
    cage: list | None = None,
    center: tuple[float, float] | None = None,
    first_layer_squish: float = 0.75,
    cuff_lh: float = 0.3,
    travel_z_clearance: float = 5.0,
    points_per_turn: int = 240,
    fan_speed: float | None = None,
) -> dict:
    """Emit a complete loop-fabric print and return a report dict.

    ``fan_speed`` (0..1), when given, overrides ``writer.fan_speed`` for the
    fan turned on after the cuff. Loop fabric has no per-point wall tilt to
    ramp against (it's stitches, not a wall), so unlike the parametric wall
    there is no min->max overhang ramp here -- just a flat commanded speed.
    None (default) falls back to ``writer.fan_speed``, byte-identical output.
    """
    profile = writer.profile
    cx, cy = center if center is not None else profile.bed_center

    # Strand extrusion: fabric strands are round-ish beads about a nozzle
    # diameter tall — decoupled from the row pitch (which is several mm).
    strand_h = min(max(profile.nozzle_diameter, 0.3), 0.6)
    writer.layer_height = strand_h

    stitches = max(4, spec.loops_per_turn)
    row_mm = max(_MIN_ROW_MM, spec.row_mm)
    mode = spec.stitch_mode if spec.stitch_mode in ("dip", "spike") else "dip"
    loop_h = max(row_mm + _INTERLOCK_MM, spec.up_mm)  # exceed pitch to interlock
    wave_amp = max(0.0, spec.wave_amp)
    # Machine Z-excursion ceiling: on probe-behind-nozzle machines (Trident)
    # the nozzle may only sit z_amp_max below nearby printed material, or the
    # trailing probe strikes the fabric. Probe-less printers ride free.
    z_cap = getattr(profile, "z_amp_max", 0.95)
    # Refuse rather than emit a fabric that cannot hold together. Below this
    # clearance the dip-mode clamp further down drives loop_h under row_mm --
    # the loop ends up SHORTER than the row pitch, so it never reaches the row
    # beneath and the rows come out as separate, unconnected rings. The file
    # looks fine and the part falls apart. No shipped profile is anywhere near
    # this (the Trident's 0.95 is the tightest), but an imported custom printer
    # can declare any clearance it likes.
    if mode == "dip" and z_cap < _MIN_DIP_CLEARANCE_MM:
        raise ValueError(
            f"{getattr(profile, 'name', 'This printer')} allows only "
            f"{z_cap:.2f}mm of Z clearance, and a dipped loop fabric needs at "
            f"least {_MIN_DIP_CLEARANCE_MM:.2f}mm ({_MIN_ROW_MM:.2f}mm row "
            f"pitch plus {_INTERLOCK_MM:.2f}mm of interlock) or the loops "
            f"cannot reach the row below and the rows never join. Use the "
            f"spike stitch, which stands above the row line instead.")
    z_clamped = False
    if mode == "spike":
        # Spikes rise ABOVE the line: the probe exposure is only the NEXT
        # row's pass below the standing tips = (loop_h - row_mm). Probed
        # machines can therefore keep full open rows in this mode.
        if profile.has_probe and (loop_h - row_mm) > z_cap:
            loop_h = row_mm + z_cap
            z_clamped = True
        if (wave_amp > 0.0 and profile.has_probe
                and (loop_h - row_mm) + 2.0 * wave_amp > z_cap):
            wave_amp = max(0.0, (z_cap - (loop_h - row_mm)) / 2.0)
            z_clamped = True
    else:
        # Dip mode: the nozzle descends the full loop height below the line.
        if loop_h > z_cap:
            loop_h = z_cap
            # Pull the pitch down with it so the loop still clears the row
            # beneath by _INTERLOCK_MM. The guard above guarantees z_cap is
            # large enough for this to land at or above _MIN_ROW_MM, so the
            # result can no longer invert (loop shorter than the pitch).
            row_mm = min(row_mm, max(loop_h - _INTERLOCK_MM, _MIN_ROW_MM))
            z_clamped = True
        if (wave_amp > 0.0 and profile.has_probe
                and loop_h + 2.0 * wave_amp > z_cap):
            wave_amp = max(0.0, (z_cap - loop_h) / 2.0)
            z_clamped = True
    n_waves = max(1, spec.waves)
    cuff_hook = min(_CUFF_HOOK_MM, 0.4 * loop_h)
    cuff_turns = max(1, spec.cuff_turns)
    stagger = spec.align != "column"

    def _radius(theta: float, z: float) -> float:
        t = min(1.0, max(0.0, z / max(height, 1e-6)))
        r = shape(theta)
        if radius_envelope is not None:
            r *= radius_envelope(t)
        if cage is not None:
            r *= cage_scale(cage, theta, t)   # asymmetric local deformation
        return r

    # ---- vertical layout -----------------------------------------------------
    z0 = first_layer_squish * cuff_lh
    cuff_top = z0 + cuff_turns * cuff_lh
    if mode == "spike":
        # Row 0's line sits right on the cuff; its spikes stand up from it.
        line0 = cuff_top + strand_h + wave_amp
        n_rows = max(1, int((height - line0) / row_mm) + 1)
        # Highest point: last row's spike tips or the closing rim, whichever
        # reaches further (rows are planar rings; rim = row_mm + 2 strands).
        top_z = (line0 + (n_rows - 1) * row_mm
                 + max(loop_h, row_mm + 2.0 * strand_h) + wave_amp)
    else:
        # Row 0's line sits so its loops dip cuff_hook below the cuff top.
        line0 = cuff_top + loop_h - cuff_hook + wave_amp
        n_rows = max(1, int((height - line0) / row_mm) + 1)
        top_z = (line0 + (n_rows - 1) * row_mm
                 + 3.0 * strand_h + wave_amp)

    # ---- safety ----------------------------------------------------------------
    max_r = 0.0
    for k in range(points_per_turn):
        th = 2.0 * math.pi * k / points_per_turn
        for zz in (0.0, height * 0.5, height):
            max_r = max(max_r, _radius(th, zz))
    fit_r = max_r + spec.out_mm
    if (cx - fit_r < profile.print_min_x or cx + fit_r > profile.print_max_x
            or cy - fit_r < profile.print_min_y or cy + fit_r > profile.print_max_y):
        raise ValueError(
            f"Fabric footprint radius {fit_r:.1f} at center ({cx:.0f},{cy:.0f}) "
            f"falls outside the safe print area. Shrink the radius.")
    if top_z > profile.z_max:
        raise ValueError(
            f"Print top Z {top_z:.1f} exceeds Z max {profile.z_max}. "
            f"Reduce height.")

    # ---- emit -------------------------------------------------------------------
    writer.header()
    if z_clamped:
        # "probe keep-out" is only true on a machine that HAS a trailing
        # probe (Trident); a probe-less machine (e.g. Bambu P1S, has_probe=
        # False) is still clamped by z_amp_max, just for a different reason
        # -- calling it "probe keep-out" there would be a false claim about
        # hardware that isn't present.
        reason = "probe keep-out" if getattr(profile, "has_probe", True) else "z-amp ceiling"
        writer.comment(
            f"NOTE: loop height clamped to {loop_h:.2f}mm / row to {row_mm:.2f}mm "
            f"({profile.name} z_amp_max={z_cap}mm - {reason})")
    sx = cx + _radius(0.0, 0.0)
    writer.comment("move to fabric start")
    writer.safe_lift(z0 + travel_z_clearance)
    writer.travel(sx, cy, z0 + travel_z_clearance)
    writer.travel(sx, cy, z0)
    writer.unretract()

    # Solid cuff: one completely FLAT priming ring squished onto the plate
    # (the whole first layer sits level at z0 for maximum grip), then a
    # vase-mode helix climbs to the cuff top.
    writer.comment(f"anchor cuff (flat first ring + {cuff_turns} turns)")
    cuff_pts = (cuff_turns + 1) * points_per_turn
    for i in range(1, cuff_pts + 1):
        s = i / points_per_turn            # turns completed
        th = 2.0 * math.pi * (i % points_per_turn) / points_per_turn
        z = z0 + max(0.0, s - 1.0) * cuff_lh   # turn 0 flat, then helix
        r = _radius(th, z)
        speed = writer.first_layer_speed if s <= 1.0 else writer.print_speed
        writer.extrude_to(cx + r * math.cos(th), cy + r * math.sin(th), z,
                          speed=speed, layer_height_override=cuff_lh)
        if i == points_per_turn:
            writer.set_fan(fan_speed if fan_speed is not None else writer.fan_speed)

    # Fabric rows: continuous helix of stitches.
    writer.comment(f"loop fabric ({n_rows} rows x {stitches} stitches, "
                   f"{mode} stitch)")
    dtheta = 2.0 * math.pi / stitches
    two_pi = 2.0 * math.pi

    def _line_z(row: int, k: int, u: float, th: float) -> float:
        # PLANAR rows (level rings, not a helix): the bottom row hugs the
        # cuff all the way around and the top row ends level. The nozzle
        # climbs row_mm in a short seam strand between rows (the first
        # extrusion of each row connects from the previous row's end).
        lz = line0 + row * row_mm
        if wave_amp > 0.0:
            # Alternate rows wave in opposite phase so crossing wavy rows
            # meet at the nodes (zigzag weave).
            lz += wave_amp * math.sin(n_waves * th + math.pi * (row % 2))
        return lz

    def _emit(th: float, line_z: float, z: float, r_extra: float,
              flow: float | None = None) -> None:
        r = _radius(th, line_z) + r_extra
        writer.extrude_to(cx + r * math.cos(th), cy + r * math.sin(th), z,
                          speed=spec.speed_mm_s,
                          flow_override=spec.flow if flow is None else flow)

    for row in range(n_rows):
        phase = 0.5 * dtheta if (stagger and row % 2 == 1) else 0.0
        for k in range(stitches):
            theta0 = k * dtheta + phase
            if mode == "spike":
                # Rounded arch stitch: a cubic bezier that pulls up
                # diagonally HIGHER than the target, curves over the crown,
                # travels to directly above the drop point, then pushes
                # straight DOWN onto the row line to stick the landing —
                # straight legs cannot form real rounded loops on the bed.
                # Sampled in (u, z) space: u = fraction of the stitch arc,
                # z = height above the local row line.
                H = loop_h
                lean = min(max(spec.lean_deg, 0.0), 45.0)
                r_here = _radius(theta0, line0 + row * row_mm)
                stitch_len = max(dtheta * r_here, 1e-6)
                # Forward advance of the rise (leaning arch toward the print
                # direction so the strand is partially self-supporting).
                u_lean = math.tan(math.radians(lean)) * 1.45 * H / stitch_len
                u_lean = min(max(u_lean, 0.08), 0.45)
                land_u = min(max(2.2 * u_lean + 0.15, 0.30), 0.70)
                # Control points (u, z). P0 = (0, 0) is the current position
                # (the anchor) and is NOT emitted — the nozzle is already
                # there. C2 sits directly above P3 so the ending tangent is
                # vertical: the descent "pushes down to stick" the landing.
                c1u, c1z = u_lean, 1.45 * H       # pull up diagonally, higher
                c2u, c2z = land_u, 1.05 * H       # directly above the drop
                p3u = land_u                       # landing u (z = 0)
                drop_depth = (0.18 * H if row == 0 else 0.34 * H)
                                                 # first row keeps a gentler drop;
                                                 # later rows plunge deeper after the
                                                 # crown to lock into the taller loop
                # Sample the bezier at t = j/10 for j = 1..10 (apex = j=5).
                us, zs = [], []
                for j in range(1, 11):
                    t = j / 10.0
                    mt = 1.0 - t
                    b1, b2, b3 = 3.0*mt*mt*t, 3.0*mt*t*t, t*t*t
                    us.append(b1*c1u + b2*c2u + b3*p3u)   # p0u = p... = 0
                    z_base = b1*c1z + b2*c2z            # p0z = p3z = 0
                    if j > 5:
                        z_base -= drop_depth * (j - 5) / 5.0
                    zs.append(z_base)
                # Cumulative 3D path length (chord lengths in the
                # (u*stitch_len, z) 2D plane, from the P0 anchor) so coasting
                # is measured along the real curve, not by sample index.
                cum = [0.0] * 11
                px, pz = 0.0, 0.0
                for j in range(1, 11):
                    xj, zj = us[j-1] * stitch_len, zs[j-1]
                    cum[j] = cum[j-1] + math.hypot(xj - px, zj - pz)
                    px, pz = xj, zj
                apex_len = cum[5]
                for j in range(1, 11):
                    u = us[j-1]
                    z = zs[j-1]
                    th = theta0 + dtheta * u
                    lz = _line_z(row, k, u, th)
                    # Radial lean toward the next layer's path so the next
                    # row lands square on the standing tie.
                    r_next = _radius(th, lz + row_mm)
                    r_here2 = _radius(th, lz)
                    radial_extra = ((r_next - r_here2 + spec.out_mm)
                                    * min(max(z / H, 0.0), 1.0))
                    # COASTING: on the rise, stop extruding once within
                    # coast_mm of the apex (residual pressure forms the
                    # crown); j=1 always lays material so the base sticks.
                    if j <= 5:
                        seg_flow = 0.0 if (j != 1 and
                                           apex_len - cum[j] < spec.coast_mm) \
                            else None
                    else:
                        seg_flow = None
                    _emit(th, lz, lz + z, radial_extra, flow=seg_flow)
                    if j == 5:
                        # -- crown pause: the standing strand hardens as a
                        # tie. Optional retract kills melt pressure during
                        # the pause; re-prime before descending.
                        if spec.tip_retract_mm > 0.0:
                            writer.retract(spec.tip_retract_mm)
                        if spec.dwell_ms > 0:
                            writer.dwell(spec.dwell_ms)
                        if spec.tip_retract_mm > 0.0:
                            writer.unretract(spec.tip_retract_mm)
                # -- run along the row line to the next stitch
                for j in range(1, 4):
                    u = land_u + (1.0 - land_u) * j / 3.0
                    th = theta0 + dtheta * u
                    lz = _line_z(row, k, u, th)
                    _emit(th, lz, lz, 0.0)
            else:
                for j in range(1, _SEGS_PER_STITCH + 1):
                    u = j / _SEGS_PER_STITCH
                    bell = (1.0 - math.cos(two_pi * u)) / 2.0     # 0..1..0
                    th = theta0 + dtheta * (u - _LOOP_Q * math.sin(two_pi * u))
                    lz = _line_z(row, k, u, th)
                    _emit(th, lz, lz - loop_h * bell, spec.out_mm * bell)
        writer.comment(f"row {row + 1}/{n_rows}")

    # Closing rim ("bind-off"): plain rings that finish the top edge cleanly.
    # Spike mode: the first ring runs one row-pitch above the last line, so it
    # welds the standing spike tips exactly like another row would; a second
    # ring stacks on it for a solid edge. Dip mode: rings stack directly on
    # the last row line.
    last_line = line0 + (n_rows - 1) * row_mm
    rim0 = last_line + (row_mm if mode == "spike" else strand_h)
    writer.comment("closing rim (2 turns)")
    for turn in range(2):
        rz = rim0 + turn * strand_h
        for i in range(1, points_per_turn + 1):
            th = two_pi * i / points_per_turn
            if wave_amp > 0.0 and mode == "spike" and turn == 0:
                # First rim ring follows the last row's wave so it stays in
                # contact with the (wavy) spike tips all the way around.
                rz_i = rz + wave_amp * math.sin(
                    n_waves * th + math.pi * ((n_rows - 1) % 2))
            else:
                rz_i = rz
            r = _radius(th, rz_i)
            writer.extrude_to(cx + r * math.cos(th), cy + r * math.sin(th),
                              rz_i, speed=spec.speed_mm_s,
                              layer_height_override=strand_h,
                              flow_override=spec.flow)

    writer.retract()
    writer.footer()

    segs = 13 if mode == "spike" else _SEGS_PER_STITCH
    return {
        "points": cuff_pts + n_rows * stitches * segs + 2 * points_per_turn,
        "stitch_mode": mode,
        "fabric_rows": n_rows,
        "stitches_per_row": stitches,
        "loop_count": n_rows * stitches,
        "row_mm_effective": round(row_mm, 2),
        "loop_mm_effective": round(loop_h, 2),
        "wave_amp_effective": round(wave_amp, 2),
        "z_amp_clamped": z_clamped,
        "footprint_radius_mm": round(fit_r, 2),
        "top_z_mm": round(top_z, 2),
        "filament_mm": round(writer.total_filament_mm, 1),
        "max_z_rate_mm_s": round(writer.max_z_rate, 2),
        "layer_height_clamp_events": writer.layer_height_clamp_events,
        "blob_count": 0,
        "bounds": writer.bounds,
    }
