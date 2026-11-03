# Debrief contract v1

Exact keys; reject duplicates, unknown fields/enums, wrong types/versions. Integers exclude booleans. Strings: Unicode scalars; nonempty excludes blank. Never normalize/coerce; patterns match exactly. <!-- declaration: purpose.scope -->

## Catalog: `debrief_catalog.v1`

- Top level: `schema_version` = `debrief_catalog.v1`, `version` = integer 1, `entries` = nonempty list.
- Entry: `id` (unique `DB` plus three digits), `skill` (exact existing bundle name under `skills/`, with `SKILL.md`), `what_broke`, `mechanism`, `proposed_change` (nonempty strings), `measures` (nonempty distinct pair list).
- Pair: `measure` (`[a-z][a-z0-9_]*`), `population` (`units`, `attempts`, `evidence_documents`, `helper_scripts`, `state_entries`).

Whole tuples cannot mix. Mechanisms describe observations. <!-- declaration: purpose.scope -->

## Packet: `debrief_packet.v1`

Field grammar: <!-- declaration: purpose.scope -->

```text
packet = {schema_version: "debrief_packet.v1", catalog_version: [catalog integer],
 packet_id: [random content-independent UUID4, lowercase hyphenated RFC 4122],
 project: "project A", learnings: [1..50 learning objects]}
learning = {id: [sequential L01..L50 from L01], catalog_entry: [catalog id],
 skill, what_broke, mechanism, proposed_change: [entry's byte-for-byte strings],
 mechanism_basis: [reported | inferred | reproduced], counts: [zero or more counts],
 evidence_refs: [nonempty E001..E999 list]}
count = {measure, population: [permitted catalog pair], scope: "inspected_set",
 numerator, denominator: [type(v) is int; 0 <= numerator <= denominator <= 1000000;
 denominator >= 1], cohort_ref: [C001..C999]}
```

Each measure occurs at most once per learning. Never add/substitute populations. E/C refs resolve privately during compilation. <!-- declaration: purpose.scope -->

Public construction uses only fixed keys, catalog literals, generated ids, enums and verified integers; no source strings/deny-list/scanner. <!-- declaration: purpose.scope -->

## Issue grammar and approval

Repository `hanig/brood-lord`; label `debrief`; renderer version 1 (int). Title = `"hanig-debrief: " + packet_id`. Preamble:

Evidence references are held privately by the source project's owner.

Body: `PREAMBLE + "\n\n\x60\x60\x60json\n" + json.dumps(packet, sort_keys=True, indent=2, ensure_ascii=False) + "\n\x60\x60\x60"`; `\x60` is a backtick. One fenced `json` block; nothing follows (no final newline).

Approval digest: SHA-256 hex of UTF-8 canonical compact JSON (`sort_keys=True`, `separators=(",", ":")`, `ensure_ascii=False`) of exactly `{"packet": packet, "title": title, "repository": "hanig/brood-lord", "label": "debrief", "renderer_version": 1}` Owner sees the scrubbed packet/title and approves this digest. Changes void approval; basis never upgrades. Never pass the dossier to the filer. <!-- declaration: purpose.scope -->

## Filing state machine

Persist digest/identity and `POST_IN_FLIGHT` before POST; freeze content. Transitions:

| From | To | Condition |
| --- | --- | --- |
| `DRAFT` | `APPROVED` | Owner approves displayed packet digest |
| `APPROVED` | `DRAFT` | Withdrawn/changed before POST |
| `APPROVED` | `POST_IN_FLIGHT` | Revalidate approval, persist intent |
| `POST_IN_FLIGHT` | `DELIVERY_UNCERTAIN` | Unknown/interrupted delivery |
| `POST_IN_FLIGHT`, `DELIVERY_UNCERTAIN` | `READBACK_FAILED` | Known issue, readback unavailable |
| `POST_IN_FLIGHT`, `DELIVERY_UNCERTAIN`, `READBACK_FAILED` | `FILED` | One exact repository/title/label/canonical-body readback |
| `POST_IN_FLIGHT`, `DELIVERY_UNCERTAIN`, `READBACK_FAILED` | `CONFLICT` | Mismatch/multiple identities |

`FILED`/`CONFLICT` are terminal. `DELIVERY_UNCERTAIN`, `READBACK_FAILED`, `CONFLICT` fail closed: no repost. Crashes are uncertain; empty search proves no nondelivery. Recovery retains records/reconciles read-only; uncertainty stays stopped. Each listed source allows that transition. <!-- declaration: purpose.scope -->

## Private dossier: debrief_dossier.v1

Private grammar: <!-- declaration: purpose.scope -->

```text
dossier = {schema_version: "debrief_dossier.v1", evidence, cohorts, candidates,
 omissions, provenance}
evidence = {E001..E999: {source_root: [label], locator: [relative locator],
 sha256: [64 lowercase hex file digest], location: [line range or JSON pointer],
 unit_id, attempt_id: [nonempty where applicable, otherwise null],
 adapter: {name, version: [nonempty strings]}, status: [ok|skipped|unsupported|unstable]}}
location = {line_range: [start, end]} | {json_pointer: [RFC 6901 string]}
cohorts = {C001..C999: {population: [population enum], members, qualifying: [sorted distinct string ids]}}
candidates = [{catalog_entry: [id or null], status: [selected|covered|unrepresentable|rejected],
 basis: [mechanism basis enum], rationale: [nonempty private string], evidence_refs: [E-refs], counts: [packet count objects],
 reproduction, coverage_disposition: [covered|extends|conflicts|not_found_in_snapshot|unknown]}]
reproduction = null | {receipt_ref: [E-record citing reproduction receipt],
 tested_version: [brood-lord commit or local modification tested], probe, observed: [nonempty strings]}
omissions = [{source_root, locator, reason: [nonempty strings]}]
provenance = {[skill name]: [readable provenance marker contents]}
```

Line ranges: inclusive integers, end >= start >= 1. Locators are relative without parent traversal. Omissions include every skipped/over-budget input; `.` identifies uninspected remainder.

Counts require matching population, qualifying subset of members, `len(qualifying) == numerator`, `len(members) == denominator`. <!-- declaration: purpose.scope -->

Reproduction is null unless basis is reproduced, when complete fields are required. Signature matching never establishes reproduction. Reported: source assertion. Inferred: classified evidence with private rationale. <!-- declaration: purpose.scope -->

## Triage: debrief_triage.v1

At debrief/triage/ + packet_id + .json. Keys: schema_version = debrief_triage.v1, packet_id (packet UUID), packet_digest (SHA-256 of compact canonical packet JSON, UTF-8), issue (exactly repository = hanig/brood-lord, number = positive integer), dispositions (object keyed by learning id, exactly every packet learning). Each disposition:

- Accept keys: disposition = accept, unit_refs (nonempty list of nonempty unit ids).
- Reject keys: disposition = reject, reason_code (already_addressed, local_modification, unsupported_mechanism, insufficient_evidence, out_of_scope), rationale (nonempty), references (public brood-lord repository paths or HTTPS links under https://github.com/hanig/brood-lord/, including issues, PRs and commits; nonempty required only for already_addressed). <!-- declaration: purpose.scope -->

Offline validation checks mechanics; the orchestrator judges. Triage never marks coverage. <!-- declaration: purpose.scope -->

## Coverage: debrief_coverage.v1

Keys: schema_version = debrief_coverage.v1, version = integer 1 (registry format revision), entries (list, initially empty). Entry keys: id (unique CV001 upward, minimum three-digit padding without extra leading zeros), catalog_entry (existing id), applicability, references (nonempty list), note (nonempty). Applicability: exactly {"fixed_in": SHA} (full lowercase 40- or 64-hex commit SHA) or {"always": true}. <!-- declaration: purpose.scope -->

References: public https://github.com/hanig/brood-lord/pull/N (N is a positive integer; optional slash-separated [A-Za-z0-9_-]+ route, query and fragment; no whitespace/backslash) or repository paths (Unicode, spaces allowed; no NUL, absolute path or empty/./.. component). An always mapping is unconditional. A fixed_in mapping uses Git ancestry against a local checkout, never commit ordering. <!-- declaration: purpose.scope -->

## Budgets and constants

Caps: 8 MiB/file, 10,000 files/root, 256 MiB total. The 5 MB plan/1.2 MB ledger fit. Limits are inclusive: retain a fully read item at a cap; record the reached limit and any skipped remainder in omissions, then stop before the next read. No silent truncation. <!-- declaration: purpose.scope -->





The synthetic example uses a single accepted learning and a synthetic plan unit. <!-- declaration: workflow.triage -->

## Versioned extension: dossier and packet v2 (v1 remains frozen)

`debrief_dossier.v2` and `debrief_packet.v2` retain all v1 fields and rules above except the schema label and E/C grammar. Each v2 evidence ID is `E` + `str(n).zfill(3)` and each cohort ID is `C` + `str(n).zfill(3)` for integers 1 through 999999. Extra leading zeros are forbidden: E001, E999, E1000 and E123456 are valid; E0001, E000, E01 and E1000000 are invalid. This includes reproduction references. v1 keeps E001..E999 and C001..C999. Both versions compare exact strings. <!-- declaration: purpose.scope -->

`collect` and `compile` write v2. Compilation refuses v1 dossiers by name as mixed-version until `debrief.py upgrade --workdir DIR` changes only schema_version to v2. Repeating upgrade is harmless; `downgrade --workdir DIR` is refused by name. Upgrade leaves packets and approval/filing state untouched. Retain a v1 workdir copy to recount its old packet, or compile and approve a new packet in a separate draft workdir. Recount/show refuse mixed-version pairs. Readers accept both versions, including filed packets and triage records. Renderer and approval digest rules are unchanged. The evidence capacity guard uses the v2 bound and records `over-budget: evidence ids` for inputs exceeding it. <!-- declaration: purpose.scope -->
