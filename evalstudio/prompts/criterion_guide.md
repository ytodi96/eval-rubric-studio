Criterion guide

- id: short kebab-case, unique within the rubric (e.g. "offers-free-reship").
- name: a few words a person can scan in a report.
- description: one or two sentences saying exactly what passes. Include the specific values.
- dimension: the card dimension key this criterion measures, or null if none fits.
- weight: 1-3. 3 = the response is useless or harmful without it; 2 = important; 1 = polish.
  Start from the dimension's weight on the card and adjust for this case.
- must_pass: true only for hard rules (the card's must / must-not) and for behaviours whose
  failure would make the response wrong no matter how good the rest is. A case with any failed
  must-pass criterion fails outright, so don't overuse it: usually 1-3 per rubric.
- scale:
  - "pass_fail" when the answer is yes or no (a fact is stated, a rule is followed, an action is
    offered). Leave anchors empty.
  - "1-4" when quality comes in degrees (tone, clarity, how helpful a next step is). Write four
    anchors, one per level, each describing what a response at that level looks like for this
    case: 1 = fails, 2 = weak, 3 = good, 4 = excellent. Anchors must be distinguishable from
    each other by reading the response.
- check: a code check when the criterion can be decided exactly by code; otherwise type "none"
  (the LLM judge grades it). Code checks are pass/fail only.
  - contains / not_contains: `values` are literal phrases (case-insensitive by default; quotes
    and dashes are normalised). For contains, `match` "all" needs every value, "any" needs one.
    not_contains fails if any value appears.
  - regex / not_regex: put the Python regular expression in `pattern` (not in `values`). It runs
    in multiline mode (^ and $ match at every line; use \A and \Z for the start and end of the
    whole response) and is case-sensitive; use (?i) at the start to ignore case. Line breaks may
    be \n or \r\n, so match them with \s* or \r?\n.
  - numeric: `pattern` captures a number in its first group (e.g. \$\s?([\d,]+(?:\.\d+)?)); passes
    if any captured number is within `tolerance` of `expected`.
  - length: `unit` "words" or "chars", with `min` and/or `max`.
  - json_schema: `json_schema` is the JSON Schema the whole response must match, as a JSON string.
    Only for systems whose responses are JSON. For text, use regex.
  Only use a code check if it would pass every good response and fail the bad ones it targets.
  A required phrase is rarely safe: good responses word things differently. Exact numbers,
  fixed sign-offs, forbidden words, formats and length limits usually are.
- also_judge: true when a code check catches the obvious cases but phrasing could slip past it,
  so the LLM judge also grades it (both must pass). Typical for forbidden content: a word list
  catches "discount code", the judge catches "15 percent off your next box".
- Leave every check field the chosen type doesn't use as null.
