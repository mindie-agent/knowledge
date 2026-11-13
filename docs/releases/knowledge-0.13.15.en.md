# mindie-knowledge 0.13.15: preserve enterprise network configuration

[中文](knowledge-0.13.15.md)

Knowledge feed, community Git and publication validation now append their own Git options after caller configuration instead of resetting `GIT_CONFIG_COUNT` and losing proxy or CA/TLS settings. Windows `core.longpaths=true` remains process scoped; user and global Git configuration are untouched.

Background contexts use diagnostics' shared transport projection for Git HTTP proxy, CA and TLS options, excluding credential helpers and authentication headers. Applying a new context removes previously managed network variables so revoked proxy settings take effect. One-time credentials retain the existing encrypted, single-consumption protocol.

Pins formal diagnostics 0.5.10 with Windows extended-path/SQLite URI fixes, viable retention budgets, short generated diagnostic runtimes and live degraded-worker upgrades. Consent, leases, queues and uncertain external-write receipts are retained without replaying publication.

Validation covers real Git configuration, service process boundaries, proxy removal and long paths. The formal workflow delivers exact-source benchmarks, release assets and old-client upgrade evidence. The reported TCP timeout still requires investigation on the affected enterprise network.
