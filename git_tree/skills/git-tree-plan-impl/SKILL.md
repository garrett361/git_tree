---
name: git-tree-plan-impl
description: Pick up and implement a plan handed off by the git-tree-plan skill. Use when starting work in a worktree that was created by git-tree-plan and contains a PLAN.md, or when the user says "implement the plan here", "pick up this handoff", or similar, from inside such a worktree.
---

# Implement a handed-off plan

This worktree and its `PLAN.md` were produced by another session running the `git-tree-plan`
skill, which already resolved open questions with the user and got the plan approved. You have
none of that conversation's context — only what's written down. Treat the plan as trustworthy,
not as something to re-litigate from scratch, but verify it against the actual repository rather
than executing it blind.

## 1. Read the plan, then read what it points at

Read `PLAN.md` in the current worktree in full. If it isn't there under that name, ask the user
where it is rather than guessing. Then read the specific files, functions, and line ranges the
plan names — not just enough to skim, enough to confirm the plan's claims about them still hold.
This is also where staleness surfaces: a moved function, a renamed field, code that's already
been changed since the plan was written. There's no separate verification step beyond this,
because reading the plan's own references is the same work that would otherwise duplicate it.

If anything conflicts with what the plan says, or an open question was left unresolved, stop and
raise it with the user before writing code — this includes anything the plan's own author has
flagged as unresolved (out-of-scope caveats and future-work notes are not the same thing as an
unresolved question; those are the plan working as intended).

## 2. Summarize understanding before implementing

Post a concise summary of what you're about to do: the goal, the key design decisions the plan
already made and why, and the files you'll touch. This is a checkpoint by inspection, not a
formal approval gate — the user reads it to confirm you and the plan actually agree, and can
redirect before you're deep into an implementation built on a misreading. Fold in whatever extra
context or emphasis the user passed via this skill's `args` at invocation; if it changes scope or
conflicts with something in `PLAN.md`, say so explicitly in the summary rather than silently
picking one.

## 3. Implement

Follow the plan's structure. Run whatever verification section it specifies.
