---
name: brood-conclave
description: >-
  Convene a panel of the top frontier model from each family (GPT, Gemini,
  Claude, Muse) and have them deliberate to a shared conclusion: blind
  answers, anonymized cross-examination on the cruxes, a ratification vote.
  Use when the owner asks to convene the conclave, the panel or the
  committee, to "ask all the models", to get the models to debate or agree, or
  for a multi-model answer to a strategy, science or review question. The moderator
  (you) runs the protocol and never votes. Not for a quick second opinion on
  code (use brood-review-gate).
---

# brood-conclave

A structured panel, not a chat room. Free-form debate between models mostly
converges on the most confident voice; majority voting explains most of what
debate gains (Choi et al. 2025, "Debate or Vote"), and diverse starting views
plus explicit confidence are what actually help (Zhu et al., ACL Findings
2026). So the protocol keeps the first answers independent, makes every later
move cite an argument, hides who said what, and ends in a vote.

You are the moderator. You write the brief, list the cruxes, draft the joint
statement and report. You do not add claims, take sides, or resolve a crux
yourself. The owner wants the panel's spread and disagreement, not your
synthesis.

Set `BROOD_CONCLAVE_DIR` to the directory containing the loaded `SKILL.md`;
every command is `python3 "$BROOD_CONCLAVE_DIR/scripts/conclave.py" <command>`
(below: `conclave`). Run the long ones (`round`, `ratify`, `doctor`) in the
background; you are notified when they finish.

## Host capability boundary

`scripts/conclave.py` needs Python 3.9+ (standard library only) plus the seat
harnesses it routes to: inside a Lor session (`LOR_SESSION_ID` set) the `lor`
CLI, otherwise `codex`, `agy`, `claude`, `pi` or `opencode`. codex, agy and
claude use their own logins; pi and opencode take provider keys from the
environment, falling back to `export` lines in `~/.zshrc`. It never installs a
harness. Without a working route for a seat, report that seat unavailable with
the doctor's reason; never invent a panelist's answer, stance or vote.

## 1. Roster

`conclave resolve` picks the top model per family from the live OpenRouter
catalog plus what codex and agy list natively, and maps each to its harness
routes. Show the owner the roster in one line per seat. Say explicitly when
the output says `CHANGED`, `NEW_LINE`, `NATIVE`, a native note, or
`unranked new lines`: those are the moments a new flagship (Gemini Carbon,
Opus 6, GPT-7 Astra) appears. The rules live in `families.json`:

- each family lists product `lines`, best first; the newest version of the
  first listed line wins, so a new version inside a line needs no edit;
- an unknown line released after the current pick and priced at least
  `adopt_ratio` (0.5) of it is adopted automatically and flagged `NEW_LINE`;
  add it to `lines` afterwards so the choice is explicit;
- `pin` overrides everything (an OpenRouter-style id, even one only a native
  harness lists).

Effort is `xhigh` everywhere, clamped down only where the model's ceiling is
lower (Gemini Argon tops out at `high`; say so when the owner asks for more).

## 2. Routes and web search

Every route runs with web search on; a route without search is not a route.

| family | routes, in order | search |
|---|---|---|
| openai | codex → pi → opencode → lor | `codex --search`; OpenRouter `:online`; opencode Exa `websearch` |
| google | agy → pi → opencode → lor | Google grounding; `:online`; Exa |
| anthropic | claude → pi → opencode → lor | `--allowedTools WebSearch,WebFetch`; `:online`; Exa |
| meta | pi → opencode → lor | `:online`; Exa |

`lor` runs each seat as a Lor subagent on the Lor router with full sandbox
tools (inheriting the caller's toolset: shell, file tools, etc.) and web search
via `lor web search` and `lor web fetch`. **Lor subagent mode is the default**
(`"lor": true` in `families.json`): every seat runs exclusively as a Lor
subagent (fail-closed, no fallback to codex/agy/claude/pi/opencode) inside a
Lor session (`LOR_SESSION_ID` set).

- `conclave new <board> --brief brief.md` creates a Lor-mode board (`"lor":
  true` in `board.json`), so every subsequent `round` and `ratify` uses Lor
  subagents automatically.
- To use the other harnesses instead (`codex`/`agy`/`claude`/`pi`/`opencode`),
  pass `--no-lor` to `new` (persisted as `"lor": false` in `board.json`) or to
  any individual `resolve`, `doctor`, `round`, or `ratify` command (or set
  `CONCLAVE_LOR=0` / `"lor": false` in `families.json`). Pass `--lor` to force
  Lor mode on a `--no-lor` board.

Each Lor seat runs fresh in its own `work/<seat>` working directory (isolated
from other panelists' files on the board) and reads attached figures and other
attachments directly. Its model ids can differ from OpenRouter's: `lor model
list` is matched automatically, dots become dashes, and `lor_ids` in
`families.json` maps the rest; the id that worked is kept in `lor_ids.json` in
the state directory. Each seat's `work/<seat>/session.json` (and `.meta.json`)
records its Lor session and cost in USD. A Lor moderator also receives each
seat's final message as a subagent report; ignore those and read the board.

A seat falls through to the next route on a non-zero exit, a timeout, an
empty answer, or a quota message: codex running out of plan usage moves the
GPT seat to `pi` with `openrouter/openai/<model>:online` at the same effort,
then to `opencode`. The post's `.meta.json` records every attempt.

`conclave doctor` proves each route before you rely on it: it asks every
route for the latest release tag of a GitHub repo, a fact newer than any
model's training data, and passes a route only if the answer matches the
real tag. Run it when the roster changes and at least weekly; `round` tries
doctor-failed routes last. Exit 3 means some seat has no working route: tell
the owner which, and why (the doctor's detail line), before going on. OpenRouter
seats can fail on the owner's ZDR privacy setting (Muse has before, and Fable
always does); that is the owner's setting to change, and `opencode-direct` avoids
OpenRouter where a direct key exists. A harness older than the model also
fails fast: "Claude Code 2.1.274 does not support this model" meant the
Homebrew cask was behind (`brew upgrade --cask claude-code`; on a native
install, `claude update`). Updating a harness changes the owner's machine, so
say so when you do it.

## 3. Brief

Write `brief.md` yourself, then confirm the question with the owner if it is
strategic and not fully specified. A good brief:

- asks one question that has an answer, and demands one decisive answer per
  seat rather than an option list;
- carries every fact the seats need (they see nothing else), including
  numbers that exist only in figures, transcribed neutrally;
- states constraints and what is off the table;
- never contains your own view or a draft answer.

Figures go in with `--attach`. lor, codex, pi, opencode and claude receive
them (lor also receives non-image attachments); agy cannot read files
headless, so if figures matter and you are running with `--no-lor`, add
`--route google=pi` to each `round`.

## 4. Round 0, blind

```
conclave new <board-dir> --brief brief.md [--attach fig*.png] [--no-lor]
conclave round <board-dir> 0
```

Boards are plain folders; put them next to the work (for example
`./conclave/<date>-<slug>`). Each seat posts markdown plus a JSON block of
claims with 0 to 100 confidences. Expect the slowest seat (Astra at xhigh with
search) to take 15 to 30 minutes. A failed seat can be rerun alone with
`--seats <seat>`; seats that already posted are skipped unless `--force`.

## 5. Cruxes

`conclave digest <board-dir> 0` prints every seat's bottom line and claims
with real names (for you only). Write `r1-cruxes.json`:

```json
{"round": 1, "cruxes": [
  {"id": "C1", "statement": "A declarative claim, as one side would put it.",
   "for": ["openai", "meta"], "against": ["google"], "uncertain": ["anthropic"],
   "note": "optional neutral context"}]}
```

- 2 to 6 cruxes, each a real, load-bearing split: a claim that, if it went
  the other way, would change some seat's bottom line;
- one statement per crux, written so "agree" and "disagree" are both
  defensible; no loaded wording, no compound claims;
- skip disagreements over trivia, wording or things one web search settles
  (check those yourself and put the fact in `note`);
- if all seats agree on everything, say so to the owner and go to step 7.

## 6. Cross-examination

```
conclave round <board-dir> 1
conclave status <board-dir>
conclave cruxes <board-dir>        # writes r2-cruxes.json from open cruxes
conclave round <board-dir> 2
```

Each seat sees the cruxes, its own last answer, and the others' answers as
Panelist A/B/C, shuffled per reader and scrubbed of self-identification. It
must take a stance on every crux and may change only by naming the argument
that moved it. One seat per round, in rotation, also plays devil's advocate
against the majority, while keeping its honest positions in the JSON.
Cross-examination rounds take as long as round 0 or longer, because seats
re-check the facts each crux turns on (about 9 minutes even at low effort in
the first smoke test).

`status` prints the stance table and the stop rule: stop when every crux is
settled, when a round changes no position, or at `max_rounds` (3). Between
rounds, edit the file `cruxes` wrote:

- add a crux a seat raised under "New crux" if it is load-bearing;
- reword an open crux when the seats' reasons show they split on the
  wording rather than the substance (keep its id, say so in `note`). In the
  first smoke test, "unique filenames need no locking" split 2-2 only
  because two seats added "with temp-file-and-rename" and two read it
  without; the sharpened statement is the real crux;
- never drop an open crux, and never reword one toward your own view.

## 7. Statement and ratification

Draft `statement.md` from the posts alone:

1. the panel's answer, where it agrees (with how many seats, at what
   confidence);
2. majority positions with each dissent stated as strongly as its holder put
   it ("one panelist holds ..."), anonymous;
3. open cruxes with what evidence would settle each;
4. nothing the panel did not say.

```
conclave ratify <board-dir>
```

Every seat votes approve, approve_with_reservations or object, with edits.
If anyone objects, apply the edits that do not misstate other seats and
ratify once more; record what is still objected to as dissent. Two passes at
most.

## 8. Report

`conclave export <board-dir>` writes `transcript.md` with real model names.
Give the owner: the roster line (model, route actually used, effort; say if any
seat fell back), the statement, the vote tally, and the board path. Name the
seats in the report; anonymity is for the seats, not for the owner.

## Confidential material

Claude Code's auto-mode classifier may block sending an unpublished
manuscript to outside providers. Do not work around it. Prepare the board,
then give the owner the exact `conclave round ...` command to run in their
own terminal, and read the posts when they say it finished. `round --dry-run`
writes the packets and prints the commands without running them.
`ratify` has no dry run.

The classifier also blocks briefs that carry unpublished results (it did on
the first naming board, 2026-11-01). The owner can lift this for conclave.py by
running `scripts/allow.py` themselves, which adds
`Bash(python3 .../conclave.py:*)` allow rules for this installed copy to
`~/.claude/settings.json`. Never run it for them: it changes their permission
settings. Once the rules are in, call conclave.py by the absolute path
`allow.py` printed (the expanded `$BROOD_CONCLAVE_DIR/scripts/conclave.py`),
so the command matches the rule's prefix.

## Files

- `families.json`: lines, adoption rule, routes, effort, round limit
- `scripts/conclave.py`: all commands; standard library only; no model ids
- `scripts/allow.py`: adds (or `--remove`s) the allow rules for conclave.py;
  the owner runs it, never the moderator
- `templates/`: the round-0, cross-examination, devil's-advocate and
  ratification prompts
- state: `${XDG_STATE_HOME:-~/.local/state}/hanig-conclave/` (`roster.json`,
  `doctor.json`, `lor_ids.json`; live state keeps its `hanig-` name)
- board: `brief.md`, `board.json`, `roster.json`, `r<k>/` (posts, meta,
  packets, logs, labels), `r<k>-cruxes.json`, `statement.md`, `votes/p<n>/`,
  `transcript.md`
