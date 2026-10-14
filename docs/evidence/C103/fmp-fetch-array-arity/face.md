# fmp-fetch-array-arity — face readings (verbatim carrier lines)

Each block is copied out of the named carrier under ``runs/``; the carrier is the archive and this file is its index. Recompute a block with the ``argv=`` printed under it.

## runs/arity-ab-abc.txt

```text
source_sha256_16=3babf2dd3c62f039
argv=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python docs/evidence/C103/fmp-fetch-array-arity/probes/arity_ab.py
interpreter=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python
arity_ab_exit=0 (recorded in the body: 'arity_ab_exit=0' is the probe's own closing line)
face: repo_root=/Users/yunjinqi/Documents/new_projects/opendata
face: argv=[]
face: head_fake_params=[3]
face: worktree_fake_params=[4]
face: tracked_file_untouched=True sha256_16=fd347307fa9d8717
face: armA_head_copy {'collected': 37, 'failed': 10, 'passed': 27, 'skipped': 0, 'errors': 0, 'other': 0, 'accounted': 37, 'balanced': 1} exit=1
face: armB_worktree {'collected': 37, 'failed': 0, 'passed': 37, 'skipped': 0, 'errors': 0, 'other': 0, 'accounted': 37, 'balanced': 1} exit=0
face: denominator_matched=True
face: armA_failure_lines=['E   opendata.services.provider_model_query.ProviderModelQueryUpstreamError', 'raise ProviderModelQueryUpstreamError from None']
verdict: PASS - arm A fails 10, arm B passes 37/37
face: product_fetch_array_params=['endpoint', 'params', 'timeout', 'transport']
face: product_positional_arg_count=4
face: armC_three_param_fake[3]=TypeError(arm_c.<locals>.three_param_fake() takes 3 positional arguments but 4 were given)
face: armC_four_param_fake[4]=accepted
verdict: arm C PASS - a 3-parameter fake cannot serve the 4-argument product call
arms_a_b_exit=0 arm_c_exit=0
arity_ab_exit=0
```

