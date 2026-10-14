# C103 / fmp-fetch-array-arity — the fmp 10-face stale three-argument fake

## Fault

``tests/test_fmp_iter2_market.py`` drives every case through one shared fake transport. The product's
``fetch_array`` is called with 4 positional arguments
(``face: product_fetch_array_params=['endpoint', 'params', 'timeout', 'transport']``); the HEAD copy of
the fake accepted 3. Each case that reached the fetch raised ``TypeError`` inside the
engine, so 10 of the module's 37 collected cases went red off a single stale
signature.

## Arms, and what each one rules out

- **arm A, HEAD copy** — 37 collected, 10 failed, exit non-zero: the fault
  is in HEAD, so the repair is not chasing a bug I introduced.
- **arm B, worktree** — 37 collected, 37 passed, exit 0: the worktree fake
  takes 4 parameters.
- **arm C, counterfact** — the product call replayed against a deliberately 3-parameter fake raises
  ``TypeError(arm_c.<locals>.three_param_fake()``, and the 4-parameter fake accepts. Without this arm, arm B only shows that some
  signature works; it does not show that the fourth argument is load-bearing.
- **guard on the repair itself** — ``tracked_file_untouched=True`` measured by digest, because
  the shared fixture is a batch-guarded file.

## Recompute

```text
/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python docs/evidence/C103/fmp-fetch-array-arity/probes/arity_ab.py
```

Carrier: ``runs/arity-ab-abc.txt`` · index: ``face.md``
