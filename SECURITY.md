# Security

This is a local experiment. Run it only against projects and agent executables you trust. The
engineer and configured test command can execute code with your account's permissions. A custom
reviewer executable is also trusted code; the read-only sandbox guarantee applies to Codex CLI
sessions started by the harness, not to arbitrary wrappers.

The selected project's `.harness/` directory contains objectives, transcripts, raw model output
and a SQLite database. Keep it private and do not commit or share it. The harness rejects a
symlinked `.harness/` directory and sets its permissions to owner-only. Existing runtime files
created by older versions should be reviewed before sharing a project.

The harness sends repository evidence, including diffs and test output, to the configured reviewer
CLI. That CLI may transmit the content to its provider. Do not run it on sensitive projects unless
that transfer is permitted.

For a vulnerability report, use GitHub's private vulnerability reporting for this repository.
