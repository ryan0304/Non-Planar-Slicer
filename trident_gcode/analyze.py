"""Analyze external G-code (e.g. from FullControl) against the Trident's limits.

Designs made elsewhere weren't generated with our Z-velocity and volumetric-flow
clamps, so before printing one it's worth checking it actually fits the machine and
doesn't ask the Z axis or hotend to do something they can't. This parses any
standard G-code and produces a pass/fail report for the Trident.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .profile import PrinterProfile, TRIDENT


@dataclass
class GcodeAnalysis:
    moves: int = 0
    extrude_moves: int = 0
    min: list = field(default_factory=lambda: [math.inf] * 3)
    max: list = field(default_factory=lambda: [-math.inf] * 3)
    max_z_rate: float = 0.0          # mm/s
    max_flow: float = 0.0            # mm^3/s
    filament_mm: float = 0.0
    est_time_s: float = 0.0          # estimated print time (seconds)
    peak_z_accel: float = 0.0        # mm/s^2  finite-difference Z-accel demand
    unsupported_moves: int = 0       # extruding moves above z=1.0 with nothing under them
    probe_hits: int = 0              # moves where printed material invades the probe keep-out
    probe_worst_mm: float = 0.0      # deepest keep-out violation (mm above the clearance)
    blob_count: int = 0              # E-only extrusion moves (heuristic blob detection)
    dwell_time_s: float = 0.0       # total G4 dwell time
    min_fan_speed: float | None = None   # 0..1, over extruding moves only; None = no M106 seen
    max_fan_speed: float | None = None   # 0..1, over extruding moves only; None = no M106 seen
    issues: list = field(default_factory=list)

    @property
    def height(self) -> float:
        return self.max[2] - self.min[2]

    @property
    def footprint(self) -> tuple[float, float]:
        return (self.max[0] - self.min[0], self.max[1] - self.min[1])

    @property
    def unsupported_pct(self) -> float:
        return 100.0 * self.unsupported_moves / self.extrude_moves if self.extrude_moves else 0.0


def analyze_gcode(path: str, profile: PrinterProfile = TRIDENT,
                  filament_diameter: float = 1.75) -> GcodeAnalysis:
    fil_area = math.pi * (filament_diameter / 2) ** 2
    a = GcodeAnalysis()
    x = y = z = e = 0.0
    curF = 0.0
    cur_fan = 0.0            # sticky M106/M107 state (0..1); fan defaults off
    fan_ever_on = False      # True once the first M106 is seen -- gates min/max
                             # tracking so the deliberate fan-off adhesion window
                             # before it (M107 in start_gcode, no M106 yet) doesn't
                             # drag min_fan_speed down to 0 regardless of what the
                             # print actually ramps between once the fan is live
    rel_e = False           # M82 absolute / M83 relative
    abs_xyz = True          # G90/G91
    have = False

    # --- Print-time / Z-accel-demand state (see model notes below) ---
    max_v = profile.max_velocity
    max_a = profile.max_accel
    max_zv = profile.max_z_velocity
    max_za = profile.max_z_accel
    prev_vz = None          # commanded Z velocity (mm/s, signed) of previous move
    prev_dt = None          # duration (s) of the previous move
    pending_retract = 0.0   # filament pulled back by E-only retracts, not yet
                            # re-pushed: an unretract merely cancels this and
                            # must not be counted as a blob deposit
    seen_xy_extrude = False  # blobs only exist mid-print; E-only pushes before
                             # the first real extrusion are priming, not blobs

    # --- Unsupported-extrusion state ---
    # Spatial hash of prior extrusion segment midpoints. Key = (round(x/2),
    # round(y/2)) -> set of quantised (qx, qy, qz) points in that 2x2mm cell. A
    # new extruding segment above z=1.0 is "unsupported" if no prior midpoint
    # lies within 1.0mm in XY (actual distance test) *and* 0.2..2.0mm below the
    # segment's own z (i.e. nothing to deposit onto). O(n): each query scans the
    # 3x3 neighbour cells only. Points are quantised (0.5mm XY, 0.1mm Z) and
    # de-duplicated per cell, so a tall single-wall print that revisits the same
    # column every turn can't grow an unbounded point list.
    CELL = 2.0
    XY_R2 = 1.0 * 1.0      # max XY distance^2 to count as supporting
    Z_LO, Z_HI = 0.2, 2.0  # supporting layer must be this far below
    grid: dict = {}

    # --- Probe keep-out state ---
    # Skip the entire probe check if the printer has no probe (e.g. Bambu Lab).
    check_probe = profile.has_probe and profile.probe_radius > 0
    # The inductive probe rides at nozzle + (probe_dx, probe_dy), with its body
    # bottom probe_clearance above the nozzle tip. Track the MAX printed height
    # per coarse cell; any earlier material inside probe_radius of the probe
    # centre that is taller than nozzle_z + clearance is a collision.
    PCELL = 4.0
    p_dx, p_dy = profile.probe_dx, profile.probe_dy
    p_clear = profile.probe_clearance
    p_rad = profile.probe_radius
    p_cells = int(math.ceil(p_rad / PCELL)) + 1
    maxz_grid: dict = {}

    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line[0] in (";", "("):
                continue
            up = line.upper()
            head = up.split(None, 1)[0]
            if head == "M83": rel_e = True; continue
            if head == "M82": rel_e = False; continue
            if head == "G91": abs_xyz = False; continue
            if head == "G90": abs_xyz = True; continue
            if head == "G92":                       # reset axis counters (usually E0)
                for tok in up.split()[1:]:
                    if tok[:1] == "E":
                        try: e = float(tok[1:])
                        except ValueError: pass
                continue
            if head == "G4":
                # Dwell: G4 P<ms> or G4 S<seconds>
                dwell_s = 0.0
                for tok in up.split()[1:]:
                    try:
                        v = float(tok[1:])
                    except ValueError:
                        continue
                    if tok[0] == "P":
                        dwell_s += v / 1000.0
                    elif tok[0] == "S":
                        dwell_s += v
                a.dwell_time_s += dwell_s
                a.est_time_s += dwell_s
                continue
            if head == "M106":                      # part-cooling fan on, S0-255
                for tok in up.split()[1:]:
                    if tok[:1] == "S":
                        try: cur_fan = min(1.0, max(0.0, float(tok[1:]) / 255.0))
                        except ValueError: pass
                fan_ever_on = True
                continue
            if head == "M107":                      # part-cooling fan off
                cur_fan = 0.0
                continue
            if head not in ("G0", "G1"):
                continue

            nx, ny, nz = x, y, z
            de = None
            for tok in up.split()[1:]:
                try:
                    v = float(tok[1:])
                except ValueError:
                    continue
                c = tok[0]
                if c == "X": nx = v if abs_xyz else x + v
                elif c == "Y": ny = v if abs_xyz else y + v
                elif c == "Z": nz = v if abs_xyz else z + v
                elif c == "F": curF = v
                elif c == "E":
                    de = v if rel_e else (v - e)
                    e = (e + v) if rel_e else v

            if have:
                dx, dy, dz = nx - x, ny - y, nz - z
                dist = math.sqrt(dx*dx + dy*dy + dz*dz)
                a.moves += 1
                extruding = de is not None and de > 1e-6
                speed = curF / 60.0
                if dist > 0 and speed > 0:
                    zr = speed * abs(dz) / dist
                    a.max_z_rate = max(a.max_z_rate, zr)
                    if extruding:
                        a.extrude_moves += 1
                        a.filament_mm += de
                        seen_xy_extrude = True
                        if fan_ever_on:
                            a.min_fan_speed = cur_fan if a.min_fan_speed is None else min(a.min_fan_speed, cur_fan)
                            a.max_fan_speed = cur_fan if a.max_fan_speed is None else max(a.max_fan_speed, cur_fan)
                        flow = de * fil_area * speed / dist
                        a.max_flow = max(a.max_flow, flow)

                    # ---- Effective speed (print-time model) --------------
                    # The commanded feedrate is clamped by everything Klipper
                    # would clamp it by:
                    #   * machine max_velocity
                    #   * short-segment accel: a segment only dist mm long can't
                    #     reach more than v <= sqrt(dist * max_accel) even under
                    #     full accel from rest (triangular profile, ignoring the
                    #     entry speed -> conservative lower bound on speed).
                    #   * Z axis: the Z *component* v*zf can't exceed max_z_velocity,
                    #     so v <= max_z_velocity / zf; and the Z component can't
                    #     accelerate faster than max_z_accel over the Z distance,
                    #     v*zf <= sqrt(|dz| * max_za)  =>  v <= sqrt(|dz|*max_za)/zf.
                    zf = abs(dz) / dist                 # Z fraction of this move
                    v = min(speed, max_v, math.sqrt(dist * max_a))
                    if zf > 1e-9:
                        v = min(v, max_zv / zf, math.sqrt(abs(dz) * max_za) / zf)
                    if v <= 0:
                        v = speed
                    dt = dist / v
                    a.est_time_s += dt

                    # ---- Z-accel demand (finite difference) --------------
                    # Signed Z velocity actually achievable this move, then the
                    # change vs the previous move over the previous move's
                    # duration: how hard the wave crest asks Z to swing. Only
                    # measured between consecutive EXTRUDING moves - travel
                    # lift-off/landing junctions are planned by Klipper's own
                    # lookahead and are not the wave-crest signal we care about.
                    if extruding:
                        vz = v * (dz / dist)            # signed
                        if prev_vz is not None and prev_dt and prev_dt > 0:
                            z_accel_demand = abs(vz - prev_vz) / prev_dt
                            a.peak_z_accel = max(a.peak_z_accel, z_accel_demand)
                        prev_vz, prev_dt = vz, dt
                    else:
                        prev_vz, prev_dt = None, None

                    # ---- Unsupported-extrusion detector ------------------
                    if extruding:
                        mx, my, mz = (x + nx) / 2, (y + ny) / 2, (z + nz) / 2
                        if mz > 1.0:
                            gx, gy = round(mx / CELL), round(my / CELL)
                            supported = False
                            for cx in (gx - 1, gx, gx + 1):
                                for cy in (gy - 1, gy, gy + 1):
                                    cell = grid.get((cx, cy))
                                    if not cell:
                                        continue
                                    for (px, py, pz) in cell:
                                        below = mz - pz
                                        if Z_LO <= below <= Z_HI:
                                            ddx, ddy = mx - px, my - py
                                            if ddx*ddx + ddy*ddy <= XY_R2:
                                                supported = True
                                                break
                                    if supported:
                                        break
                                if supported:
                                    break
                            if not supported:
                                a.unsupported_moves += 1
                        # ---- Probe keep-out check -------------------------
                        if check_probe:
                            pxc, pyc = mx + p_dx, my + p_dy
                            pgx, pgy = round(pxc / PCELL), round(pyc / PCELL)
                            limit = mz + p_clear
                            worst = 0.0
                            for cxx in range(pgx - p_cells, pgx + p_cells + 1):
                                for cyy in range(pgy - p_cells, pgy + p_cells + 1):
                                    topz = maxz_grid.get((cxx, cyy))
                                    if topz is None or topz <= limit:
                                        continue
                                    ddx = cxx * PCELL - pxc
                                    ddy = cyy * PCELL - pyc
                                    if ddx*ddx + ddy*ddy <= (p_rad + PCELL * 0.71) ** 2:
                                        worst = max(worst, topz - limit)
                            if worst > 0.05:
                                a.probe_hits += 1
                                a.probe_worst_mm = max(a.probe_worst_mm, worst)

                        # Record this segment's midpoint as future support,
                        # quantised (0.5mm XY, 0.1mm Z) and de-duplicated per
                        # cell so a tall single-wall print can't grow an
                        # unbounded point list.
                        key = (round(mx / CELL), round(my / CELL))
                        grid.setdefault(key, set()).add(
                            (round(mx * 2) / 2, round(my * 2) / 2, round(mz, 1))
                        )
                        # Coarse max-height grid for the probe check.
                        pkey = (round(mx / PCELL), round(my / PCELL))
                        if maxz_grid.get(pkey, -1.0) < mz:
                            maxz_grid[pkey] = mz

                elif dist == 0 and de is not None and de > 1e-6:
                    # E-only extrusion (blob / extrude-in-place): no XY/Z
                    # movement, just filament push. Don't compute flow (would
                    # be divide-by-zero). Estimate time from E feedrate.
                    a.extrude_moves += 1
                    a.filament_mm += de
                    # Only the push BEYOND any outstanding retract deposits
                    # material — a plain unretract is not a blob.
                    net_de = de - pending_retract
                    pending_retract = max(0.0, pending_retract - de)
                    if net_de > 0.05 and seen_xy_extrude:
                        a.blob_count += 1
                    if curF > 0:
                        dt = de / (curF / 60.0)
                        a.est_time_s += dt
                elif dist == 0 and de is not None and de < -1e-6:
                    # E-only retract: remember it so the matching unretract
                    # isn't misread as a blob deposit.
                    pending_retract += -de

                for i, val in enumerate((nx, ny, nz)):
                    a.min[i] = min(a.min[i], val)
                    a.max[i] = max(a.max[i], val)
            x, y, z = nx, ny, nz
            have = True

    _evaluate(a, profile)
    return a


def _evaluate(a: GcodeAnalysis, p: PrinterProfile) -> None:
    if a.min[0] < p.print_min_x or a.max[0] > p.print_max_x \
            or a.min[1] < p.print_min_y or a.max[1] > p.print_max_y:
        a.issues.append(
            f"Footprint X[{a.min[0]:.0f},{a.max[0]:.0f}] Y[{a.min[1]:.0f},{a.max[1]:.0f}] "
            f"falls outside the safe area X[{p.print_min_x:.0f},{p.print_max_x:.0f}] "
            f"Y[{p.print_min_y:.0f},{p.print_max_y:.0f}] - recenter or scale down."
        )
    if a.max[2] > p.z_max:
        a.issues.append(f"Top Z {a.max[2]:.1f} exceeds Z max {p.z_max}.")
    if a.max_z_rate > p.max_z_velocity + 0.1:
        a.issues.append(
            f"Peak Z-rate {a.max_z_rate:.1f} mm/s exceeds max_z_velocity "
            f"{p.max_z_velocity} - firmware will slow these moves; slope/feedrate is too steep."
        )
    if a.peak_z_accel > 1.2 * p.max_z_accel:
        a.issues.append(
            f"Peak Z-accel demand {a.peak_z_accel:.0f} mm/s^2 exceeds max_z_accel "
            f"{p.max_z_accel:.0f} - firmware will throttle wave crests; expect slower "
            f"prints and possible blobbing."
        )
    if a.extrude_moves and a.unsupported_pct > 2.0:
        a.issues.append(
            f"{a.unsupported_moves} unsupported extrusion moves ({a.unsupported_pct:.1f}%) "
            f"- printing in mid-air; add a base/body under the design."
        )
    if a.probe_hits and p.has_probe:
        a.issues.append(
            f"PROBE COLLISION RISK: {a.probe_hits} moves where printed material rises "
            f"up to {a.probe_worst_mm:.1f} mm into the probe keep-out (probe at nozzle "
            f"+({p.probe_dx:.0f},{p.probe_dy:.0f}), clearance {p.probe_clearance:.1f} mm). "
            f"Reduce z-amp / surface amplitude, or verify the real probe clearance."
        )


def _fmt_time(seconds: float) -> str:
    """Format a duration as e.g. '1h 03m 20s' / '12m 40s' / '45s' (ASCII only)."""
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def format_report(a: GcodeAnalysis, profile: PrinterProfile = TRIDENT) -> str:
    fp = a.footprint
    z_over = a.peak_z_accel > 1.2 * profile.max_z_accel
    unsup_over = a.extrude_moves and a.unsupported_pct > 2.0
    lines = [
        f"  moves           : {a.moves}  ({a.extrude_moves} extruding)",
        f"  footprint       : {fp[0]:.0f} x {fp[1]:.0f} mm  "
        f"(X {a.min[0]:.0f}-{a.max[0]:.0f}, Y {a.min[1]:.0f}-{a.max[1]:.0f})",
        f"  height (Z)      : {a.height:.1f} mm  (Z {a.min[2]:.1f}-{a.max[2]:.1f})",
        f"  filament        : {a.filament_mm/1000:.2f} m",
        f"  est. print time : {_fmt_time(a.est_time_s)}",
        f"  peak Z-rate     : {a.max_z_rate:.1f} / {profile.max_z_velocity} mm/s"
        + ("  [OVER]" if a.max_z_rate > profile.max_z_velocity + 0.1 else "  [ok]"),
        f"  peak Z-accel    : {a.peak_z_accel:.0f} / {profile.max_z_accel:.0f} mm/s^2"
        + ("  [OVER]" if z_over else "  [ok]"),
        f"  peak flow       : {a.max_flow:.1f} mm^3/s",
        f"  unsupported     : {a.unsupported_moves} moves ({a.unsupported_pct:.1f}%)"
        + ("  [HIGH]" if unsup_over else "  [ok]"),
        f"  probe keep-out  : {a.probe_hits} risky moves"
        + (f" (worst +{a.probe_worst_mm:.1f} mm)  [HIT RISK]" if a.probe_hits else "  [ok]"),
    ]
    if a.min_fan_speed is not None:
        lines.append(
            f"  fan speed       : {a.min_fan_speed:.0%} - {a.max_fan_speed:.0%}"
        )
    if a.blob_count > 0 or a.dwell_time_s > 0:
        blob_fil_m = 0.0
        if a.blob_count > 0:
            # Rough estimate: blob filament = total filament attributable to blobs
            # (we don't track per-blob E separately, so just note the count)
            blob_fil_m = 0.0  # not separately tracked
        lines.append(
            f"  blobs           : {a.blob_count}"
            + (f" (dwell {a.dwell_time_s:.1f}s)" if a.dwell_time_s > 0 else "")
        )
    if a.issues:
        lines.append(f"  {profile.name.upper()} WARNINGS:")
        lines += [f"    - {m}" for m in a.issues]
    else:
        lines.append(f"  fits the {profile.name} safely [OK]")
    return "\n".join(lines)
