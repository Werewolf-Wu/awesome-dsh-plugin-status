#!/usr/bin/env bash
# Publish the generated snapshot as a single parentless commit.
#
# Usage: bash scripts/publish.sh <expected-remote-main-sha>
#
# The lease value is the commit SHA that was checked out at the start of the
# run. If main moved in the meantime the push is rejected and this script
# fails without overwriting the concurrent update; the next run picks up the
# new main and regenerates.
set -euo pipefail

BASE_SHA=${1:?usage: bash scripts/publish.sh <expected-remote-main-sha>}

if ! [[ $BASE_SHA =~ ^[0-9a-f]{40}$ ]]; then
  echo "publish.sh: expected a 40 character commit SHA, got '$BASE_SHA'" >&2
  exit 1
fi

# Stage only the managed generated paths; scripts and workflows already in the
# index stay untouched.
git add -A -- README.md README.zh.md LICENSE upstream/README.zh.md catalog/
tree=$(git write-tree)

new_commit=$(git -c user.name='github-actions[bot]' \
  -c user.email='41898282+github-actions[bot]@users.noreply.github.com' \
  commit-tree "$tree" -m "Daily plugin status snapshot")

# commit-tree is only ever called without -p, but assert the result anyway so
# that a future edit can never turn history back into an accumulator.
if [ "$(git rev-list --parents -n 1 "$new_commit" | wc -w)" -ne 1 ]; then
  echo "publish.sh: refusing to push $new_commit: it has parent commits" >&2
  exit 1
fi

git push --force-with-lease="refs/heads/main:$BASE_SHA" origin "$new_commit:refs/heads/main"
