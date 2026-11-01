# Altium autonomy test runbook

This runbook is intentionally separate from automated tests. Nothing in the
unit suite starts, stops, focuses, or edits a real Altium instance.

## Preconditions

1. Commit or externally back up the test project.
2. Open a disposable copy in Altium Designer.
3. Install the bundled scripts and configure a dedicated Run Script hotkey for
   `StartMCPServer`; do not reuse a destructive Altium shortcut.
4. Run `app_capability_probe(probe_live=true)` and require matching script
   versions before mutations.
5. Run `app_checkpoint`, then keep its checkpoint id in the test log.

## Staged validation

1. **Observation only:** call `app_visual_context`, `app_list_dialogs`, and
   `proj_visual_cross_probe` for one known designator. Verify the BMP and that
   the viewport was not otherwise changed.
2. **Failure recovery:** introduce a deliberate compile error only in the
   disposable script copy. Verify `app_get_script_errors` reports file, line,
   symbol, and safe buttons. Remove the error manually.
3. **Restart:** call `app_restart_altium_bridge` with the dedicated hotkey.
   Poll `app_get_restart_status`; require `running` and the expected script
   version. Any license/save/update dialog must produce `blocked`.
4. **Transaction rollback:** use `app_change_transaction` in dry-run mode,
   then execute one reversible property change with a deliberately failing
   validation. Verify checkpoint restore and document reload.
5. **Library:** in a disposable SchLib, generate a two-part component with
   `lib_create_symbol_in_parts` (with `shared_pins` for supply pins); inspect both parts and shared power pins.
6. **PCB planning:** call `pcb_plan_bga_fanout` and `pcb_plan_return_vias` with
   defaults. Review geometry before repeating with `apply=true, confirm=true`.
   Run clearance, unrouted, return-path, and differential-pair audits afterward.

## Fully offline routing acceptance

Before opening Altium, exported or synthetic `generic.get_pcb_geometry` data can
be processed with `route_build_offline_package`. Prefer `routing_style="45deg"`;
the router blocks diagonal corner cutting and writes `routing-plan.json` plus a
structured `routing-plan.svg`. Do not consider the result ready unless
`acceptance.safe_to_apply` is true.

Run `pcb_audit_placement_plan` on proposed centroids/courtyards and explicit
roles (`decoupling`, `termination`, `connector`). Then run `route_audit_plan`
with high-speed net names, reference-via positions and differential-pair skew
limits. These are preflight checks only: the package intentionally retains
`requires_live_drc=true`, because offline geometry cannot replace Altium's DRC.

## Stop conditions

Stop automation immediately on an unknown modal dialog, version mismatch,
checkpoint failure, missing reference net, unexpected document focus, or any
DRC regression. Restore the checkpoint before investigating further.

## Evidence to retain

Keep the capability report, restart status JSON, before/after viewport images,
transaction operation results, validation output, DRC output, and checkpoint
id. These artifacts are the acceptance record for enabling unattended use.
