# MCP Git Safety Fix

Implemented on 2026-08-06.

## Fixed

- Fetches the selected remote branch before deploy and compares local/remote ancestry.
- Fast-forwards clean behind-only checkouts.
- Blocks dirty stale, local-ahead, and diverged checkouts without discarding changes.
- Locks local and remote baseline SHAs and verifies them again immediately before commit.
- Verifies the new commit parent matches the locked baseline before push.
- Fingerprints the scoped working-tree diff so a stale resumable job cannot absorb a newer change.
- Prevents automatic rollback when the job no longer exclusively owns local and remote HEAD.
- Stops automatic source reverts for ordinary module upgrade/status/registry failures; rollback remains limited to a pushed manifest/source failure.

## Verification

`pytest -q` result: **471 passed, 2 skipped, 24 deselected**.
