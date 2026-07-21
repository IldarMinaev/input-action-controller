# Maintaining input-action-controller

This page covers dependency updates, project releases, and AUR publication. User installation and configuration steps
remain in the [README](../README.md).

## Dependency updates

Install the Renovate GitHub App for this repository after merging `renovate.json`. Renovate opens the Dependency
Dashboard and checks weekly for updates to pinned GitHub Actions and `requirements-ci.txt`.

Renovate does not merge changes automatically. Review each pull request, wait for CI, and merge it through the normal
branch-protection rules. The GitHub Actions group keeps action revisions pinned to full commit digests.

## Project release

1. Create a release branch from the latest `main`.
2. Update the version in `pyproject.toml`, `PKGBUILD`, `scripts/makepkg`, `scripts/verify-artifacts`, and
   version-specific test fixtures.
3. Run the local checks:

   ```bash
   PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python -m unittest discover -s tests -v
   scripts/public-release verify-tree .
   ./scripts/makepkg -f
   ```

4. Open a pull request and merge it only after CI passes.
5. Create and push a signed annotated tag from the merge commit:

   ```bash
   git switch main
   git pull --ff-only origin main
   git tag -s vX.Y.Z -m "input-action-controller X.Y.Z"
   git push origin vX.Y.Z
   ```

6. Wait for the release workflow, then inspect the GitHub release and its four assets: the wheel, Python source
   distribution, Arch package, and Arch source archive.

Do not reuse a failed release tag after anyone may have fetched it. During private pre-release testing, delete and
recreate a tag only when no external user can depend on it.

## AUR publication

Keep the AUR Git checkout next to the project checkout. After the GitHub release succeeds, update and build the AUR
package from the project repository:

```bash
./scripts/update-aur-package X.Y.Z ../input-action-controller-aur
```

The script downloads the release archive, imports its `PKGBUILD`, configures the AUR source URL and checksum,
regenerates `.SRCINFO`, and runs a clean package build. It does not commit or publish anything.

Review and publish from the AUR checkout:

```bash
cd ../input-action-controller-aur
git diff -- PKGBUILD .SRCINFO
git add PKGBUILD .SRCINFO
git commit -S -m "Update to X.Y.Z"
git push origin master
```

Verify the AUR package page after every push. The AUR repository contains only package metadata; keep the update script
and maintenance documentation in this project.
