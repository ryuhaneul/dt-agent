"""Built-in instructions for ``memory_lint.mode: full`` (used when ``LINT_FULL.md`` is unusable).

Kept identical to the seed file ``_home_defaults/workspace/tools/memory_tools/LINT_FULL.md``
(a test compares them).
"""

from __future__ import annotations

DEFAULT_FULL_INSTRUCTIONS = """\
# Memory lint: semantic check, monthly stale check and report

This file is read by the framework for every `memory_lint.mode: full` run and is yours to edit
(it is created once and never overwritten by updates; delete it and restart to get the current default text back).
The framework puts the structural check result and the stale-check flag above it.

Your final answer is delivered to the user as the report. Do not send messages yourself.
Write the report in the user's language.

## 1. Structural results

Use the structural check result given above (it comes from
`python3 tools/memory_tools/memory_lint.py check`). Do not repeat the check unless you fixed
something in step 4 and want to confirm the fix.

## 2. Semantic check (what the script cannot see)

Read the pages and judge (a) whether two pages are duplicates of the same topic and (b) whether
two pages state different values for the same fact (a contradiction).

Duplicates, strict criteria. A topic overlap alone is not a duplicate:

- Report a duplicate only when two pages are in effect the same content. The same keywords or
  subject are not enough.
- Two pages about the same subject but with a different aspect or role are a legitimate split.
  Do not recommend shrinking them. Example: how to call an API versus how it is operated
  (subscription, pricing); a whole-infrastructure page versus an SSH helper page.
- Even for a real duplicate, the direction of reduction is from the less detailed page to a
  pointer at the more detailed one, so detail is kept. Never recommend summarizing the more
  detailed page.

Never read or judge the text between `--- SHARED KNOWLEDGE START ---` and
`--- SHARED KNOWLEDGE END ---`; that block is maintained by the framework.

A pair that was already judged a legitimate split must not be flagged again.

## 3. Stale check (only when the stale flag above says to perform it)

The structural check only looks at form. Here you compare the content with reality: the code,
configuration and files that exist. Do this once a month, in the first run of the month; the
month is recorded as attempted when that run starts, not when it succeeds. When the flag says it
was already attempted this month, skip this step and state in the report: "Stale check skipped
in this run - already attempted in the first run of this month."

Scope: pages that record rules, preferences, workarounds or instructions (typically
`type: feedback` pages and similar). A page is a candidate if at least one of these holds:

1. A workaround for a problem that has since been fixed: the tool or framework no longer needs
   it. Evidence lives in the framework reference `docs/dt-agent/`, in `tools/` and in `skills/`.
2. A conflict with a current rule: `BEHAVIOR.md`, a rules file or a convention page of the wiki,
   or a later-dated page, says the opposite. The later date wins.
3. A reference to something that no longer exists: a removed skill, tool, path or server.
   Verify existence by listing or opening the file, not by assumption.
4. A fact that changed: model name, path, host, version. Before comparing a name against a
   configuration file, first confirm that the name is actually listed in that file. Do not read
   a neighbouring entry by line number.

Not stale, never a candidate: a merely old date; a record of a past incident (its Why section);
two pages that only share a topic; a sentence already corrected with a note such as "the old X
is retired".

A candidate needs evidence: a file path plus a line number or code location. Without it, do not
raise it.

"No candidate" is a normal result. Write one line "checked, nothing wrong" for anything where
you only verified one side of a supposed conflict, where the file did not actually contain the
subject, or where your judgement rested on "probably". Do not raise low-confidence items to fill
a count: one wrong candidate costs more than five right ones (a person has to check and reject
it, and a wrong edit is risky).

## 4. Automatic fixes (small things you fix yourself and report)

Fix only what has exactly one mechanical correct answer:

- A broken link caused by a typo or a missing prefix, when the target page is uniquely identified
  by its alias or basename.
- A missing `updated:` field: insert today's date.
- Missing frontmatter: create the minimum set (`title`, `type`, `tags`, `aliases`, `updated`),
  deriving the title from the body.

Never do anything else on your own: changing the wording or content of a page, deleting,
merging or shrinking pages, resolving a contradiction, updating a fact. All of that is the
user's decision. If the target of a fix is ambiguous (two or more candidates), do not fix it;
raise it as a decision item.

## 5. Limits

Do not modify or delete any `.md` file beyond what step 4 allows.

## Searching

- Look pages up with `python3 tools/memory_tools/memory_search.py word1 word2` first (two to
  four key words, not a whole sentence).
- Never search the whole workspace root recursively (`rg ... .`, `grep -r .`): it walks backups,
  tasks and temporary directories and overloads the host. Always limit the path.
- Extract context around a keyword with Python slicing, not with a bounded-repetition `grep`
  pattern: some environments wrap `grep` in a tool that rejects such patterns.

## Report format

Group the report into what you handled and what the user must decide; no raw log dumps.
Title: "Memory Lint report (YYYY-MM-DD)".

1. **One-line conclusion**: either "all fine" or "N decisions needed". The user decides from
   this line whether to open the report.
2. **What I fixed** (no action needed): only items actually fixed in step 4, one line each:
   what, then how. "None" if nothing.
3. **What needs the user's decision** (the core of the report). For every candidate fill in
   all four parts; if one is missing, you have not finished investigating that candidate:
   - what the problem is, in one plain sentence;
   - evidence: file path and line number, both sides for a conflict;
   - why a decision is needed now: what goes wrong if it is left alone;
   - options: two or three possible actions with one recommended option named. A single word
     such as "recommended" is not enough.
   Order by severity: security or wrong-behaviour risk, then factual error, then notation
   mismatch.
4. **For reference** (no action): by-design findings (links skipped by the check, frontmatter
   exceptions of the main memory file) as a count on one line. No lists.

Do not:

- list raw findings and stop; a list without material for a decision is not a report;
- repeat last week's candidate in the same words. Items already judged a legitimate split or
  "no action" are not raised again; a still-valid open item is marked "open for N weeks";
- invent candidates. If nothing is wrong, end with "nothing wrong".
"""
