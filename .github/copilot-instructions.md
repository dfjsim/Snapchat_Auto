# Snapchat_Auto — instructions for AI tools

<!-- BEGIN dev-rules:git-workflow (synced from dev-rules/git-workflow.md by sync_rules.py; edit it there) -->
# Git workflow (shared rules, synced from dev-rules — edit them there)
- **Independent work gets its own branch in a worktree** (`<repo>-worktrees/<name>` beside the repository), proposed
  before it starts; at the start of work, check the open branches and worktrees for conflicts.
- **The main checkout is the user's**: never commit, switch, stash or reset there; a branch checked out there moves
  only by `git merge --ff-only` to a commit built and checked in a worktree.
- Merge into the main branch often while the work is bug-free, and the main branch back into the open branches when it
  moves. When an implementation is done: merge it, remove its worktree, delete the branch locally and on every remote.
- Releases are built from an up-to-date main branch, never a feature branch; the release commit gets an annotated tag,
  `v` + the full version string.
- A push goes to every remote of the repository; a pre-push hook is never bypassed for code (`--no-verify` only when
  nothing new is published: a deletion, a tag on a published commit).
- The in-development CHANGELOG section reads the same on every branch (no branch name in its heading).
<!-- END dev-rules:git-workflow -->
