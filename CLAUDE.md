# CLAUDE.md

Everything this repo emits is fed to a physical 3D printer. A wrong number is
broken hardware, not a bad render. When a change trades safety for convenience,
take the safe side and say so.

## Invariants that will bite you

**Extrusion is relative.** `GcodeWriter` emits an `E` delta per move. Any start
G-code containing `M82` (absolute extrusion) makes the printer read each small
delta as an absolute target and over-extrude from the first move. The stock
start G-code shipped by most vendors uses `M82` — `printer_validate` treats it
as a hard error for exactly this reason. Never "fix" that by relaxing the check.

**`z_amp_max` has two consumers.** It caps the non-planar wave amplitude *and*
the loop-fabric row height / stitch dip. Both must derive from it. On the
Trident it is 0.95 mm, revised down empirically after a probe strike — it is a
measured constant, not a placeholder to tidy.

**No machine limit may be a module constant.** Ceilings come from the selected
`PrinterProfile`. A hardcoded `AMP_MAX = 0.95` once capped a Bambu at 0.95
against its own 4.0 *and* allowed 0.95 on a printer declaring 0.40. If you find
yourself typing a millimetre figure into `serve.py`, it belongs on the profile.

**Server clamps must be at least as strict as UI clamps.** `serve.py`'s
docstring promises a buggy or malicious client can never request an over-limit
print. Keep that true — validate on the server even when the browser already
did.

**Non-finite floats defeat guards rather than tripping them.** Every comparison
against `NaN` is `False`, so it survives `min()`/`max()` clamping and passes
`GcodeWriter._check_bounds` silently. `json.loads` accepts the bare tokens
`NaN`/`Infinity`. Reject non-finite values at the boundary; never clamp them.

**Presence checks must read commands, not comments.** A start-G-code block whose
only mention of homing was `; see PRINT_START` once satisfied the "no `G28`"
check, disabling the single test most likely to save a bed. Strip comments
before testing for a command.

**Missing config values become conservative defaults, never another machine's.**
An unknown `max_z_velocity` is 10 mm/s, not the Trident's 25. Silently
inheriting a different printer's limits is how a gantry gets wrecked.

## Conventions that differ from defaults

- **Python: standard library only.** No pip installs for generation. Adding a
  dependency is a design decision, not an implementation detail.
- **`viewer/` is vanilla JS with no build step.** No bundler, no framework, no
  new runtime dependencies.
- **ASCII-only strings** in source, console output, and G-code comments. Nothing
  enforces this mechanically.
- **Reserved CSS tokens** (see the comments at the top of `viewer/style.css`):
  `--accent-purple` belongs to the Point Edit Modifiers subsystem alone, and
  `--ok` / `--warn` / `--danger` are only for safety states. Using either
  elsewhere breaks a deliberate visual language.

## Testing

No test runner config exists; run the scripts directly.

```bash
python tools/check_regression.py      # byte-compares output against regression_ref/
python tools/test_printer_import.py   # printer config parser + validator
```

`check_regression.py` must stay **byte-identical**. Every reference file is
Trident-generated, so a diff means a change altered real machine output — treat
that as a bug to explain, not a baseline to regenerate. If the change is
genuinely intended, say so explicitly and regenerate deliberately.

`tools/fixtures/printers/hostile.cfg` is adversarial on purpose (absurd limits,
`M502`, no `G28`, unresolved placeholders). When you add a validator rule, add
its counter-example there.

## Working here

- Prefer fixing the cause over widening a limit. If a design won't fit, the
  answer is usually the design, not the ceiling.
- Add a regression test for every safety hole found — several in this codebase
  were reintroduced once already.
- Print-tested claims need a print. Published specs, conservative defaults, and
  derived clearances are all guesses until someone runs the machine; label them
  that way.
