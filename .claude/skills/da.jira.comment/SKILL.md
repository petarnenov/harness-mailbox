---
name: da.jira.comment
description: "Draft an English Jira comment on a project ticket, show it, and post only after confirmation. Use for \"/da.jira.comment <KEY> <what>\", \"comment on ISSUE-123\", \"comment on a Jira issue\", \"write a ticket comment\" while working in this repository."
argument-hint: "<issue-key> <what the comment should say>"
---

LOOP: da.jira.comment@2026-11-16.1

# DA Jira Comment — harness-mailbox dashboard

Post one well-formed comment on a Jira issue. `$ARGUMENTS` holds the issue key and what the comment
should say — in any language, as rough notes or as a finished text.

A comment is written for a reader who opens the ticket cold, weeks later, on a phone. It must make
sense without the chat that produced it, say its point in the first line, and make any ask impossible
to miss. Everything below serves that.

## Hosts

| | Claude Code | Codex CLI | Copilot CLI |
|---|---|---|---|
| invoke | `/da.jira.comment <KEY> <what>` | `$da.jira.comment <KEY> <what>` (skill read through `.agents/skills/da.jira.comment`) | `/da.jira.comment <KEY> <what>` |
| Atlassian access | Discover the connected Atlassian read/comment operations | Discover the connected Atlassian read/comment operations | Discover configured Atlassian operations; if absent, draft from supplied evidence and do not post |
| confirmation tool | `AskUserQuestion` (*Post* / *Edit* / *Cancel*) | `request_user_input` | `ask_user`; when unavailable, plain-text question and wait |

Operation names differ by host; the **procedure and the rules do not**. If a host exposes neither the
operation nor `discover`, say so and fall back to the draft file. Never guess an operation name.

## Fixed rules

- **English only.** The posted text is English, even when the request, the notes, or the ticket are
  in another language. Translate meaning, not words. Names are written as Jira displays them. Code identifiers, file paths, class names, CLI commands, error messages and UI labels stay verbatim.
- **Never post unconfirmed.** Show the exact final text and wait for an explicit yes. Silence,
  "looks fine?" or approval of an earlier draft is not a yes. Any change → show it again.
- **Never post on behalf of another skill or agent.** Only a direct request from the user in this
  conversation authorizes a Jira write. `da.loop`, `da.merge` and the `da-python-*` agents are
  forbidden from writing to Jira; they may draft into a run-log side file for the user instead.
- **One new comment per call.** Edit an existing comment (`commentId`) only when the user asks, and
  only one the user authored. Never delete comments, change fields, transition, log work or assign —
  a comment says *why*; the field change itself is the user's, or a separate explicit request.
- **No secrets, ever.** On this stack that means no access/session tokens, API keys, passwords, private keys, customer data, or SQL/data dumps containing real records. Replace them with placeholders and tell the user what was left out.

## Procedure

### 1. Resolve the issue
Key from `$ARGUMENTS` → from the conversation → ask. A key guessed from the branch name is
**confirmed with the user** first — branch names can refer to a parent task or broader integration work; verify the issue key with the user whenever ambiguous.
Resolve the Atlassian site/cloud ID from the connected resources; do not assume a site identifier.

### 2. Read before writing
Fetch the issue (`getJiraIssue`): summary, status, type, assignee, reporter, parent, and **all
comments** (`listJiraIssueComments`, paged — tickets often have an empty description
and keep the requirement, reproduction data and the target base branch in comments). Use them to:
- avoid repeating what is already said — if the point is already there, tell the user and stop;
- answer the right person and the right question when the comment is a reply;
- match the ticket's vocabulary (feature names, AC numbering, the branch name QA expects);
- when the ticket is a sub-task, review its parent so the comment does not contradict a decision recorded there.

### 3. Pick the comment type
Exactly one purpose per comment. When the request mixes two (a status update and a question for
someone else), propose two comments, or put the ask first and the status after it.

| Type | Use it for |
|---|---|
| Status update | progress on work in flight |
| Question | something that blocks or shapes the work and needs an answer |
| Blocker | work cannot continue until something external changes (an unavailable dependency, environment, access grant, or required configuration) |
| Ready for QA / handoff | the change is done and someone else picks it up |
| Bug report / repro | a defect found on this ticket |
| Decision record | a choice was made — capture what, why, and by whom (architecture choice, API contract, or deployment decision) |
| Review / reply to feedback | answering comments on the ticket or a PR |
| Investigation findings | what was checked, what was found, what it means |

### 4. Draft in English with the template

Write in the templates below; drop a section that would be empty, never pad one. Keep the whole
comment **under ~150 words** unless it is a repro or an investigation, and never more than ~300 —
longer material goes into an attachment, a Confluence page or the PR, and the comment links to it.

**Status update**
```
**Status:** <one line — where things stand, e.g. "server change complete; frontend change in review">
**Done:** <bullets, past tense, outcomes not activities>
**Next:** <bullets>
**Risks / blockers:** <bullet, or omit>
```

**Question**
```
@<Person> <the question itself, one sentence, ending with "?">
**Context:** <1–3 lines — only what is needed to answer>
**Options:** A) <option — consequence>  B) <option — consequence>
**Recommendation:** <A/B and why, one line>
**Needed by:** <absolute date> — <what it blocks>
```

**Blocker**
```
**Blocked:** <what cannot proceed>
**Cause:** <fact, with evidence link> (label a guess as "Hypothesis:")
**Needed:** @<Person> <the concrete action that unblocks it>
**Impact:** <what slips, and by when>
```

**Ready for QA / handoff**
```
**Ready for QA** — <one-line summary of the change>
**PR:** <pull request link, if applicable>   **Branch:** <base branch the PR targets>
**Env:** <target environment and service endpoint, if applicable>
**Deployment:** <none | service image rebuilt | configuration or secret updated>
**How to test:**
1. <step — login id / role, never a real client>
2. <step>
**Expected:** <result>
**Out of scope / known limitations:** <bullets, or omit>
```

**Bug report / repro**
```
**Summary:** <what is wrong, one line>
**Environment:** <env/namespace, image tag or commit, service version, client, user role or test identity (never real customer data)>
**Steps to reproduce:**
1. <step>
**Expected:** <...>
**Actual:** <...>
**Evidence:** <screenshot/log attachment or link; short error text or HTTP status in a code block>
**Frequency / impact:** <always / intermittent; which roles or firms are affected>
```

**Decision record**
```
**Decision:** <what was decided>
**Why:** <reason, 1–2 lines>
**Alternatives considered:** <option — why not>
**Decided by:** <names>, <absolute date>   **Follow-up:** <ticket or action, or omit>
```

**Reply to feedback**
```
@<Person> <addressed / answered / disagree — one line>
- <point 1> — <what changed, with commit/PR link>, or <the answer>
- <point 2> — ...
```

**Investigation findings**
```
**Finding:** <the conclusion first>
**Checked:** <bullets — what was looked at, including what was ruled out; name the component: dashboard backend / frontend / orchestrator scripts / container workflow>
**Root cause:** <fact> / **Hypothesis:** <if unproven>
**Next step:** <owner — action>
```

### 5. Style rules (apply to every type)

- **Bottom line first.** The first line alone must tell a reader what this comment is and whether
  they must act.
- **Explicit ask, explicit owner.** Every request names who (@mention), what, and by when. "Can
  someone look at this?" is not an ask.
- **Mention sparingly.** @mention only people who must act or decide — a mention is a
  notification. Do not mention the assignee for a plain status update; they already watch it.
- **Facts vs guesses.** State facts plainly; mark anything unproven as "Hypothesis:". No "should
  be fine", no "probably".
- **Absolute dates** with the day ("by Tue, 2026-11-03"), never "tomorrow" or "next week".
- **Link, don't dump.** PR, commit, workflow run, Confluence and dashboard links instead of pasted
  content. A code block only for a short, relevant excerpt (≤10 lines) — an exact error, an HTTP
  status, a query, a config key. Full logs go into an attachment.
- **Name the layer.** Say whether a change or defect lives in the dashboard backend, frontend, orchestrator scripts or container workflow — QA and reviewers route by that.
- **Neutral, professional tone.** No blame, no sarcasm, no apology padding, no emojis, no
  exclamation marks. Criticise code and behaviour, never people.
- **Plain, short sentences.** Active voice. No filler ("Just wanted to…", "Hope this helps"). Spell
  out an acronym the audience may not know on first use.
- **Self-contained.** No "as discussed" or "see chat" without the substance or a link.
- **No AI attribution.** The comment is the user's; do not add "generated by…" lines.

### 6. Self-check before showing
Go through the draft and fix it until every line holds:

- [ ] entirely English — no Cyrillic outside quoted identifiers or UI strings
- [ ] first line states the point
- [ ] one purpose; the template's sections are filled or dropped, not padded
- [ ] every ask has an owner and an absolute date
- [ ] every mention resolved to an accountId (step 7)
- [ ] no secrets, credentials or customer PII (see Fixed rules for this stack's list)
- [ ] links instead of dumps; code blocks ≤10 lines
- [ ] length within the limit
- [ ] nothing duplicates an existing comment

### 7. Mentions
Markdown `@Name` does **not** notify anyone. For each person:

1. Resolve with `lookupJiraAccountId` (Claude: `discover` → `executeRead`). If nothing matches, try a spelling or surname fragment. Never infer an account identifier from a display name.
2. Several matches or none → ask the user; never guess an accountId.
3. With at least one mention, post with `contentFormat: "adf"` and a mention node:
   `{"type":"mention","attrs":{"id":"<accountId>","text":"@<Display Name>"}}`.
   The body is a full ADF document: `{"type":"doc","version":1,"content":[{"type":"paragraph",
   "content":[<mention node>,{"type":"text","text":" ..."}]}]}`; bold is a text node with
   `"marks":[{"type":"strong"}]`, lists are `bulletList`/`orderedList` of `listItem`.
   Without mentions, `contentFormat: "markdown"` is enough.

A mention added later by editing a comment may not notify — put it in the new comment.

### 8. Show, confirm, post
Show the user, in the chat:

- the issue key and its summary;
- the comment type and who will be notified;
- the **exact** text that will be posted (rendered markdown, not ADF JSON);
- anything left out on purpose (secrets, PII, content moved to a link).

Ask for confirmation with the host's question tool (*Post* / *Edit* / *Cancel*). On *Edit* apply the
changes and show it again. On *Post* call the host's comment-write operation
(`addOrEditJiraIssueComment` on Claude, `addCommentToJiraIssue` on the Atlassian Remote MCP) with the
confirmed text — the same text, no last-minute rewording. Use `commentVisibility` only when the user
asks to restrict it. On a host without Atlassian access, *Post* means: write the draft file named in
the Hosts table and tell the user where it is.

### 9. Report
Reply to the user in their chat language with the posted comment's link
(`https://<issue URL>?focusedCommentId=<id>`) and who was notified. If the
call failed, show the error verbatim and do not retry with a changed text without asking.
