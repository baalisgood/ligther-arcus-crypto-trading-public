# Lighter–Arcus deployment

Public snapshot: https://github.com/baalisgood/ligther-arcus-crypto-trading-public

Use Linux with Bash and `flock`. Create the project-local `.venv` with the required Python runtime and dependencies before starting. The launcher uses `.venv/bin/python`; an existing runtime can be selected with `ARBITRAGE_PYTHON`. Configure credentials locally using the root `env.template`. This repository contains no exchange credentials, account data, or server environment files. Keep local credentials out of Git and restrict their permissions.

From the cloned project directory, create the fixed root credentials file without overwriting an existing one:

```bash
cp -n env.template .env
chmod 600 .env
```

Edit `.env` locally and fill all seven fields: `LIGHTER_API_KEY_PRIVATE_KEY`, `LIGHTER_ACCOUNT_INDEX`, `LIGHTER_API_KEY_INDEX`, `ARCUS_API_KEY`, `ARCUS_API_SIGNING_KEY`, `ARCUS_WALLET_ADDRESS`, `ARCUS_ACCOUNT_INDEX`. Zero is a valid index; Arcus account indices are 0–9. Use a dedicated Arcus Ed25519 API signing seed, never a wallet private key. Missing/empty fields and invalid indices reject initialization. Do not commit or upload `.env`.

Lighter and Arcus credentials are read only from this project's root `.env`, regardless of the working directory. Shell environment variables and legacy authentication YAML do not override or fill missing values. Public runtime settings ship in `config/exchanges/lighter_runtime.yaml` and `config/exchanges/arcus_runtime.yaml`; no private exchange YAML is required.

After installing dependencies and completing the credentials file, run:

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
