# Releasing

Stable releases use annotated tags matching `vMAJOR.MINOR.PATCH`. Pushing such
a tag runs the `Release` GitHub Actions workflow, which:

1. builds and tests a static x86-64 Linux binary with musl;
2. creates a binary installation archive and a tagged source archive;
3. generates `SHA256SUMS`; and
4. publishes the assets in a GitHub release with generated release notes.

Before tagging, confirm that `main` is clean, pushed, and passing CI. Then run:

```sh
git tag -a vX.Y.Z -m "pam-u2f-touch-popup vX.Y.Z"
git push origin vX.Y.Z
```

The workflow can be run manually from GitHub Actions to test the build and
packaging steps without publishing a release. Published tags and releases are
public interfaces: do not move or replace them; publish a follow-up version
instead.
