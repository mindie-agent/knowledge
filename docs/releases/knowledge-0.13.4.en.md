# mindie-knowledge 0.13.4

[中文](knowledge-0.13.4.md) · **English**

The side-by-side component runtime builder now uses the installer's default system certificate policy for uv downloads behind enterprise TLS proxies. Explicit `UV_SYSTEM_CERTS`, `UV_NATIVE_TLS`, CA paths, and proxy settings retain precedence.

Failed builds still propagate their errors and leave the candidate unpublished. Release selection, idle activation, knowledge session authorization, and contribution settings retain their existing behavior.

Validation: 61 focused component-build, formal-release, ETag-cache, and release-reader tests passed. A real Release wheel probe on the enterprise network failed with `UnknownIssuer` under uv's default trust store and succeeded with system certificates. The formal release workflow runs the complete component contracts and this version's benchmarks and publishes their results as release assets.
