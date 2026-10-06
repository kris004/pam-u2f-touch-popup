#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise the actual release workflow shell blocks with offline mocks."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = (PROJECT_ROOT / ".github/workflows/release.yml").read_text()
COMMIT = "1" * 40
OTHER_COMMIT = "2" * 40
TAG_OBJECT = "3" * 40
OTHER_TAG_OBJECT = "4" * 40


def step_block(name: str) -> str:
    marker = f"      - name: {name}\n"
    assert WORKFLOW.count(marker) == 1, name
    return WORKFLOW.split(marker, 1)[1].split("\n      - ", 1)[0]


def step_script(name: str) -> str:
    block = step_block(name)
    if "        run: |\n" not in block:
        return block.split("        run: ", 1)[1].splitlines()[0] + "\n"
    lines = block.split("        run: |\n", 1)[1].splitlines()
    script = []
    for line in lines:
        if line and not line.startswith("          "):
            break
        script.append(line[10:] if line else "")
    return "\n".join(script) + "\n"


class Harness:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.bin = root / "bin"
        self.bin.mkdir()
        self.state = root / "api.json"
        self.log = root / "calls.jsonl"
        self.output = root / "output"
        self.ref = {"object": {"type": "tag", "sha": TAG_OBJECT}}
        self.tag = {
            "sha": TAG_OBJECT, "tag": "v0.0.1",
            "object": {"type": "commit", "sha": COMMIT},
            "verification": {"verified": True, "reason": "valid"},
        }
        self.env = os.environ.copy()
        # No real gh, git, make, or sudo is reachable from these shell blocks.
        self.env.update(
            PATH=str(self.bin),
            GITHUB_REPOSITORY="fixture/project", GITHUB_REF_NAME="v0.0.1",
            GITHUB_REF_TYPE="tag", GITHUB_SHA=COMMIT, GITHUB_EVENT_NAME="push",
            GITHUB_OUTPUT=str(self.output), VERIFIED_COMMIT=COMMIT,
            VERIFIED_TAG_OBJECT=TAG_OBJECT, RELEASE_REF=COMMIT,
            RELEASE_TEST_STATE=str(self.state), RELEASE_TEST_LOG=str(self.log),
        )
        for name in ("jq", "tar", "gzip", "sha256sum"):
            command = shutil.which(name, path="/usr/bin:/bin")
            assert command, f"release tests require {name}"
            (self.bin / name).symlink_to(command)
        for name in ("gh", "git", "make"):
            self.write_mock(name, MOCK)

    def write_mock(self, name: str, code: str) -> None:
        command = self.bin / name
        # Use the same selected Python interpreter; no PATH-selected runtime.
        command.write_text(f"#!{sys.executable}\n" + code)
        command.chmod(0o755)

    def run(self, name: str, *, head: str = COMMIT) -> subprocess.CompletedProcess[str]:
        self.state.write_text(json.dumps({"ref": self.ref, "tag": self.tag, "head": head}))
        self.output.write_text("")
        return subprocess.run(
            ["/bin/bash", "-e", "-o", "pipefail", "-c", step_script(name)],
            cwd=self.root, env=self.env, text=True, capture_output=True,
            timeout=5, check=False,
        )

    def calls(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []


MOCK = '''import io, json, os, sys, tarfile
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
state = json.loads(Path(os.environ["RELEASE_TEST_STATE"]).read_text())
with open(os.environ["RELEASE_TEST_LOG"], "a") as log:
    log.write(json.dumps({"name": name, "args": args, "release_ref": os.environ.get("RELEASE_REF")}) + "\\n")
if name == "gh":
    if args[:1] == ["api"]:
        assert len(args) == 2
        if args[1] == "repos/fixture/project/git/ref/tags/" + os.environ["GITHUB_REF_NAME"]:
            print(json.dumps(state["ref"]))
        elif args[1] == "repos/fixture/project/git/tags/" + state["tag"]["sha"]:
            print(json.dumps(state["tag"]))
        else:
            raise SystemExit("unknown mocked API path")
    elif args[:2] == ["release", "create"]:
        pass # Only record publication, never contact GitHub.
    else:
        raise SystemExit("unmocked gh operation")
elif name == "git":
    if args == ["rev-parse", "HEAD"] or args[:2] == ["rev-parse", "--verify"]:
        print(state["head"])
    elif args[:3] == ["show", "-s", "--format=%ct"]:
        print("1")
    elif args[:1] == ["archive"]:
        with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as archive:
            member = tarfile.TarInfo("fixture-source/README")
            content = b"mock source archive\\n"
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    else:
        raise SystemExit("unmocked git operation")
elif name == "make":
    import hashlib
    dist = Path("dist")
    dist.mkdir(exist_ok=True)
    assets = ["pam-u2f-touch-popup-0.0.1-linux-x86_64-musl.tar.gz", "pam-u2f-touch-popup-0.0.1-src.tar.gz"]
    for asset in assets:
        with tarfile.open(dist / asset, "w:gz"):
            pass
    (dist / "SHA256SUMS").write_text("".join(hashlib.sha256((dist / a).read_bytes()).hexdigest() + "  " + a + "\\n" for a in assets))
else:
    raise SystemExit("unmocked command")
'''


def test_verified_tag_is_bound_before_checkout() -> None:
    for event_sha in (COMMIT, TAG_OBJECT):
        with tempfile.TemporaryDirectory() as tmp:
            harness = Harness(Path(tmp))
            harness.env["GITHUB_SHA"] = event_sha
            result = harness.run("Resolve build identity")
            assert result.returncode == 0, result.stderr
            assert harness.output.read_text() == f"commit={COMMIT}\ntag-object={TAG_OBJECT}\n"
            assert len(harness.calls()) == 2, "ref and tag each require one snapshot"
            assert WORKFLOW.index("name: Resolve build identity") < WORKFLOW.index("uses: actions/checkout@")
            assert "ref: ${{ steps.identity.outputs.commit }}" in WORKFLOW
    for event_sha in (OTHER_COMMIT, OTHER_TAG_OBJECT):
        with tempfile.TemporaryDirectory() as tmp:
            harness = Harness(Path(tmp))
            harness.env["GITHUB_SHA"] = event_sha
            assert harness.run("Resolve build identity").returncode != 0
            assert harness.output.read_text() == ""


def test_initial_validation_rejects_mismatched_or_untrusted_tags() -> None:
    changes = [
        ("ref", ("object", "type"), "commit"),
        ("ref", ("object", "sha"), "bad-object"),
        ("tag", ("object", "sha"), OTHER_COMMIT),
        ("tag", ("object", "type"), "tag"),
        ("tag", ("tag",), "v0.0.2"),
        ("tag", ("verification", "verified"), False),
        ("tag", ("verification", "reason"), "expired_key"),
    ]
    for collection, keys, value in changes:
        with tempfile.TemporaryDirectory() as tmp:
            harness = Harness(Path(tmp))
            target = getattr(harness, collection)
            for key in keys[:-1]:
                target = target[key]
            target[keys[-1]] = value
            result = harness.run("Resolve build identity")
            assert result.returncode != 0, (collection, keys, value)
            assert harness.output.read_text() == ""
            assert not any(c["args"][:2] == ["release", "create"] for c in harness.calls())


def test_checkout_must_match_the_verified_commit() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        harness = Harness(Path(tmp))
        assert harness.run("Verify checkout identity").returncode == 0
        assert harness.run("Verify checkout identity", head=OTHER_COMMIT).returncode != 0
        assert WORKFLOW.index("name: Verify checkout identity") < WORKFLOW.index("name: Build and test static binary")


def test_tag_movement_blocks_attestation_and_publication() -> None:
    for step in ("Recheck tag before attestation", "Publish GitHub release"):
        for same_commit in (True, False):
            with tempfile.TemporaryDirectory() as tmp:
                harness = Harness(Path(tmp))
                result = harness.run("Resolve build identity")
                assert result.returncode == 0, result.stderr
                harness.ref["object"]["sha"] = OTHER_TAG_OBJECT
                harness.tag["sha"] = OTHER_TAG_OBJECT
                if not same_commit:
                    harness.tag["object"]["sha"] = OTHER_COMMIT
                result = harness.run(step)
                assert result.returncode != 0, result.stderr
                assert not any(c["args"][:2] == ["release", "create"] for c in harness.calls())
        for field, value in (("sha", OTHER_COMMIT), ("type", "tag")):
            with tempfile.TemporaryDirectory() as tmp:
                harness = Harness(Path(tmp))
                harness.tag["object"][field] = value
                assert harness.run(step).returncode != 0
                assert not any(c["args"][:2] == ["release", "create"] for c in harness.calls())
    assert WORKFLOW.index("name: Recheck tag before attestation") < WORKFLOW.index("name: Attest build provenance")


def test_unchanged_identity_allows_mocked_publication() -> None:
    for event_sha in (COMMIT, TAG_OBJECT):
        with tempfile.TemporaryDirectory() as tmp:
            harness = Harness(Path(tmp))
            harness.env["GITHUB_SHA"] = event_sha
            assert harness.run("Recheck tag before attestation").returncode == 0
            result = harness.run("Publish GitHub release")
            assert result.returncode == 0, result.stderr
            publication = harness.calls()[-1]
            assert publication["args"][:3] == ["release", "create", "v0.0.1"]
            target_index = publication["args"].index("--target")
            assert publication["args"][target_index + 1] == COMMIT


def test_package_step_receives_immutable_commit() -> None:
    for event_name in ("push", "workflow_dispatch"):
        with tempfile.TemporaryDirectory() as tmp:
            harness = Harness(Path(tmp))
            harness.env["GITHUB_EVENT_NAME"] = event_name
            result = harness.run("Package release assets")
            assert result.returncode == 0, result.stderr
            make_call = next(c for c in harness.calls() if c["name"] == "make")
            assert make_call["release_ref"] == COMMIT
            version = "v0.0.1" if event_name == "push" else "v0.0.0"
            assert f"VERSION={version}" in make_call["args"]
            assert "RELEASE_REF: ${{ steps.identity.outputs.commit }}" in step_block("Package release assets")
    with tempfile.TemporaryDirectory() as tmp:
        harness = Harness(Path(tmp))
        assert harness.run("Package release assets", head=OTHER_COMMIT).returncode != 0
        assert not any(c["name"] == "make" for c in harness.calls())


def test_dispatch_is_non_publishing_even_from_a_tag() -> None:
    for ref_type, event_sha in (("branch", COMMIT), ("tag", COMMIT), ("tag", TAG_OBJECT)):
        with tempfile.TemporaryDirectory() as tmp:
            harness = Harness(Path(tmp))
            harness.env.update(GITHUB_EVENT_NAME="workflow_dispatch", GITHUB_REF_TYPE=ref_type, GITHUB_SHA=event_sha)
            result = harness.run("Resolve build identity")
            assert result.returncode == 0, result.stderr
            expected = f"commit={COMMIT}\n"
            if ref_type == "tag":
                expected += f"tag-object={TAG_OBJECT}\n"
            assert harness.output.read_text() == expected
            assert len(harness.calls()) == (2 if ref_type == "tag" else 0)
            for job in ("attest", "publish"):
                block = re.split(r"\n  [a-z]+:\n", WORKFLOW.split(f"  {job}:\n", 1)[1], maxsplit=1)[0]
                assert "if: github.event_name == 'push' && github.ref_type == 'tag'" in block


def test_packaging_snapshots_local_ref_once() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        harness = Harness(Path(tmp))
        # Other packaging programs are real, unprivileged tools operating only
        # within this fixture. Git is still a recorder with an immutable result.
        harness.env["PATH"] = f"{harness.bin}:/usr/bin:/bin"
        harness.env.update(RELEASE_REF="mutable-fixture-ref", BINARY="/bin/true", OUTPUT_DIR=str(harness.root / "dist"))
        harness.state.write_text(json.dumps({"ref": harness.ref, "tag": harness.tag, "head": COMMIT}))
        for file in ("README.md", "LICENSE", "pam-u2f-touch-popup-setup", "pam-u2f-touch-popup.service.in", "pam-u2f-touch-popup-device.service.in", "pam-u2f-touch-popup-device.path"):
            shutil.copyfile(PROJECT_ROOT / file, harness.root / file)
        (harness.root / "examples").mkdir()
        shutil.copyfile(PROJECT_ROOT / "examples/70-pam-u2f-touch-popup-device-gate.rules", harness.root / "examples/70-pam-u2f-touch-popup-device-gate.rules")
        result = subprocess.run(
            ["/bin/bash", str(PROJECT_ROOT / "scripts/package-release.sh"), "v0.0.0", "fixture"],
            cwd=harness.root, env=harness.env, text=True, capture_output=True, timeout=5, check=False,
        )
        assert result.returncode == 0, result.stderr
        git_calls = [c["args"] for c in harness.calls() if c["name"] == "git"]
        assert git_calls[0] == ["rev-parse", "--verify", "mutable-fixture-ref^{commit}"]
        assert git_calls[1] == ["show", "-s", "--format=%ct", COMMIT]
        assert git_calls[2][-1] == COMMIT
        assert len(git_calls) == 3


def main() -> int:
    tests = [
        test_verified_tag_is_bound_before_checkout,
        test_initial_validation_rejects_mismatched_or_untrusted_tags,
        test_checkout_must_match_the_verified_commit,
        test_tag_movement_blocks_attestation_and_publication,
        test_unchanged_identity_allows_mocked_publication,
        test_package_step_receives_immutable_commit,
        test_dispatch_is_non_publishing_even_from_a_tag,
        test_packaging_snapshots_local_ref_once,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
