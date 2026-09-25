# ADR 0002 — Secrets hydration: keyring, with an honest container fallback

- Status: accepted
- Date: 2026-09-25

## Context

Users need a way to get credentials into a freshly cloned installation without
committing them. The usual answer is a `.env` file, which then lives forever on
disk, gets copied into backups, and is the most common way a credential leaks.

The requested behaviour was: a `.env` for initial hydration that the tool then
deletes, explaining why, with the user's confirmation.

## Decision

Hydration runs in one direction, with verification:

1. `cartera config hydrate` reads the bootstrap file.
2. Each secret is written to the OS keyring.
3. Each secret is **read back and compared**. Deletion only proceeds if every
   value round-tripped.
4. The tool explains why the file should go, then asks for explicit
   confirmation. `--keep` skips deletion; `--yes` skips the prompt.

When the keyring is unavailable — inside a container there is no D-Bus Secret
Service — hydration **refuses to delete anything** and points at runtime
injection (docker secrets, `systemd LoadCredential`, CI secret stores). Deleting
a mounted file while the value still has to arrive by environment variable would
be security theatre: the file was never what protected the secret.

## Consequences

- Secrets never need to persist in plaintext on the host.
- Containers and hosts take different paths, and the tool states which one it is
  on rather than pretending they are equivalent.
- A `.env` that has been deleted is not reproducible without re-entering the
  value, which is the intended trade: the alternative is a plaintext secret
  sitting on disk.
- `.env.example` remains the documented template and is the only `.env`-named
  file in the repository.

## Open question

A future change may add an encrypted-at-rest store for users who reject both the
keyring and environment variables (e.g. age-encrypted file with a passphrase).
Not implemented yet; no secret material is currently written to disk by this
project.
