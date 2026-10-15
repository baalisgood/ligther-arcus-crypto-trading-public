# Lighter–Arcus deployment

Public snapshot: https://github.com/baalisgood/ligther-arcus-crypto-trading-public

Use Linux with Bash and `flock`. Create the project-local `.venv` with the required Python runtime and dependencies before starting. The launcher uses `.venv/bin/python`; an existing runtime can be selected with `ARBITRAGE_PYTHON`. Configure credentials locally using the supplied templates. This repository contains no exchange credentials, account data, or server environment files. Keep local credentials out of Git and restrict their permissions.

Run these commands from the cloned project directory:

```bash
bash start.sh monitor
# Review accounts, positions, orders and other strategies before selecting live:
bash start.sh live
```

Omitting the mode selects `monitor`. Only an explicit `live` argument enables the launcher's live mode. Strategy settings in the supplied profiles are retained; review them before live use. Use `start.sh` for Lighter–Arcus runs. Generic direct-Python examples elsewhere in the README do not provide the launcher lock protection.

By default the shared launcher uses `run/lighter-arcus.lock`, which excludes concurrent instances from the same checkout. Multiple checkouts or an existing deployment must use the same absolute `ARBITRAGE_LOCK_FILE` path to share that protection. Create its parent directory first and use the same setting for every instance:

```bash
export ARBITRAGE_LOCK_FILE=/path/to/shared/run/lighter-arcus.lock
bash start.sh monitor
```

The lock only excludes processes that use that same lock file. Do not delete, replace, or change its inode while coordinating instances. Do not bypass `start.sh` or choose a different lock to run another instance. Review other strategies sharing exchange accounts separately.

Ctrl+C and process exit do not guarantee positions are closed: Python asyncio cancellation can bypass the intended emergency-close branch. Before stopping or switching versions, review and handle actual positions and orders, then verify their final state at the exchanges. A clean process exit is not proof of flat positions. To roll back, stop the current instance, verify positions and orders, and use the preserved prior launcher with the same shared lock. Code rollback cannot reverse fills.

This snapshot is source code, not a configured deployment. No live-account, fee, profitability, or production-readiness claim follows from offline checks.
