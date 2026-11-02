# Debrief contract v1

Exact keys only; reject duplicates at every depth, unknown fields/enums,
wrong types and unsupported versions. Integers mean
`type(v) is int`, excluding booleans. Strings contain Unicode scalar values;
nonempty strings exclude blank text. Never trim, case-fold, Unicode-normalize
or coerce to accept input. ASCII patterns match exactly.

## Catalog: `debrief_catalog.v1`

- Top level: `schema_version` = `debrief_catalog.v1`, `version` = integer 1,
  `entries` = nonempty list.
- Entry: `id` (unique `DB` plus three digits), `skill` (exact existing bundle
  name under `skills/`, with `SKILL.md`), `what_broke`, `mechanism`,
  `proposed_change` (nonempty strings), `measures` (nonempty distinct pair list).
- Pair: `measure` (`[a-z][a-z0-9_]*`), `population` (`units`, `attempts`,
  `evidence_documents`, `helper_scripts`, `state_entries`).

Whole tuples cannot be mixed. Changes require a new version.
Mechanisms describe reports, not proven causes.

## Packet: `debrief_packet.v1`

Top level: `schema_version` = `debrief_packet.v1`, `catalog_version` (integer
matching catalog), `packet_id` (random UUID4 independent of content, lowercase hyphenated RFC 4122 variant), `project` = `project A`, `learnings`
(list of 1–50 items).

Learning: `id` (sequential `L01`–`L50` starting at `L01`), `catalog_entry` (id),
`skill`, `what_broke`, `mechanism`, `proposed_change` (copied byte-for-byte from
that entry's decoded strings), `mechanism_basis` (`reported`, `inferred`,
`reproduced`), `counts` (list, possibly empty), `evidence_refs` (nonempty list
of `E001`–`E999`).

Count: `measure`, `population` (permitted pair), `scope` = `inspected_set`,
`numerator`, `denominator` (integers: `0 <= numerator <= denominator`,
`1 <= denominator <= 1000000`), `cohort_ref` (`C001`–`C999`). Each measure occurs
at most once per learning. Never add/substitute populations. E/C refs resolve
privately at compilation.

Public output uses only fixed keys, trusted catalog literals, generated ids,
enums and verified integers. No source strings or production deny-list/scanner.

## Issue grammar and approval

Repository `hanig/brood-lord`; label `debrief`; renderer version integer 1.
Title: exactly `hanig-debrief: <packet_id>`. Fixed preamble paragraph:

> Evidence references are held privately by the source project's owner.

Body: `PREAMBLE + "\n\n\x60\x60\x60json\n" + json.dumps(packet, sort_keys=True,
indent=2, ensure_ascii=False) + "\n\x60\x60\x60"`; `\x60` is a backtick.
Exactly one fenced `json` block; nothing follows, even a trailing newline.

Approval digest: SHA-256 hex of UTF-8 canonical compact JSON (`sort_keys=True`,
`separators=(",", ":")`, `ensure_ascii=False`) of exactly
`{"packet": packet, "title": title, "repository": "hanig/brood-lord",
"label": "debrief", "renderer_version": 1}` with actual packet object/title.
Owner approval follows viewing the scrubbed packet/title and binds this digest.
Changes void approval; approval never upgrades basis. The dossier
is private and never passed to the filer.

## Filing state machine

One issue per packet. Persist digest/issue identity and
`POST_IN_FLIGHT` before POST, then freeze content. Legal transitions:

| From | To | Condition |
| --- | --- | --- |
| `DRAFT` | `APPROVED` | Owner approves displayed packet digest |
| `APPROVED` | `DRAFT` | Withdrawn/changed before POST |
| `APPROVED` | `POST_IN_FLIGHT` | Revalidate approval, persist intent |
| `POST_IN_FLIGHT` | `DELIVERY_UNCERTAIN` | Unknown/interrupted delivery |
| `POST_IN_FLIGHT`, `DELIVERY_UNCERTAIN` | `READBACK_FAILED` | Known issue, readback unavailable |
| `POST_IN_FLIGHT`, `DELIVERY_UNCERTAIN`, `READBACK_FAILED` | `FILED` | One exact repository/title/label/canonical-body readback |
| `POST_IN_FLIGHT`, `DELIVERY_UNCERTAIN`, `READBACK_FAILED` | `CONFLICT` | Mismatch/multiple identities |

Each source permits the transition. `FILED`/`CONFLICT` are terminal.
`DELIVERY_UNCERTAIN`, `READBACK_FAILED`, `CONFLICT` fail closed: no automatic
repost. Crash during POST is uncertain; empty search is not nondelivery proof.
Recovery retains records and reconciles read-only; unresolved states stay stopped.
Same-state persistence authorizes no POST.

## Private dossier: `debrief_dossier.v1`

Keys: `schema_version` = `debrief_dossier.v1`, `evidence`, `cohorts`,
`candidates`, `omissions`, `provenance`.

`evidence`: object keyed `E001`–`E999`. Record keys:
`source_root` (private label), `locator` (relative, no absolute/parent traversal),
`sha256` (64 lowercase hex file digest), `location`, `unit_id`, `attempt_id`
(nonempty strings where applicable, otherwise null), `adapter` (exactly
nonempty `name`, `version` strings), `status` (`ok`, `skipped`, `unsupported`,
`unstable`). Location is `{"line_range": [start, end]}` (inclusive integers,
1 <= start <= end) or `{"json_pointer": pointer}` (RFC 6901). Undigested files are omissions.

`cohorts`: object keyed `C001`–`C999`. Record keys: `population`, `members`,
`qualifying` (sorted distinct private ids). Qualifying is a subset of members.
Counts require matching population, `len(qualifying) == numerator` and
`len(members) == denominator`. Sort exact strings.

`candidates`: list. Keys: `catalog_entry` (id or null), `status` (`selected`,
`covered`, `unrepresentable`, `rejected`), `basis` (basis enum), `rationale`
(nonempty private string), `evidence_refs`, `counts` (lists as above),
`reproduction`, `coverage_disposition` (`covered`, `extends`, `conflicts`,
`not_found_in_snapshot`, `unknown`). Null entries have no counts/public output.

`reproduction`: null unless basis is `reproduced`, when complete and required.
Keys: `receipt_ref` (E-record citing reproduction receipt), `tested_version`
(brood-lord commit or local modification tested), `probe`, `observed` (nonempty
strings). Signature matching never yields reproduction. `reported`: source
asserts mechanism. `inferred`: classified evidence supports it; private
rationale records inference.

`omissions`: every skipped/over-budget input with `source_root`, `locator`,
`reason` (nonempty strings); root-level `.` may describe uninspected remainder.
`provenance`: installed skill names mapped to readable marker contents;
absent/unreadable markers are omissions.

## Triage: `debrief_triage.v1`

At `debrief/triage/<packet_id>.json`. Keys: `schema_version` = `debrief_triage.v1`,
`packet_id` (packet UUID), `packet_digest` (SHA-256 of UTF-8 canonical compact
packet JSON, distinct from approval digest), `issue` (exactly `repository` =
`hanig/brood-lord`, `number` = positive integer), `dispositions` (object keyed
by learning id, exactly every packet learning). Each disposition:

- Accept keys: `disposition` = `accept`, `unit_refs` (nonempty list of nonempty unit ids).
- Reject keys: `disposition` = `reject`, `reason_code` (`already_addressed`,
  `local_modification`, `unsupported_mechanism`, `insufficient_evidence`,
  `out_of_scope`), `rationale` (nonempty), `references` (public brood-lord
  references below; nonempty required only for `already_addressed`).

Offline validation checks mechanics; the orchestrator judges. Triage never
marks coverage automatically.

## Coverage: `debrief_coverage.v1`

Keys: `schema_version` = `debrief_coverage.v1`, `version` = integer 1 (registry
format revision), `entries` (list, initially empty). Entry keys: `id` (unique
`CV001` upward, three-digit padding without extra leading zeros), `catalog_entry`
(existing id), `applicability`, `references` (nonempty list), `note` (nonempty).
Applicability: exactly `{"fixed_in": SHA}` (full lowercase 40- or 64-hex commit
SHA) or `{"always": true}`.

References: public `https://github.com/hanig/brood-lord/pull/<positive integer>`
or repository paths of slash-separated `[A-Za-z0-9_.-]+` components excluding
empty/`.`/`..`, with no absolute paths. References are syntax-checked. Applicability uses only Git ancestry against a
local checkout, never commit ordering; unknown commits mean unknown.

## Budgets and constants

Per file: 8 MiB (8,388,608 bytes), fitting the cited 5 MB plan and 1.2 MB ledger.
Per root: 10,000 files. Total across roots: 256 MiB (268,435,456 bytes). Limits
are inclusive; reaching one stops reads at that boundary and records the limit
and skipped remainder in `omissions`. No silently truncated evidence.

## Supported envelope and accepted limits

This unit writes data and a validator only. Wording in the catalog is reviewed by the panel as public text.
