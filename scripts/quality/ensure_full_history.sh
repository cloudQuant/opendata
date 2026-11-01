#!/usr/bin/env bash
# Make the repository's history complete before a member that judges history.
#
# Why this exists: on GitHub Actions `pull_request` runs the workspace can be
# truncated *while* `make gate` executes, even though `actions/checkout` ran with
# `fetch-depth: 0`.  Measured on one commit (job 124969115974): `after-checkout
# shallow=false head_commits=978 all_commits=1003` and `after-gate shallow=true
# head_commits=976 all_commits=1001`.  `acceptance_item_probe.py`'s AC-16|09 reads
# `git rev-parse --is-shallow-repository` and must refuse to call a shallow
# repository a full-history scan, so that cell flips on a platform side effect
# rather than on anything the acceptance item is about - and a ratchet entry for it
# would then be red in the runs where the platform did not truncate.
#
# Completing the history at the last moment before the judgement is what makes the
# measurement reproducible.  It fails closed: if the chain cannot be completed, the
# member reports that instead of judging a truncated repository.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

if [ "$(git rev-parse --is-shallow-repository)" != "true" ]; then
  printf 'history: complete (head=%s all=%s)\n' "$(git rev-list --count HEAD)" "$(git rev-list --count --all)"
  exit 0
fi

REFSPEC='+refs/heads/*:refs/remotes/origin/*'
if ! git remote get-url origin >/dev/null 2>&1; then
  echo "history: repository is shallow and has no 'origin' remote to complete it from." >&2
  echo "Remedy: work in a full clone (git clone without --depth), or fetch the history:" >&2
  echo "  git fetch --unshallow origin ${REFSPEC}" >&2
  exit 1
fi

if git fetch --unshallow --prune origin "${REFSPEC}" >/dev/null 2>&1; then
  :
elif git fetch --refetch --prune origin "${REFSPEC}" >/dev/null 2>&1; then
  :
fi

if [ "$(git rev-parse --is-shallow-repository)" = "true" ]; then
  echo "history: still shallow after unshallow and refetch; refusing to judge history faces." >&2
  exit 1
fi

printf 'history: completed before the probe (head=%s all=%s)\n' \
  "$(git rev-list --count HEAD)" "$(git rev-list --count --all)"
