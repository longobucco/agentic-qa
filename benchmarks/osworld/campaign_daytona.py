"""Daytona backend of the official361 campaign driver (benchmarks/osworld/campaign.py).

Every sandbox a driver child provisions carries the label osworld.driver_run=<OSW_DRIVER_RUN>
(env/sandbox.provision). A child killed past its grace period never deletes its sandbox, so after
every round and on exit the driver deletes the sandboxes labelled with its own run id -- and only
those: the label is re-checked client-side, so another driver's (or a manual) sandbox is never
touched. The guest image is the digest pinned in config.IMAGE; a caller's OSW_IMAGE is refused.
"""

NAME = "daytona"
LOG_SUFFIX = "_daytona"
DRIVER_RUN_LABEL = "osworld.driver_run"   # == env/sandbox.DRIVER_RUN_LABEL (not imported: config)


def protocol_env():
    return {"OSW_BACKEND": "daytona"}


def harness_knob_defaults():
    # OSW_IMAGE "" = config's pinned digest; any other value would swap the guest image.
    return {"OSW_IMAGE": "", "OSW_PROVISION_TIMEOUT": "600"}


def env_conflicts(environ):
    return []   # the image is covered by harness_knob_defaults' OSW_IMAGE entry


def _client():
    from benchmarks.osworld.env import sandbox
    return sandbox._client()


def sweep(run_id, log, client=None):
    """Delete every sandbox labelled osworld.driver_run == run_id; return how many. API errors
    are logged, never raised (this also runs on the way out, under a signal exit code)."""
    if not run_id:
        raise ValueError("empty driver run id: would match every sandbox started outside a driver")
    removed = 0
    try:
        from daytona_sdk import ListSandboxesQuery
        client = client or _client()
        # Materialize the matches before deleting anything: client.list() may be a paginated
        # cursor, and deleting mid-iteration could disturb it.
        matches = [sb for sb in client.list(ListSandboxesQuery(labels={DRIVER_RUN_LABEL: run_id}),
                                             request_timeout=60)
                   if (getattr(sb, "labels", None) or {}).get(DRIVER_RUN_LABEL) == run_id]
        for sb in matches:
            try:
                client.delete(sb)
                removed += 1
            except Exception as e:
                log(f"sandbox sweep: could not delete {getattr(sb, 'id', sb)}: {e!r}")
        log(f"sandbox sweep: deleted {removed} sandbox(es) labelled {DRIVER_RUN_LABEL}={run_id}")
    except Exception as e:
        log(f"sandbox sweep failed ({removed} deleted): {e!r}")
    return removed
