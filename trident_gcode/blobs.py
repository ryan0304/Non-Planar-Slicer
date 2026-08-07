"""Blob texture placement for non-planar spiral prints.

Computes which path indices should receive a blob deposit, based on
configurable spacing, alignment, and fade-in/out parameters.  Pure function
-- no G-code emission, easy to test and mirror in JS.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# Alignment modes for blob rows.
ALIGN_STAGGER = "stagger"   # alternate rows offset by half a pitch (diamond packing)
ALIGN_COLUMN = "column"     # rows aligned vertically (bead columns)
ALIGN_JITTER = "jitter"     # deterministic pseudo-random scatter (organic)

ALIGN_MODES = (ALIGN_STAGGER, ALIGN_COLUMN, ALIGN_JITTER)


@dataclass
class BlobSpec:
    """Parameters controlling blob placement and size."""
    blobs_per_turn: int = 8       # how many blobs around each turn
    turn_stride: int = 3          # place blobs every N turns (vertical spacing)
    stagger: bool = True          # legacy flag; ignored when align is set explicitly
    align: str = ALIGN_STAGGER    # stagger | column | jitter
    jitter: float = 0.5           # scatter strength, fraction of blob pitch (align=jitter)
    volume_mm3: float = 1.5       # material volume per blob
    volume_scale_start: float = 1.0  # volume multiplier at the base (height envelope)
    volume_scale_end: float = 1.0    # volume multiplier at the top
    dwell_after_ms: int = 200     # cooling pause after each blob
    fade_in: float = 0.15         # height fraction to fade in (no blobs near base)
    fade_out: float = 0.05        # height fraction to fade out at top


def _hash01(row: int, b: int) -> float:
    """Deterministic pseudo-random in [0,1) from a (row, blob) pair.

    Same formula is trivially portable to JS for the draft preview.
    """
    x = math.sin(row * 127.1 + b * 311.7) * 43758.5453
    return x - math.floor(x)


def blob_volume_at(height_frac: float, spec: BlobSpec) -> float:
    """Blob volume (mm^3) at a given height fraction, applying the envelope."""
    t = min(1.0, max(0.0, height_frac))
    scale = spec.volume_scale_start + t * (spec.volume_scale_end - spec.volume_scale_start)
    return max(0.0, spec.volume_mm3 * scale)


@dataclass
class LoopSpec:
    """Parameters for loop-fabric walls (knitted / chainmail vases).

    The wall is NOT solid: it is rows of VERTICAL loop stitches.  Each spiral
    turn climbs ``row_mm`` (several mm, not a normal layer height) and the
    strand continuously traces cursive loop-de-loops in the wall plane: the
    nozzle dips ``up_mm`` DOWN and back up per stitch, self-crossing so each
    loop hangs below the row line and hooks around the row beneath.  A solid
    ``cuff_turns``-turn band at the bottom anchors the first fabric row.
    """
    loops_per_turn: int = 40      # stitches around each turn
    turn_stride: int = 1          # (unused in fabric mode; kept for API compat)
    # Stitch motion:
    #   "dip"   — cursive loop-de-loop dipping BELOW the row line, hooking the
    #             row beneath (probe budget: full loop height).
    #   "spike" — rise ABOVE the row line, PAUSE at the peak so the strand
    #             hardens as a standing tie, descend, continue; the next row's
    #             line welds the spike tips (probe budget: up_mm - row_mm
    #             only, so probed machines can print open rows this way).
    stitch_mode: str = "dip"
    # Spike mode only: tilt each spike forward (print direction) by this many
    # degrees from vertical. A leaning strand is partially self-supporting
    # (like a printed overhang) and the tips spread over the gaps, shortening
    # the next row's unsupported bridges. 0 = straight up.
    lean_deg: float = 20.0
    # Spike mode only: stop extruding this many mm BEFORE the peak (coast).
    # Residual nozzle pressure forms the final stretch of the strand, so the
    # peak pause doesn't ooze a blob onto every tip. 0 = extrude to the top.
    coast_mm: float = 0.8
    # Spike mode only: retract this much filament during the peak pause and
    # re-prime before the descent. Stronger than coasting — physically kills
    # the melt pressure while the strand hardens. 0 = off.
    tip_retract_mm: float = 0.0
    align: str = ALIGN_STAGGER    # stagger | column | jitter (row phase offset)
    # Scatter strength when align=jitter, as a fraction of stitch pitch. This
    # said "(unused in fabric mode)" and listed only stagger|column above,
    # which was simply wrong: compute_loop_sites() below hands both fields to
    # the same compute_sites() the blobs use, so jitter has always worked
    # here. The comment was believed over the code, and the UI controls for it
    # were never built -- a working feature was unreachable for that reason
    # alone.
    jitter: float = 0.5
    row_mm: float = 2.5           # vertical rise per fabric row (spiral pitch)
    up_mm: float = 3.5            # loop height: how far each stitch dips down
    out_mm: float = 0.5           # outward lean at the loop bottom
    rejoin_mm: float = 2.0        # (teardrop mode only — STL decoration path)
    dwell_ms: int = 0             # (teardrop mode only)
    flow: float = 1.2             # strand thickness multiplier
    speed_mm_s: float = 10.0      # stitch speed (slow = cleaner loops)
    cuff_turns: int = 3           # solid anchor turns at the base
    # Row-line waviness: the row line itself undulates in Z around the
    # perimeter, phase-flipped on alternate rows so wavy rows cross/touch at
    # the nodes (zigzag-weave fabrics). Costs Z budget: 2*wave_amp + loop
    # height must fit inside the machine's z_amp_max.
    wave_amp: float = 0.0         # mm row-line wave amplitude (0 = flat rows)
    waves: int = 12               # wave repeats around the perimeter
    fade_in: float = 0.10         # (teardrop mode only)
    fade_out: float = 0.0         # (teardrop mode only)


def compute_sites(
    total_points: int,
    points_per_turn: int,
    per_turn: int,
    turn_stride: int,
    align: str,
    jitter: float,
    fade_in: float,
    fade_out: float,
) -> set[int]:
    """Generic site placement on the spiral: which path indices get a feature.

    Shared by blob deposits and hanging loops (same lattice/stagger math).
    """
    if per_turn <= 0 or points_per_turn <= 0 or total_points <= 0:
        return set()
    if align not in ALIGN_MODES:
        align = ALIGN_STAGGER

    sites: set[int] = set()
    total_turns = total_points // points_per_turn
    pitch = 1.0 / per_turn                      # angular fraction between sites
    stride = max(1, turn_stride)

    for t in range(total_turns):
        if t % stride != 0:
            continue
        row = t // stride
        for b in range(per_turn):
            angle_frac = b * pitch
            if align == ALIGN_STAGGER and row % 2 == 1:
                angle_frac += 0.5 * pitch
            elif align == ALIGN_JITTER:
                angle_frac += (_hash01(row, b) - 0.5) * jitter * pitch
                angle_frac %= 1.0
            idx = t * points_per_turn + int(angle_frac * points_per_turn)
            # Safety: skip index 0 and anything at the very end.
            if idx <= 0 or idx >= total_points - 2:
                continue
            # Fade zones: skip sites near the base and top.
            height_frac = idx / total_points
            if height_frac < fade_in:
                continue
            if height_frac > (1.0 - fade_out):
                continue
            sites.add(idx)

    return sites


def compute_blob_sites(
    total_points: int,
    points_per_turn: int,
    blob_spec: BlobSpec,
) -> set[int]:
    """Return the set of path indices where blobs should be placed."""
    align = blob_spec.align if blob_spec.align in ALIGN_MODES else (
        ALIGN_STAGGER if blob_spec.stagger else ALIGN_COLUMN)
    return compute_sites(
        total_points, points_per_turn,
        blob_spec.blobs_per_turn, blob_spec.turn_stride,
        align, blob_spec.jitter, blob_spec.fade_in, blob_spec.fade_out,
    )


def compute_loop_sites(
    total_points: int,
    points_per_turn: int,
    loop_spec: LoopSpec,
) -> set[int]:
    """Return the set of path indices where hanging loops should be made."""
    return compute_sites(
        total_points, points_per_turn,
        loop_spec.loops_per_turn, loop_spec.turn_stride,
        loop_spec.align, loop_spec.jitter, loop_spec.fade_in, loop_spec.fade_out,
    )
