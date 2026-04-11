# Franklin Support (Internal)

This document is **not shipped with the package**. It lives at the repo
root and contains operational secrets used by franklin support staff to
unblock paying customers. Do not paste its contents into public issue
trackers, screenshots, or chat logs.

## License bypass

`FRANKLIN_LICENSE_BYPASS` is an environment-variable escape hatch for
the license gate in `src/franklin/license.py`. When the env var is set
to the current bypass secret, `ensure_license` allows the command to
proceed and prints a dim warning to stderr. The bypass never touches
disk and is never logged.

**Current bypass secret:**

    ROTATE-ME-ON-RELEASE

**How to use (support scenario — customer is locked out):**

    FRANKLIN_LICENSE_BYPASS=ROTATE-ME-ON-RELEASE franklin push ./runs/<slug> --repo owner/name

**Rotation workflow:**

1. Edit `src/franklin/license.py` and replace the value of
   `_BYPASS_SECRET` with a freshly generated random string.
2. Update the "Current bypass secret" line above to match.
3. Ship a new release. Any older bypass tokens are invalidated the
   moment the new version is installed.

Rotate whenever:

- A value is suspected to have leaked (shared outside the support team,
  committed to a public repo, pasted into a chat log, etc).
- A support staff member leaves the team.
- At least quarterly as routine hygiene.

## License public key

`src/franklin/_license_public_key.pem` is the RS256 public key used to
verify license JWTs. The matching private key is stored in the signing
infrastructure (separate secrets system, not in this repo). To rotate
the public key:

1. Generate a new RSA-2048 keypair in the signing infrastructure.
2. Export the public half as PEM.
3. Replace `src/franklin/_license_public_key.pem` with the new bytes.
4. Re-issue licenses to every active customer, signed with the new
   private key.
5. Ship a release. Old licenses will fail signature verification and
   customers must run `franklin license login` with their new token.

The keypair currently bundled with the repo is a development keypair
generated locally when RUB-75 landed. It **must** be rotated before
any production license is issued.

## Revocation endpoint

`_REVOCATION_ENDPOINT` in `license.py` currently points at a
placeholder (`https://franklin.example.com/licenses/revocations.json`).
When the real endpoint is deployed, update the constant and ship a
release. The endpoint must return JSON in the form:

    { "revoked": ["jti1", "jti2", ...] }

Missing-endpoint and network-failure paths already silently fall back
to the cached revocation list; pointing at a nonexistent URL is safe,
just not useful.
