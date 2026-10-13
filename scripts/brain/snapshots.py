"""Current factor refresh and atomic, deadline-aware prompt publication."""
from __future__ import annotations


def update_factor_library_snapshot(*,
        WORKSPACE_DIR,
        os,
        sys):
    from astra_backend.deadline import check_deadline
    check_deadline()
    scripts_path = os.path.join(WORKSPACE_DIR, "scripts")
    if scripts_path not in sys.path:
        sys.path.append(scripts_path)
    import factor_library
    # Refresh failures are mandatory-input failures, not permission to use a
    # stale shared file. The caller consumes this exact returned snapshot.
    snapshot = factor_library.update_factor_library()
    check_deadline()
    return snapshot


def write_prompt_snapshot(*, AI_LAST_PROMPT_FILE, _build_effective_prompt_text,
                          effective_system_prompt, os, prompt, time_str):
    import tempfile
    from astra_backend.deadline import DeadlineExceeded, check_deadline, commit_scope
    check_deadline()
    tmp_prompt = None
    try:
        fd, tmp_prompt = tempfile.mkstemp(prefix=".ai-prompt-", suffix=".tmp",
                                         dir=os.path.dirname(AI_LAST_PROMPT_FILE) or ".")
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(_build_effective_prompt_text(
                effective_system_prompt=effective_system_prompt, policy_version="",
                time_str=time_str, prompt=prompt))
            output.flush()
            os.fsync(output.fileno())
        with commit_scope():
            os.replace(tmp_prompt, AI_LAST_PROMPT_FILE)
    except DeadlineExceeded:
        raise
    except Exception:
        pass
    finally:
        if tmp_prompt and os.path.exists(tmp_prompt):
            os.unlink(tmp_prompt)
