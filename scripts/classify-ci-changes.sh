#!/bin/sh
set -eu

usage() {
  echo 'usage: classify-ci-changes.sh --git <base-sha> <head-sha>' >&2
  echo '       classify-ci-changes.sh --files' >&2
  exit 2
}

classify_files() {
  _files="$1"
  if [ -z "$_files" ]; then
    python=true
    web=true
    release_metadata=false
    must_run_main=true
    return
  fi
  python=false
  web=false
  release_metadata=false
  must_run_main=false
  while IFS= read -r path; do
    [ -n "$path" ] || continue
    case "$path" in
      .release-please-manifest.json|VERSION)
        python=true
        web=true
        release_metadata=true
        must_run_main=true
        ;;
      release-please-config.json)
        python=true
        web=true
        release_metadata=true
        ;;
      LICENSE*|NOTICE*|*THIRD_PARTY*|*LICENSES*|.github/workflows/*|scripts/classify-ci-changes.sh|Dockerfile|.dockerignore|compose.yml|vastora/*|tests/browser_qa.mjs|web/vite.config.js|web/index.html|web/package.json|web/package-lock.json)
        python=true
        web=true
        ;;
      *.md|docs/*|.github/ISSUE_TEMPLATE/*|.github/PULL_REQUEST_TEMPLATE.md)
        ;;
      web/*)
        web=true
        ;;
      app/*|tests/*|requirements.txt|scripts/run-python-tests.py)
        python=true
        ;;
      *)
        python=true
        web=true
        ;;
    esac
  done <<EOF
$_files
EOF
}

mode="${1:-}"
python=true
web=true
release_metadata=false
must_run_main=true

case "$mode" in
  --git)
    [ "$#" -eq 3 ] || usage
    merge_base="$(git merge-base "$2" "$3")"
    changed="$(git -c core.quotePath=false diff --no-renames --name-only "$merge_base" "$3")"
    classify_files "$changed"
    ;;
  --files)
    [ "$#" -eq 1 ] || usage
    changed="$(cat)"
    classify_files "$changed"
    ;;
  *)
    usage
    ;;
esac

printf 'python=%s\nweb=%s\nrelease_metadata=%s\nmust_run_main=%s\n' \
  "$python" "$web" "$release_metadata" "$must_run_main"
