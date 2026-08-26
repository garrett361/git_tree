---
name: git-tree-stack-prs
description: Turn an existing chain of git-tree branches into a linked stack of draft pull requests on GitHub, using `gh stack link` without adopting gh-stack's local tracking. Use when asked to open stacked PRs for a git-tree stack, to wire a freshly split branch chain up on GitHub, or to add pull requests for branches git-tree already manages.
---

# Open a stack of PRs for a git-tree branch chain

git-tree owns the branches; this only wires them up on GitHub. Every step is ordered to keep that
true, so that `gh` never creates a branch, never pushes one, and never writes local tracking state.

## Prerequisites

The branches already exist as a git-tree chain and are the shape you want. Whether each branch is
independently mergeable is **not** checked here: a branch can be green at its own tip and still
break its base once merged alone. Ask for that review separately if it matters.

Run from any worktree of the repo; every command below names its branch explicitly, so which branch
is checked out does not matter.

```sh
gh auth status
gh extension install github/gh-stack   # needs gh >= 2.90.0; the `repo` scope is enough
```

## 1. Read the chain

```sh
git tree --json
```

Each branch's `parent` field is its PR base. Order the branches bottom-up, root-most first, and name
that order to the user before touching anything. Every step below runs in that order.

## 2. Push the whole chain first

```sh
git tree push <bottom-branch> -y
```

This pushes the branch and every descendant to the tree's single remote with `--force-with-lease`,
parents before children, skipping a branch's descendants if it fails so a partial stack never lands.
Do it before any `gh` call: `gh stack link` pushes branch-name arguments itself, and pushing first
makes that a no-op, leaving git-tree the only thing that writes remote refs.

## 3. Propose every title and body, then wait

Draft, for each branch bottom-up:

- a **title**, defaulting to that branch's headline commit subject in conventional-commit form
- a **body**: one line of stack context ("Second of four stacked PRs, builds on #N") plus a sentence
  or two on what that branch alone is responsible for

Show them all to the user together and take accept-or-revise feedback per PR **before** creating
anything. A body is public the moment its PR exists, and editing it later leaves the first version
in the PR's edit history.

## 4. Create the PRs bottom-up

```sh
gh pr create --draft --base <parent> --head <branch> --title <title> --body-file <file>
```

Bottom-up is required: each body cites its parent's PR number, which does not exist until the parent
PR is created. Substitute that number into the next body just before creating it, using a **global**
replace. A non-global `sed` rewrites only the first occurrence and ships the remaining placeholders
live.

`--draft` is the default; drop it only when asked. Do not assume drafts skip CI, which depends
entirely on the repo's workflow triggers.

## 5. Link the stack, by number

```sh
gh stack link <n1> <n2> <n3>
```

Pass **PR numbers, never branch names**. Numeric arguments resolving to existing PRs make this a
pure association write: no push, no PR creation, no change to the titles and bodies just agreed.
Branch-name arguments are pushed and turned into fresh PRs whose titles and bodies come from an
undocumented generator instead.

Use no other `gh stack` subcommand. `init`, `add`, `rebase`, `sync`, and `submit` write
`.git/gh-stack` and assume ownership of branches and rebases, which is git-tree's. `--open` marks
every PR ready for review, so omit it to keep drafts.

## 6. Verify

```sh
gh api graphql -f query='{ repository(owner:"<owner>", name:"<repo>") {
  pullRequest(number: <n>) { stack { number size } stackEntry { position } baseRefName } } }'
```

`stack` must be non-null with `size` equal to the chain length, and every PR's `baseRefName` must be
its git-tree parent. Report the stack number and the PR numbers in order.

## Ordering constraints

- Creating PRs before pushing: `gh pr create` fails on a branch the remote does not have, and
  letting `gh stack link` push instead hands remote-ref writes to a tool git-tree knows nothing of.
- Creating top-down: the parent's number is unavailable for the child's body, so bodies end up wrong
  or placeholder-ridden.
- Linking by branch name: silently creates duplicate PRs carrying generated descriptions.

## After the stack exists

Context rather than steps, for work continuing in the same session:

- `git tree propagate <branch>` followed by a force-push of every branch does **not** break the
  stack. Its number, size, positions, and bases all survive a full rewrite, so nothing needs
  re-linking.
- `gh stack link <stack-number> <branch>` appends to an existing stack. A numeric first argument
  matching a stack is read as that stack, so its current members need not be re-listed.
- Once a PR merges, re-homing the remaining branches is the `git-tree-land` skill's job.
- GitHub's limits: same-repo branches only, linear history between layers (git-tree's cascade
  guarantees that), and 100 PRs per stack.
