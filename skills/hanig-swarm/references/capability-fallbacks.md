# Capability fallbacks

Loading the skill proves only loader compatibility. Check each capability when <!-- declaration: placement.reference-elaboration -->
it is needed and report the exact missing command, service, account, or path. <!-- declaration: placement.reference-elaboration -->

| capability | if unavailable | bounded fallback |
|---|---|---|
| shell/filesystem | no usable shell or denied path | report the blocked command/path and request host-approved access; never fabricate output | <!-- declaration: capability.shell-filesystem -->
| Python 3 and Git | helper or worktree evidence cannot run | install or expose host-approved programs, preserve the repository cwd, then rerun | <!-- declaration: capability.python-git -->
| Slurm scheduler | submission/accounting cannot be queried | leave scheduler work blocked and retain the declared plan; never infer a run result | <!-- declaration: capability.slurm -->
| coding CLIs and agent bus | no `lor`, `claude`, `codex` or `pi` binary, or no bus | dispatch through a backend that is present; with none, do bounded local work without delegation, or ask an operator to install one; never create look-alike paths or a daemon | <!-- declaration: capability.paseo-bus -->
| reviewer providers | configuration or coordinator credentials are absent | retain evidence and mark the change unreviewed; never borrow credentials or substitute worker self-confidence | <!-- declaration: capability.review -->
| tracker connector | no authorized connector/account | retain the idempotent outbox intent as pending synchronization for an authorized session |

Python and Git are baseline prerequisites for workflows that name them. The
coding CLIs, the bus, reviewer access, and tracker connectors are optional
capabilities with different fallbacks. Paseo is retired; it services nothing
but attempts already recorded on its route. None is installed or configured by skill installation.
Provider authentication used to start a worker is not authority to dispatch a
different backend or call another external service.
