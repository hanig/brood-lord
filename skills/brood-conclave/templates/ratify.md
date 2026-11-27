The panel's deliberation is over. You are $label. The neutral moderator has drafted the joint statement below from every round. Each panelist now votes on it.

- **approve**: you would sign it as written.
- **approve_with_reservations**: you would sign it, with your reservations recorded as dissent.
- **object**: you would not sign it. Give the specific edits that would make you approve.

Check three things: that your own position is represented accurately; that each dissent is stated fairly and as strongly as its holder would state it; and that nothing in the statement claims more agreement or more confidence than the panel showed. Use web search if a factual claim in the statement needs checking. Do not name the model or the company you are.

## Statement

$statement

## Your position in the last round

$own

## Output format

Explain your vote briefly in markdown. End with exactly one fenced JSON block of this shape:

```json
{
  "vote": "approve",
  "reservations": ["..."],
  "edits": [{"find": "exact text in the statement", "replace": "your replacement", "why": "..."}]
}
```

`vote` is approve, approve_with_reservations or object. Leave `reservations` and `edits` empty when you have none.
