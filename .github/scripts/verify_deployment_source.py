"""Reject builds outside the reviewed council, OKX, deadline and alpha delta."""
from __future__ import annotations

import ast
import hashlib
from pathlib import Path
import re
import subprocess
import sys

PREVIOUS_SHA = "90f9f3a558bdbea0171b19a42c58e2fae7ed8e9d"

# Reviewed application delta for the same-v8.6.1 retained and deadline repairs. The
# source guard is deliberately exact so an unrelated application edit cannot
# enter a release image through a workflow-only review.
APPLICATION_PATCH = {
    '.trellis/tasks/10-06-reusable-deployment-contract/prd.md',
    '.trellis/tasks/10-06-reusable-deployment-contract/design.md',
    '.trellis/tasks/10-06-reusable-deployment-contract/task.json',
    '.trellis/spec/backend/okx-taker-parsing.md',
    '.trellis/tasks/10-06-okx-taker-parse/boundary-verification.md',
    '.trellis/tasks/10-06-okx-taker-parse/design.md',
    '.trellis/tasks/10-06-okx-taker-parse/implement.md',
    '.trellis/tasks/10-06-okx-taker-parse/prd.md',
    '.trellis/tasks/10-06-okx-taker-parse/task.json',
    '.trellis/tasks/10-06-okx-taker-parse/verification.md',
    'scripts/okx_taker.py',
    'tests/extraction/test_brain_package_extraction.py',
    'tests/ops/test_brain_packages.py',
    'tests/ops/test_factors_smart_money.py',
    'tests/trading/test_okx_taker_consistency.py',
    'tests/trading/test_okx_taker_decimal.py',
    '.trellis/spec/backend/astra-cycle-deadlines.md',
    '.trellis/spec/backend/astra-release-contract.md',
    '.trellis/spec/backend/index.md',
    '.trellis/tasks/10-05-astra-cycle-deadline/design.md',
    '.trellis/tasks/10-05-astra-cycle-deadline/implement.md',
    '.trellis/tasks/10-05-astra-cycle-deadline/prd.md',
    '.trellis/tasks/10-05-astra-cycle-deadline/task.json',
    '.trellis/tasks/10-05-astra-cycle-deadline/validation.md',
    '.trellis/tasks/10-05-astra-release-preparation/design.md',
    '.trellis/tasks/10-05-astra-release-preparation/implement.md',
    '.trellis/tasks/10-05-astra-release-preparation/prd.md',
    '.trellis/tasks/10-05-astra-release-preparation/task.json',
    '.trellis/tasks/10-05-astra-release-preparation/validation.md',
    'astra_backend/council/debate.py',
    'astra_backend/deadline.py',
    'astra_backend/file_locks.py',
    'astra_backend/exchanges/diagnostics.py',
    'astra_backend/exchanges/listing.py',
    'astra_backend/exchanges/okx.py',
    'astra_backend/llm/call.py',
    'astra_backend/llm/transport.py',
    'astra_backend/okx_client.py',
    'astra_backend/routers/system.py',
    'astra_gateway/scheduler.py',
    'astra_gateway/store.py',
    'astra_gateway/telemetry.py',
    'deploy/install.sh',
    'docs/exchange_support_matrix.md',
    'env.example',
    'scripts/README.md',
    'scripts/ai_brain_trader.py',
    'scripts/backtest_engine.py',
    'scripts/brain/dispatch.py',
    'scripts/brain/packages.py',
    'scripts/brain/prompt.py',
    'scripts/trader/signal_snapshot.py',
    'tests/llm/test_alpha_transport_contract.py',
    'scripts/factor_library.py',
    'scripts/factors/okx_quant_factors.py',
    'scripts/factors/smart_money.py',
    'scripts/market_data_service.py',
    'scripts/news_sentiment_harvester.py',
    'scripts/okx_public.py',
    'scripts/sync_full_ledger.py',
    'scripts/trader/circuit_guard.py',
    'scripts/trader/factors.py',
    'tests/core/test_council_deadline.py',
    'tests/core/test_council_debate_mode.py',
    'tests/core/test_council_manager.py',
    'tests/core/test_debate_council_verdict.py',
    'tests/core/test_exchange_listing_directory.py',
    'tests/core/test_listing_gate.py',
    'tests/core/test_okx_client.py',
    'tests/llm/test_council_manager.py',
    'tests/llm/test_llm_call_tails.py',
    'tests/llm/test_llm_deadline.py',
    'tests/llm/test_llm_transport_tails.py',
    'tests/ops/test_brain_deadline.py',
    'tests/ops/test_brain_atomic_deadline.py',
    'tests/ops/test_brain_dispatch.py',
    'tests/ops/test_cycle_deadline.py',
    'tests/ops/test_file_lock_deadline.py',
    'tests/venues/test_exchange_diagnostics_tails.py',
    'tests/venues/test_market_data_service.py',
    'tests/venues/test_market_data_service_tails.py',
    'tests/venues/test_okx_public_data.py',
    'tests/venues/test_okx_public_domains.py',
}

# Exact PR3 blobs, except packages.py's explicitly approved PR5 alpha transport
# delta (head ad4b7bd7a771107a60a175b12fe6fd20349150b0). The four other taker
# pins remain unchanged. Extending paths never authorizes arbitrary file edits.
TAKER_PATCH_SHA256 = {
    'scripts/okx_taker.py': 'bd8540601ab614d9d42f054b01e588778b1afd28af90d27a90974a6eee42b2e0',
    'scripts/brain/packages.py': '4f6e3751752d200e93c4d33506a9fd84767fe3f878cbcb744515979a8611c8d2',
    'scripts/factors/smart_money.py': '78ef086483931433f6b7c1a28d8cd624972c0bf4748028da128cf9933ae446f9',
    'scripts/factor_library.py': '7c16adb40962b23b2dd7b65a4c75dd3001ab10f5d70104afe5066bbbf45b6ae1',
    'scripts/factors/okx_quant_factors.py': 'dbedeccd639e74ac52388f1f2d77d8ddd4e21c3f90190a78e4c26b9bbfa7f770',
}

# Fixed, independently reviewed PR5 application bytes, approved for this limited
# release-contract update on 2026-10-09. Never derive approval pins from a build.
ALPHA_PATCH_SHA256 = {
    'scripts/brain/packages.py': '4f6e3751752d200e93c4d33506a9fd84767fe3f878cbcb744515979a8611c8d2',
    'scripts/brain/prompt.py': 'b9db0dc842c3c8aa2112f445e216e46d9c69274ca6a20e3dfb20217232838e32',
    'scripts/trader/signal_snapshot.py': 'd67333373bf0d3e87d6b0ac9d12d666b7c65e23ff3ef26c22e2bbbce5a1f76ec',
    'tests/llm/test_alpha_transport_contract.py': '9ab014e4dd29dcd06443b73af267511d967ebba68e20425e9a7fc1e0240dcf04',
}

# Independently reviewed names include all 62 original cycle-deadline regressions.
DEADLINE_REGRESSIONS = {
    'tests/core/test_council_deadline.py': {
        'test_insufficient_cio_time_never_starts_request',
        'test_normal_modes_return_same_fresh_output_and_cio_trace',
        'test_retry_with_insufficient_backoff_and_reasoning_time_is_not_started',
        'test_seat_524_retry_spends_only_the_same_seat_deadline',
        'test_seat_deadline_exhaustion_does_not_retry_or_return_success',
        'test_stage_wait_is_bounded_and_workers_inherit_parent_context',
    },
    'tests/llm/test_llm_deadline.py': {
        'test_known_response_identity_is_recorded_before_body_read_can_block',
        'test_known_error_identity_is_recorded_before_error_body_can_block',
        'test_524_at_deadline_cannot_start_fallback',
        'test_524_retry_uses_remaining_after_backoff',
        'test_adaptive_400_cannot_renew_expired_budget',
        'test_adaptive_400_is_two_attempts_with_shared_budget',
        'test_body_trickle_expires_total_budget_and_closes_response',
        'test_cancelled_connection_retries_inside_remaining_budget',
        'test_hard_error_fallback_uses_remaining_and_actual_model',
        'test_missing_header_does_not_use_response_body_id',
        'test_nested_seat_deadline_is_not_renewed_by_new_call',
        'test_normal_success_trace_matches_safe_attempt',
        'test_parent_expiry_during_short_candidate_does_not_start_next',
        'test_response_arriving_after_deadline_is_rejected',
        'test_retry_backoff_counts_against_total_and_stops_before_next_attempt',
        'test_short_candidate_expiry_allows_next_inside_original_parent_budget',
        'test_telemetry_failure_does_not_retry_successful_model',
        'test_timeout_consuming_budget_has_no_retry_or_fallback',
    },
    'tests/ops/test_brain_deadline.py': {
        'test_slow_telemetry_does_not_mark_expired_cycle_healthy_or_return_fresh_success',
        'test_cache_lock_expiry_does_not_publish_decisions_or_position_instructions',
        'test_cache_write_expiry_does_not_publish_later_position_or_history_files',
        'test_active_cycle_lock_records_skip_before_reading_runtime_or_starting_request',
        'test_actual_fallback_request_metadata_round_trips_without_secrets',
        'test_collection_is_capped_even_when_unscheduled_cycle_has_more_time',
        'test_collection_wait_does_not_join_overdue_worker_or_accept_partial_packages',
        'test_council_cio_identity_is_recorded_with_token_usage_still_unknown',
        'test_cycle_reentry_is_rejected_before_collection_inference_or_success',
        'test_exhausted_council_does_not_start_fallback_or_publish_fresh_history',
        'test_expired_collection_does_not_start_new_dependency',
        'test_fallback_spends_only_remaining_whole_cycle_budget',
        'test_late_single_model_result_never_becomes_fresh_success',
        'test_legacy_http_path_uses_correlated_transport_without_fabricating_server_id',
        'test_model_arriving_inside_persistence_reserve_is_rejected',
        'test_model_scope_reserves_last_minute_and_restores_it_for_persistence',
        'test_pending_margin_helper_does_not_acquire_the_whole_cycle_lock',
        'test_queued_collection_worker_checks_expiry_before_starting_dependency',
        'test_too_little_time_for_fallback_fails_before_request',
        'test_whole_cycle_caps_collection_and_reserves_120_seconds_for_inference',
    },
    'tests/ops/test_cycle_deadline.py': {
        'test_concurrent_old_database_initialization_serializes_column_migration',
        'test_additive_schema_preserves_old_job_writer_and_old_rows',
        'test_aggregate_uses_actual_fallback_model_and_internal_trace_is_not_usage',
        'test_attempt_start_and_completion_upsert_one_row',
        'test_attempts_remain_behind_existing_admin_authorization',
        'test_authenticated_response_adds_bounded_attempts',
        'test_cross_slot_running_job_is_recorded_once_and_never_overlaps',
        'test_exhausted_queue_never_launches_process',
        'test_inference_scope_deducts_queue_time_and_caps_manual_calls',
        'test_invalid_or_expired_boundary_fails_closed',
        'test_invalid_trace_is_ignored_without_affecting_inference',
        'test_missed_window_is_durable_without_backfill',
        'test_nested_timeout_cannot_renew_parent',
        'test_non_trader_timeout_unchanged_and_stale_boundary_removed',
        'test_older_expired_queue_slot_is_not_lost_after_a_newer_skip',
        'test_original_slot_controls_queue_and_process_budget',
        'test_process_timeout_finishes_pending_attempt_and_does_not_fake_server_id',
        'test_recovery_closes_pending_only_preserving_completed_attempts',
        'test_retry_attempts_remain_individually_linked',
        'test_retry_backoff_does_not_sleep_or_start_a_fresh_budget',
        'test_safe_projection_drops_prompt_url_secret_and_invalid_id',
        'test_storage_admission_can_exhaust_budget_without_a_process',
        'test_storage_admission_delay_is_deducted_immediately_before_launch',
        'test_successful_job_closes_lost_completion_as_unknown_not_fake_success',
    },
    'tests/ops/test_file_lock_deadline.py': {
        'test_without_deadline_retains_blocking_and_reentrant_lock',
        'test_contended_lock_wait_expires_before_entering_or_renewing_budget',
        'test_contended_then_available_lock_enters_inside_original_deadline',
        'test_expiry_at_acquisition_unlocks_without_entering',
        'test_expired_reentry_does_not_enter_or_release_the_outer_lock',
        'test_real_cross_process_contention_times_out_then_lock_remains_usable',
        'test_real_same_process_thread_contention_preserves_absolute_deadline',
    },
    'tests/ops/test_brain_atomic_deadline.py': {
        'test_expired_success_health_cannot_replace_a_previous_failed_audit',
        'test_fsync_expiry_preserves_existing_file_and_cleans_temporary_output',
        'test_without_deadline_keeps_real_atomic_publication_behavior',
        'test_expired_failure_health_is_durable_without_renewing_decision_budget',
    },
}


def verify(source: Path, upstream: str, revision: str) -> None:
    assert re.fullmatch(r"[0-9a-f]{40}", upstream), "Unpinned upstream source"
    assert re.fullmatch(r"[0-9a-f]{40}", revision), "Unpinned corrected source"

    def git(*args: str) -> bytes:
        return subprocess.run(["git", "-C", str(source), *args], check=True,
                              capture_output=True, timeout=45).stdout

    assert git("rev-parse", "HEAD").decode().strip() == revision, "Wrong fork source commit"
    git("merge-base", "--is-ancestor", upstream, revision)
    git("merge-base", "--is-ancestor", PREVIOUS_SHA, revision)
    changes = {path.decode() for path in git("diff", "--name-only", "-z", upstream, revision).split(b"\0") if path}
    application = {path for path in changes if not path.startswith(".github/")}
    assert application == APPLICATION_PATCH, "Missing retained patch or unreviewed application changes"
    assert not git("status", "--porcelain"), "Source changed after revision verification"
    for relative, expected in TAKER_PATCH_SHA256.items():
        path = source / relative
        assert path.is_file(), f"Required reviewed taker source missing: {relative}"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected, (
            f"Reviewed taker source changed: {relative}")
    for relative, expected in ALPHA_PATCH_SHA256.items():
        path = source / relative
        assert path.is_file(), f"Required reviewed alpha source missing: {relative}"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected, (
            f"Reviewed alpha source changed: {relative}")
    suite_path = source / "tests/ops/test_brain_dispatch.py"
    tree = ast.parse(suite_path.read_text(encoding="utf-8"))
    suites = [node for node in tree.body if isinstance(node, ast.ClassDef)
              and node.name == "CouncilCompletionTests"]
    assert len(suites) == 1, "Required council completion regression missing"
    cases = {node.name for node in suites[0].body if isinstance(node, ast.FunctionDef)
             and node.name.startswith("test_")}
    required = {
        "test_council_success_returns_the_exact_persisted_fresh_cache",
        "test_council_success_records_ok_health_and_truthful_output_without_fake_usage",
        "test_successful_council_cache_reaches_real_downstream_management_gate",
        "test_council_persistence_failure_never_returns_cache_or_marks_success",
    }
    assert required <= cases and len(cases) >= 9, "Incomplete council completion regression"
    for relative, expected in DEADLINE_REGRESSIONS.items():
        path = source / relative
        assert path.is_file(), f"Required deadline regression missing: {relative}"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        cases = {node.name for suite in tree.body if isinstance(suite, ast.ClassDef)
                 for node in suite.body if isinstance(node, ast.FunctionDef)
                 and node.name.startswith("test_")}
        assert expected and expected <= cases, f"Incomplete deadline regression: {relative}"
    print("PASS: exact corrected fork commit, upstream ancestry, reviewed delta, retained council and all reviewed deadline regressions")


if __name__ == "__main__":
    verify(Path(sys.argv[1]).resolve(), sys.argv[2], sys.argv[3])
