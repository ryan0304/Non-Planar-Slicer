#!/usr/bin/env python3
"""Generate a couple of sample STL meshes into examples/ for testing mesh import.

    python tools/make_sample_meshes.py

Produces a cylinder and a 5-point star prism — handy for trying
`python generate.py --stl examples/star_prism.stl ...` without hunting for a model.
"""
from __future__ import annotations

import math
import os
import struct


def write_binary_stl(path: str, tris) -> None:
    with open(path, "wb") as f:
        f.write(b"\x00" * 80)
        f.write(struct.pack("<I", len(tris)))
        for v0, v1, v2 in tris:
            f.write(struct.pack("<12fH", 0, 0, 0, *v0, *v1, *v2, 0))


def prism(profile, height, zbot=0.0):
    """Extrude a closed 2D profile into a solid prism with flat caps."""
    n = len(profile)
    tris = []
    for i in range(n):
        x0, y0 = profile[i]
        x1, y1 = profile[(i + 1) % n]
        a = (x0, y0, zbot); b = (x1, y1, zbot)
        c = (x1, y1, zbot + height); d = (x0, y0, zbot + height)
        tris += [(a, b, c), (a, c, d)]
    for i in range(1, n - 1):           # fan-triangulate top & bottom caps
        p0, pi, pj = profile[0], profile[i], profile[i + 1]
        tris.append(((p0[0], p0[1], zbot), (pj[0], pj[1], zbot), (pi[0], pi[1], zbot)))
        tris.append(((p0[0], p0[1], zbot + height),
                     (pi[0], pi[1], zbot + height), (pj[0], pj[1], zbot + height)))
    return tris


def main() -> int:
    os.makedirs("examples", exist_ok=True)

    cyl = [(25 * math.cos(2 * math.pi * k / 64), 25 * math.sin(2 * math.pi * k / 64))
           for k in range(64)]
    write_binary_stl("examples/cylinder.stl", prism(cyl, 40))

    star = []
    for k in range(10):
        t = 2 * math.pi * k / 10
        r = 28 if k % 2 == 0 else 14
        star.append((r * math.cos(t), r * math.sin(t)))
    write_binary_stl("examples/star_prism.stl", prism(star, 45))

    print("Wrote examples/cylinder.stl and examples/star_prism.stl")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
