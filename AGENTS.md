# Repository agent policy

## Publication boundary

- Treat every file, commit, branch, tag, and reachable history as potentially
  public, even while the repository has no public remote.
- Do not commit secrets, private network details, personal data, private issue
  links, environment identifiers, or unnecessary absolute local paths.
- Keep documentation, examples, fixtures, and defaults portable for external
  users and contributors.
- Preserve unrelated dirty work and stage exact paths.
- Before exposing previously private history, scan both the current tree and
  reachable Git history for private or environment-specific material.
- Validate, commit, and push completed task-owned changes to the intended
  existing remote without a separate push request. Push directly when permitted;
  if main or the target branch is protected, push a task branch and follow the
  required pull-request/review/check workflow without bypassing protections.
- Review the complete outgoing commit range, exclude unrelated work and secrets,
  avoid force-pushes, and verify the remote branch after pushing.
- New remotes, visibility changes, release/tag publication, and deployment
  require separate explicit authorization. A push that itself releases or
  deploys also needs that separate authorization.
