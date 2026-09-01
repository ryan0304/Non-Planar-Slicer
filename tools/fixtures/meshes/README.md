# mesh.py STL loader fixtures

`valid_tetra.stl` is the control: a tiny binary STL holding one
tetrahedron (4 triangles, bounds `(0,0,0)`-`(10,8,6)`), every coordinate
finite and ordinary. It exists so the tests can prove the non-finite
rejection is a *filter*, not a blanket refusal -- a guard that rejects
good input as well as bad is not a fix.

The rest are adversarial, one deliberate counter-example each, mirroring
`tools/fixtures/printers/hostile.cfg`'s philosophy -- every loader rule
needs a fixture that proves it actually fires. All three are the same
tetrahedron with exactly one coordinate poisoned, so the only difference
from the control is the value under test:

- `nan_binary.stl` -- triangle index 2, vertex 1, Y coordinate is a NaN
  bit pattern. `struct.unpack_from("<12f", ...)` decodes it happily and
  the old loader stored it as a vertex. It is the worst of the three
  because it is *silent*: every comparison against NaN is False, so
  `mesh_bounds`'s `min(inf, nan)` returns `inf` and the mesh reports
  perfectly sane bounds while the NaN is still sitting in `tris`,
  ready to flow through `_edge_cross`, the contour stack and into
  `GcodeWriter._check_bounds` -- which also compares, and also passes it.
- `inf_binary.stl` -- triangle index 1, vertex 2, Z coordinate is +Inf.
  Same loader path as the NaN case, three different failures downstream,
  all observed on the pre-fix loader: `mesh_bounds` reports a Z span of
  `inf`; `_edge_cross` divides `(h - inf)` by `(0 - inf)` and hands back
  `(nan, nan)`, so an Inf upload degenerates into the NaN case one step
  later; and `_slice_many`'s `int(math.floor((zmax - h0) / step))` raises
  an uncaught `OverflowError: cannot convert float infinity to integer`.
- `inf_ascii.stl` -- triangle index 1, vertex 2, Z coordinate is the
  literal token `1e999`. The ASCII vertex regex accepts it (its character
  class is `[\d.eE+]`, and `1e999` is made only of those), and
  `float("1e999")` is `inf` in Python -- no exception, no warning. This
  is the STL-upload form of the exact `json.loads`-accepts-`NaN`/
  `Infinity` trap CLAUDE.md documents for the JSON request boundary:
  a textual token that a permissive parser turns into a non-finite float.

All four files are committed, not generated at test time. The binary ones
were produced with `struct.pack`; regenerate them only if the fixture's
*intent* changes, and update the triangle indices quoted in
`tools/test_mesh.py` if you do.
