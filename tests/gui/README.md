# GUI suite: issue #16 coverage map

Issue #16 predates this suite, so each of its acceptance bullets is mapped
below to the tests that now cover it. Run with `just test-gui` (Flatpak +
Xvfb); every module skips without a display, uses a tmp database with the
memory GSettings backend, stubs sync/watcher entry points, and never touches
a live account or the keyring. Tests marked NEW below were added by the
issue-16 audit itself; everything else already existed.

## Harness: SDK, display, isolation, no live accounts

- `just test-gui` / `.github/workflows/gui-tests.yml`: pytest runs inside the
  installed Flatpak under Xvfb; CI bounds the job at 45 minutes.
- Every `tests/gui/test_*.py` header: display skip, tmp database, stubbed
  network entry points, fakes never live accounts.

## Empty-account startup, accelerators, network changes

`test_empty_window.py`:

- `test_starts_on_the_no_account_page`
- `test_mail_actions_start_disabled`
- `test_window_actions_are_safe_without_accounts`
- `test_accelerators_are_registered`
- `test_offline_flip_shows_and_hides_the_banner`
- `test_compose_and_notification_entries_guard_without_accounts`

## Add / remove / re-add

`test_window_accounts.py`:

- `test_first_account_loads_the_mail_view_and_syncs`
- `test_adding_an_account_offline_skips_sync_until_reconnect`
- `test_second_account_appears_beside_the_first`
- `test_first_account_added_signal_builds_the_mail_view`
- `test_later_account_added_signal_syncs_the_newest`
- `test_remove_account_returns_to_the_empty_state`
- `test_readded_account_rebuilds_a_single_branch`
- `test_removing_one_of_two_keeps_the_other_open`

## Delayed callbacks (stale completions)

`test_lifecycle_gui.py`:

- `test_a_sync_past_remove_and_readd_files_nothing`
- `test_a_live_sync_still_files` (control)
- `test_a_queued_send_past_removal_recreates_no_sent_folder`
- `test_a_body_past_a_folder_replacement_lands_nowhere`
- `test_a_live_body_still_delivers` (control)
- `test_completions_past_window_close_touch_no_widgets`
- `test_removing_one_account_spares_the_others_sync`
- `test_a_composer_send_past_removal_files_no_sent_copy`
- `test_a_live_composer_send_files_its_sent_copy` (control)

## Multiwindow Outbox (NEW)

`test_outbox_windows.py`:

- `test_a_second_windows_drain_files_no_second_sent_copy`

Single-window drain behaviour was already covered by
`test_lifecycle_gui.py` (`test_a_queued_send_past_removal_recreates_no_sent_folder`)
and `test_tuta_drain.py` (both tests); only the shared-Outbox case was missing.

## Move undo / partial failure (NEW)

`test_move_undo.py`:

- `test_undo_returns_mail_to_its_source_uids`
- `test_a_completed_move_files_the_server_uids` (control)
- `test_a_partial_move_files_what_crossed_and_restores_the_tail`
- `test_a_failed_move_restores_everything`
- `test_a_move_past_a_sign_in_failure_restores_everything`

No GUI test touched the move paths before; nothing here overlaps existing tests.

## Reader disposal

`test_webkit_sentinel.py`:

- `test_release_drops_the_view_but_keeps_the_process`
- `test_release_anchor_restarts_the_web_process`
- `test_collapsed_view_defers_the_renderer`

## WebKit sentinel: scripts / remote blocked until allowed

`test_webkit_sentinel.py` (real WebKit against a loopback sentinel server):

- `test_remote_images_stay_blocked`
- `test_show_images_loads_remote_images`
- `test_scripts_never_run_and_never_download`
- `test_allowed_images_do_not_allow_scripts`

## WebKit: external schemes, renderer lifecycle

`test_webkit_sentinel.py`:

- `test_mailto_and_https_links_launch_outside_the_body`
- `test_foreign_scheme_links_launch_nothing`
- `test_release_drops_the_view_but_keeps_the_process`
- `test_release_anchor_restarts_the_web_process`

## Provider selection / consent

`test_provider_dialog.py`:

- `test_every_provider_gets_a_row_with_its_badge`
- `test_oauth_rows_carry_the_browser_mark`
- `test_gated_rows_stay_visible_but_insensitive`
- `test_a_shipped_client_id_enables_its_row`
- `test_an_installed_helper_enables_the_proton_row`
- `test_the_shipped_helper_lights_the_proton_row`
- `test_each_row_opens_its_dialog`

Explicit consent to downgrade delivery, `test_composer_delivery.py`:

- `test_tuta_send_waits_for_the_explicit_ordinary_opt_in`
- `test_secure_delivery_never_enables_send`
- `test_other_providers_see_no_delivery_choice`

## Login failure

Every onboarding dialog returns to its form on a refused login:

- `test_tuta_dialog.py::test_a_refused_login_returns_to_the_form`
- `test_proton_dialog.py::test_a_refused_login_returns_to_the_form`
- `test_microsoft_dialog.py::test_a_refused_login_returns_to_the_form`
- `test_fastmail_dialog.py::test_a_refused_login_returns_to_the_form`

Plus the never-start guards (`test_an_invalid_address_never_starts_login`,
`test_sign_in_without_a_password_never_starts_login`,
`test_sign_in_without_a_browser_never_starts_login`,
`test_a_malformed_code_never_starts_login`,
`test_an_existing_address_is_not_added_twice`) in each dialog module.

## TOTP flow / cancellation

Flow, `test_tuta_dialog.py`:

- `test_the_code_typed_on_the_form_goes_with_the_first_attempt`
- `test_a_missing_code_is_asked_for_on_the_same_form`
- `test_an_expired_code_clears_the_row_for_retry`
- `test_a_malformed_code_never_starts_login`
- `test_a_security_key_account_reports_itself_honestly`

Flow, `test_proton_dialog.py`:

- `test_an_expired_code_clears_the_row_for_retry`

Cancellation (NEW), `test_dialog_cancel_keyring.py`:

- `test_cancelling_a_login_saves_no_account`

## Keyring persistence failure (NEW)

`test_dialog_cancel_keyring.py`:

- `test_a_keyring_failure_returns_to_the_form_without_emitting`
- `test_a_keyring_failure_leaves_no_orphan_account`
  (`xfail(strict)`: the dialogs save the database row before the secret and
  never clean it up, so the failed account lingers without a credential and a
  retry refuses the address as a duplicate. Tuta, Proton, Microsoft and
  Fastmail share the shape; filed, not fixed, per the audit's boundary.)

## Failure logs, bounded hangs, assertion style

- Bounded: `timeout-minutes: 45` on the CI job; every pump helper takes a
  timeout; no test waits on the network.
- Style: state/behaviour assertions throughout; no exact-copy, wiring or
  pixel assertions (see each module docstring).
- NOT covered: uploading failure logs/screenshots is a CI-workflow concern,
  not a test concern -- `gui-tests.yml` has no artifact-upload step. Left for
  the workflow, out of this audit's `tests/gui/**` boundary.
