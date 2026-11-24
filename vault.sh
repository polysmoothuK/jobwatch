#!/usr/bin/env bash
# Encrypts / decrypts the private part of JobWatch (settings, source list, robot memory).
# The key comes from the JOBWATCH_KEY environment variable (a GitHub secret on the server).
#   ./vault.sh unlock   -> vault.enc  => config.json, sources/, data/
#   ./vault.sh lock     -> config.json, sources/, data/ => vault.enc
set -euo pipefail
cd "$(dirname "$0")"
: "${JOBWATCH_KEY:?JOBWATCH_KEY is not set}"
OPTS=(-aes-256-cbc -pbkdf2 -iter 200000 -md sha256 -pass env:JOBWATCH_KEY)
case "${1:-}" in
  unlock) openssl enc -d "${OPTS[@]}" -in vault.enc | tar xzf - ;;
  lock)   tar czf - config.json sources data | openssl enc -salt "${OPTS[@]}" -out vault.enc ;;
  *) echo "usage: $0 lock|unlock" >&2; exit 2 ;;
esac
