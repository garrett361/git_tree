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

Say too whether you will orchestrate or implement directly (step 3), and how you will split the
work.

## 3. Implement as an orchestrator

Unless delegating would cost about as much as doing the work, delegate it to subagents and spend
your own context on the plan-level picture: whether the pieces still add up to the plan. Ask for
concise reports, read diffs rather than whole files, and have subagents run verbose commands.

- **Briefs**: a subagent starts with none of your context, so name the `PLAN.md` section, files,
  repository rules, and how to verify. It stops and reports rather than improvising, and ends with
  what changed, any departure from the brief, and open questions.
- **Review**: read every diff yourself against the plan, the repository's rules and patterns, and
  general good practice, and send it back until it passes.
- **Deviations**: decide tactical ones yourself; take anything that changes a plan decision, the
  scope, or user-visible behavior to the user first, as in step 1.
- **Progress log**: keep a `## Progress and deviations` section at the end of `PLAN.md`, updated as
  units land. If it already exists, resume from it.
- **Parallelism**: in a shared worktree, run subagents in parallel only across disjoint files, and
  only you stage, commit, or touch branches. For more parallel work, you may give subagents their
  own worktrees off this branch and merge their work back yourself.

If you implement directly, hold your own work to the same review. Finish by running the plan's
verification section and summarizing what was done, every deviation, and anything unresolved.
