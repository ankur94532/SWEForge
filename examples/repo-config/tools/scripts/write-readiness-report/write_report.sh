#!/bin/sh
# Registered shell tool. JSON arguments arrive on standard input as one compact
# object; the process starts in the current worktree with only the declared
# environment (PATH, LANG, and this tool's `env` block).
#
# `args_schema` is enforced before this script runs, so the three required
# fields are present and correctly typed. Meaning is still this script's job:
# the schema constrains the type of `report_path`, not where it points.
#
# The field extraction below is deliberately dependency-free and POSIX-only so
# the example runs anywhere. A real shell tool handling untrusted values should
# use a JSON parser rather than sed.
set -eu

payload=$(cat)

string_field() {
  printf '%s' "$payload" |
    sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p"
}

report_path=$(string_field report_path)
release_id=$(string_field release_id)

# `case` alternation is portable; sed BRE alternation is not.
case "$payload" in
  *'"ready":true'* | *'"ready": true'*) ready=true ;;
  *) ready=false ;;
esac

case "$report_path" in
  "" | /* | *..*)
    printf 'report_path must be a relative path that does not traverse\n' >&2
    exit 2
    ;;
esac

mkdir -p "$(dirname "$report_path")"
{
  printf '# Release readiness: %s\n\n' "$release_id"
  printf -- '- format: %s\n' "$REPORT_FORMAT"
  printf -- '- ready: %s\n' "$ready"
} >"$report_path"

printf 'wrote %s\n' "$report_path"
