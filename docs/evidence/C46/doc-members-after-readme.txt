================================================================================
C46 最后一次四成员复算：README §十 补了档案清单（含本档案自己）之后的最终树
================================================================================
采集时间（运行前）: 2026-09-27T10:37:41+0200
branch: dev
HEAD  : d9f7bd4（C46 四个提交已落地）
python: Python 3.13.5
node  : v25.1.0
command: make brand-check secret-check ledger-check evidence-traceability

--- 与 gate-run3-doc-members.txt 的差别 ---
只有一处 markdown：README §十 的档案清单补了 gate-run3-doc-members.txt 与本档案两行，并加了一段
「为什么有两份四成员档案」。无 .py／前端／数仓改动（四个提交 d395797/ad1f5bb/2d902c8/d9f7bd4 之后工作树只有 README 在动）。
本档案自身是在这四遍跑完之后才拷进 docs/evidence/C46/ 的，所以最后一遍 evidence-traceability 之前先
git add 了整个目录——run3 里那条 #untracked 的处置相同：入库后复跑，不改判据。

$ make brand-check
python scripts/quality/check_brand.py
== brand tokens ==
ok   akshare_web
ok   akshare-web
ok   akshare_user
ok   akshare_data
ok   akshare_mysql
ok   akshare_warehouse
ok   akshare_redis
ok   akshare_backend
ok   akshare_frontend
ok   akshare_certbot
ok   akshare_network
ok   akshare_root_pass
ok   akshare_pass
== app package references ==
ok   no 'from app.' / 'import app.' leftovers
== context: upstream 'akshare' occurrences (not a failure) ==
  .env.example:112
  .env.example:113
  .env.example:114
  .env.example:115
  .pre-commit-config.yaml:5
OK: no brand residue and no rename leftovers.
exit=0

$ make secret-check
python scripts/quality/secret_scan_check.py

    ○
    │╲
    │ ○
    ○ ░
    ░    gitleaks

[90m10:38AM[0m [32mINF[0m [1m204 commits scanned.[0m
[90m10:38AM[0m [32mINF[0m [1mscanned ~66440674 bytes (66.44 MB) in 18.2s[0m
[90m10:38AM[0m [32mINF[0m [1mno leaks found[0m
device: gitleaks 8.30.1, command: gitleaks detect --source . --config .gitleaks.toml --redact
exit=0

$ make ledger-check
python scripts/quality/acceptance_ledger_check.py
census: items=130 proven=25 gap=8 unreviewed=97 ticked=25
OK: items=130 proven=25 gap=8 unreviewed=97 ticked=25 across 22 group(s) and 19 §10 row(s) reconcile.
exit=0

$ make evidence-traceability
python scripts/quality/evidence_traceability.py
# evidence traceability over docs/evidence/
  rounds        = 56
  files census  = 476 (gate logs: 80)
  dates in gate logs = header 74/80, printed by the run 6/80
  branch named in header = 68/80 (informational; a commit pins the tree, a branch does not)
  narrative  gaps = 0
  date       gaps = 0
  identity   gaps = 0
  command    gaps = 0
  exit       gaps = 0
  untracked  gaps = 0

OK: evidence archive matches the frozen baseline (0 legacy entr(ies)).
exit=0

$ make evidence-traceability   # 本档案入库之后再跑一次，证明它没有把 untracked 面弄脏
python scripts/quality/evidence_traceability.py
# evidence traceability over docs/evidence/
  rounds        = 56
  files census  = 477 (gate logs: 80)
  dates in gate logs = header 74/80, printed by the run 6/80
  branch named in header = 68/80 (informational; a commit pins the tree, a branch does not)
  narrative  gaps = 0
  date       gaps = 0
  identity   gaps = 0
  command    gaps = 0
  exit       gaps = 0
  untracked  gaps = 0

OK: evidence archive matches the frozen baseline (0 legacy entr(ies)).
exit=0
FINAL_MEMBERS_EXIT=0   # 四成员 + 入库后追溯复算全绿
