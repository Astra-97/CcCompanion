# Project Agent Notes

These notes describe repository workflow preferences for this private
CcCompanion fork. They do not override higher-priority system, developer,
safety, or user instructions in an active Codex session.

## Canonical Repository

- The canonical repository for Astra's private CcCompanion work is
  `https://github.com/Astra-97/CcCompanion`.
- Do not push day-to-day private changes to `CyberSealNull/CcCompanion`.
  Treat that repository only as historical upstream/reference material unless
  Astra explicitly says otherwise.
- Do not force-push or overwrite `main` to reconcile history. If histories
  diverge, integrate with a normal merge/cherry-pick in a temporary branch or
  worktree, review the result, then fast-forward the private canonical `main`.

## Build Rule

- Do not build Android APKs locally on the VPS. Push Android client changes to
  GitHub and use GitHub Actions / GitHub Releases for build artifacts unless
  Astra explicitly overrides this for a one-off emergency.

## Workflow

- Keep secrets, tokens, local configs, chat history, and generated runtime data
  out of git.
- For non-trivial changes, prefer an implementation pass plus an independent
  review pass before pushing or releasing.
- Do not restart `cc-companion.service` while replying to Astra through the
  CcCompanion app. That service owns the active request and final history
  append; restarting it mid-response will produce a recovered interruption
  message instead of the intended assistant reply.
