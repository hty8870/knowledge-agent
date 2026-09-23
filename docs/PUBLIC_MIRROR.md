# Public mirror contract

The private repository is the implementation source. The public repository is
a reviewed projection of one exact private commit, not an independently edited
release line.

`public-mirror.json` is the only generated file: it records the source private
commit, runtime version, and the hashes of the projection policy and the public
file list. `scripts/verify_public_mirror.py` re-renders the expected public
tree from that exact private commit and checks:

- the private worktree is clean and its HEAD equals `source_private_commit`;
- every public file is byte-identical to the deterministic projection of its
  private source — `packaging/public-mirror/policy.json` `mappings` declares
  the only allowed renames, and every other public path comes from the
  same-named private file;
- every tracked private path is classified — allowlisted for publication,
  delivery-ignored as internal material, or git-ignored as a local artifact —
  so nothing is silently dropped or leaked;
- `public-mirror.json` equals the manifest the generator itself would emit.

## Sync procedure

1. Commit and test the private source. Do not certify a dirty private tree.
2. Regenerate the public tree with
   `python scripts/build_public_mirror.py apply --public-root <public-worktree>`
   from a clean public branch. Never hand-edit the generated tree — fix the
   private source, the policy, or the classification instead.
3. Review the generated diff only.
4. `source_private_commit` is set to the exact private commit by the generator;
   `runtime_version` must equal `WEB_API_VERSION` in both repositories.
5. Run:

   ```powershell
   python scripts/verify_public_mirror.py `
     --private-root C:\path\to\biodata-agent-private `
     --public-root C:\path\to\biodata-agent
   ```

6. Run both full quality profiles and delivery scans before publishing.

Public CI validates the manifest and public-only exclusions without private
source access. Full cross-repository certification is a release-maintainer step
because CI must not receive private source access.
