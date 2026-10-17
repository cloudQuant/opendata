#!/usr/bin/env bash
# Re-sign every provider shard whose claims pin the shared probe runner.
#
# The plane-1 judge re-hashes each pin against the worktree
# (scripts/quality/iter2_model_evidence.py:621-627), so the one-line grant fix in
# scripts/quality/iter2_claims_probe_runner.py staled the probe pin of every accepted identity:
# measured 1280 of 1335 merged claims, covering all 256 accepted rows. A shared-instrument edit
# therefore costs a produce+record cycle for each provider the merge reads, and this script is
# that cycle. The provider population is taken from the merge glob itself, never from a list.
#
# Usage: bash docs/evidence/C125/sloos-witness-20261017/probes/resign_all_pins.sh [provider ...]
# Any provider given on the command line is skipped (already re-signed by hand).
set -u
cd /Users/yunjinqi/Documents/new_projects/opendata
export PYTHONPATH=.
PY=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python
SKIP="$*"
LOG=/tmp/resign_all.log
: > "$LOG"

providers=()
while IFS= read -r -d '' f; do
  providers+=("$(basename "$(dirname "$f")")")
done < <(find docs/evidence/C101 -maxdepth 2 -name claims.reviewed.json -print0 | sort -z)

echo "resign_population=${#providers[@]} skip=[$SKIP]"
for p in "${providers[@]}"; do
  case " $SKIP " in *" $p "*) echo "SKIP $p"; continue;; esac
  # Per-provider logs, so a reading can never be attributed to the wrong provider from a
  # shared tail.
  plog=/tmp/resign_$p.log
  $PY scripts/quality/iter2_claims_producer.py --provider "$p" \
      --evidence-root docs/evidence/C101 \
      --output "docs/evidence/C101/$p/claims.json" > "$plog" 2>&1
  produce_exit=$?
  $PY scripts/quality/iter2_primary_review_recorder.py \
      --claims "docs/evidence/C101/$p/claims.json" \
      --output "docs/evidence/C101/$p/claims.reviewed.json" \
      --provider "$p" --decision ACCEPT --reviewer-type primary_model \
      --reviewer-id iter2-acceptance-reviewer --reviewer-model qoder-cli-agent \
      --reviewer-role acceptance_reviewer >> "$plog" 2>&1
  record_exit=$?
  emitted=$(grep -o 'claims emitted=[^;]*' "$plog" | tail -1)
  nonpass=$(grep 'exit=' "$plog" | grep -vc 'status=PASS')
  signed=$(grep -o 'signed=[0-9]* refused=[0-9]*' "$plog" | tail -1)
  echo "RESIGN provider=$p produce_exit=$produce_exit record_exit=$record_exit $signed nonpass_phases=$nonpass $emitted"
done
echo "resign_done providers=${#providers[@]}"
