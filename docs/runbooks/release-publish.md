# Publishing a release to GitLab and GitHub

The repository is public. Before any commit leaves for GitLab or GitHub,
[`ci/publish_check.py`](../../ci/publish_check.py) (PB-REL-01) checks the
range of commits to send:

1. **identities**: the author *and* the committer of each commit are in
   [`ci/publish-identities.txt`](../../ci/publish-identities.txt), or in a
   private list (`private/publish-identities.txt`,
   `PLUGBOARD_PUBLISH_IDENTITIES`, `--identities FILE`);
2. **secrets**: gitleaks on the same range (config `.gitleaks.toml`);
3. **private terms**: none of the terms of `private/scan-terms.txt` in the
   lines the commits add, their messages, the names of the files they touch,
   or their identities.

A finding names the commit, the file, the line and the term's number (its
line in the terms file), never the term itself; a path that holds a term is
masked, and a refused identity is masked unless `--show-identities` is given
(local run only). Exit codes: `0` publishable, `1` finding, `2` the check
could not run (no terms file, no gitleaks, unknown reference).

This is a planned procedure: its steps replace the usual runbook sections.
The send commands are run by the operator only, from a machine allowed to
push; nothing here sends anything by itself.

## 1. Prepare the private files

```bash
cd <checkout>
cp private/scan-terms.txt.example private/scan-terms.txt
$EDITOR private/scan-terms.txt
git check-ignore -q private/scan-terms.txt && echo "ignored by git"
```

- One term per line, case-insensitive; blank lines and lines starting with
  `#` are ignored. A line is an exact text; a line starting with `re:` is a
  Python regular expression. `scripts/pre-push` reads the same file, every
  line as exact text.
- Put in it what must never be public: client names, internal domains and
  host names, private network addresses, personal names and e-mails.
- The file is never committed: `.gitignore` ignores `private/*` except the
  `*.example` files. Expected: `ignored by git`.
- Other identities allowed to author or commit:
  `private/publish-identities.txt`, one `Name <email>` per line.

Where the check looks for the terms, in this order: `--terms FILE`,
`PLUGBOARD_SCAN_TERMS`, `private/scan-terms.txt` at the root of the checked
repository, then the `private/` folder of the source repository: the one
beside `CI_GIT_DIR` in the CI, beside the common `.git` otherwise (a git
worktree reads the main checkout's file). No terms file, or one with no term:
the check fails with code `2`. `--allow-missing-terms` turns that into a
warning (`termes privés NON vérifiés`); never use it for a publication.

## 2. Check the range to send

Fetch both forges, then check, for each one, the commits it does not have
yet (here `gitlab` and `github` name the two remotes, `release/<version>` the
branch to publish):

```bash
git fetch gitlab && git fetch github
python3 ci/publish_check.py --range gitlab/main..release/<version>; echo "exit $?"
python3 ci/publish_check.py --range github/main..release/<version>; echo "exit $?"
# Expected: "publication possible : <n> commit(s) contrôlé(s)", then exit 0
```

gitleaks must be on the `PATH` (or `--gitleaks PATH`). `--base main` checks
merge-base(HEAD, main)..HEAD, as the CI does; on the base itself (empty
range) the whole tree of HEAD is scanned. `--tree` scans the whole tree of
HEAD, for a first publication.

## 3. When something is found

Do not send. Then, by finding:

| Finding | What to do |
|---|---|
| `identité non autorisée` | Rerun with `--show-identities` locally to see it. Rewrite the unpublished commits with the right author and committer (`git rebase -r <published base> --exec 'git commit --amend --no-edit --reset-author'` with the right `user.name`/`user.email`), or add the identity to `private/publish-identities.txt` if it may be public |
| `terme n°<k>` in a file or a message | Read line `<k>`'s term in your private file, remove it from the unpublished commits (rewrite them: a later commit that deletes it leaves it in the history), then check again |
| `terme n°<k>` in a file name | Rename the file in the commit that adds it |
| gitleaks | Remove the secret from the unpublished commits **and revoke it**: it has left the machine that made it |
| a finding in commits already published | Treat it as an incident: revoke what can be revoked, then ask the forge to purge it; rewriting a published branch needs the owner's agreement |

Never rewrite commits already on GitLab or GitHub to hide a finding without
that agreement.

## 4. CI green on the exact commit

The local CI runs the same check (step `publish-check`, after `gitleaks`,
`--base main`) on the exact tree of the commit. Its throwaway export has no
`private/` folder: the step reads `private/scan-terms.txt` of the source
repository, beside `CI_GIT_DIR`; without it the step fails. It does not rerun
gitleaks (`--skip-gitleaks`): the step before scanned the same range.

```bash
make ci                                   # or, on the UAT: ci lancer plugboard <sha>
```

Send only a commit whose CI is green.

## 5. Send: GitLab, then GitHub

GitLab is the canonical forge: it receives the release first. The commands
below are run by the operator; they are described here, never run by the
check.

```bash
git push gitlab release/<version>:main
git push gitlab v<version>                # the release tag, once main is accepted
git ls-remote gitlab refs/heads/main      # Expected: the checked SHA
git push github release/<version>:main
git push github v<version>
git ls-remote github refs/heads/main      # Expected: the same SHA
```

`make install-hooks` installs [`scripts/pre-push`](../../scripts/pre-push):
gitleaks and the private terms again, on each push, as a last barrier. With
`PLUGBOARD_REQUIRE_TERMS=1` it refuses a push without a terms file.

## 6. After the send

- Both forges show the same SHA for `main` and the tag.
- Keep the check's output with the release notes (it holds no term and no
  refused identity).
