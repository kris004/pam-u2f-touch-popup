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
- Do not change visibility, publish, push, or create a release without explicit
  authorization.
