# mindie-knowledge 0.13.17: Authoritative State Contract-Violation Observability & Diagnostics 2.0

[中文](knowledge-0.13.17.md)

- **Authoritative SQLite Contract-Violation Observability (`owned_state.py` + `loop/dfx.py`)**: Emits structured `owned_sqlite_authority` contract-violation diagnostics (`reason_code` + owned `source_root`, zero local paths or free-text messages) whenever `open_database` or `require_columns` fails closed on symlinked paths, missing databases with preserved WAL/SHM/init records, 0-byte files, or missing required schema columns.
- **Service Crash Attribution & Diagnostics 2.0 Pin**: Upgrades `mindie-diagnostics` to formal release `v0.5.11` (`171623584aa812677df907518dcb55b06493241b`) and forwards `source_root`, `reason_code`, `substage`, and `contract_name` in `loop/dfx.py` for centralized filing and commit-exact source permalinks.
