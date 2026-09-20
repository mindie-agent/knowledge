#!/bin/sh
# Example adapter between the community review runner and a maintainer-owned
# Grok CLI installation. The runner passes ONE bounded JSON review request on
# stdin and expects ONE JSON object (schema mindie-review/1) on stdout. The
# Grok account, model choice and credentials belong to the maintainer's own
# installation; this wrapper never reads or writes the user's global config.
set -eu
input=$(cat)  # bounded upstream: review_input_bytes, default 64 KiB
exec /usr/local/bin/grok --non-interactive --json \
  --max-output-bytes 131072 \
  --prompt-from-stdin <<<"$input"
