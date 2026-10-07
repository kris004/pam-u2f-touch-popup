# Releasing

Stable releases use signed, GitHub-verified annotated tags matching
`vMAJOR.MINOR.PATCH`. Pushing such a tag runs the `Release` GitHub Actions
workflow, which:

1. builds and tests a static x86-64 Linux binary with musl;
2. creates a binary installation archive and a tagged source archive;
3. generates `SHA256SUMS`; and
4. records signed build-provenance attestations for the assets; and
5. publishes the assets in a GitHub release with generated release notes.

Before tagging, confirm that `main` is clean, pushed, and passing CI. Then run:

```sh
git tag -s vX.Y.Z -m "pam-u2f-touch-popup vX.Y.Z"
git verify-tag vX.Y.Z
git push origin vX.Y.Z
```

Configure Git tag signing with a key recognized by GitHub before releasing.
The workflow rejects lightweight, unsigned, or GitHub-unverified release tags.
Before checkout, it snapshots the annotated tag object, requires a valid GitHub
signature, and binds it to the workflow event SHA (the direct commit target or
the exact annotated tag object).
Checkout and packaging use that exact commit. Attestation and publication each
recheck both the tag object and its commit target; a moved tag fails the run.

Protect `v*` tags against updates and deletion and enable immutable releases as
repository policy. API rechecks cannot make tag validation and release creation
one atomic operation; immutable tag protection closes that remaining window.
These settings are managed separately from the workflow.

The workflow can be run manually from GitHub Actions to test the build and
packaging steps without publishing a release, including when dispatched from a
tag. Published tags and releases are
public interfaces: do not move or replace them; publish a follow-up version
instead.
