---
name: cut-a-release
description: Publish a version: bump it everywhere, write the changelog entry, tag from master, and watch the release workflow to the end.
---

# Cut a release

A release is a `v*` tag pushed from `master`. The Release workflow runs the full CI first, then builds, scans, lists the contents, deploys to a throwaway cluster, smoke-tests, and only then publishes the image. A tag that fails publishes nothing and cannot be reused, so check everything first.

1. Run `check-a-change` and `reproduce-and-validate` on `master`'s head.
2. Version strings must all equal the tag (the workflow checks the Helm `appVersion` and `product.__version__`):
   - `app/product/__init__.py` (`__version__`)
   - `app/deploy/helm/oncall-queue/Chart.yaml` (`appVersion`)
   - `app/pyproject.toml` and the `oncall-queue` entry in `app/uv.lock`
   - the `**Version:**` line of `README.md` and each `docs/*.md` header
3. `CHANGELOG.md`: add `## [X.Y.Z] - date` with Added / Changed / Fixed. The workflow greps for `[X.Y.Z]`.
4. Scripts that run on Linux must be executable in git (`tools/deploy.sh`).
5. Run the image scan locally first: build the image, then `docker run --rm -v /var/run/docker.sock:/var/run/docker.sock aquasec/trivy:<pinned> image --severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 <image>`. Fix findings (the Dockerfile applies Debian security updates).
6. Commit, push to `master`, wait for CI to pass, then `git tag vX.Y.Z <commit>` and `git push origin vX.Y.Z`. Do not tag a commit CI has not passed.
7. Watch the Release run to the end. If a step fails, fix the cause and publish the next patch version; do not move or delete a published tag from a script.
8. Create the GitHub Release with the changelog entry as its notes (`gh release create vX.Y.Z --notes-file ...`).
9. Name the release commit in `report/final_report.tex` (Deliverables), rebuild the PDF (`xelatex` twice in `report/`), run `python tools/check_report.py` (main report must end on page 4), and push that as a normal commit.

Never force-push `master` unless you mean to rewrite history, and then only from a fresh mirror clone with a backup, never from a working checkout that holds old branches.
