#!/usr/bin/env bash
# One command: build the sandbox's dependency bundle, create an NVIDIA OpenShell sandbox running
# PrivateMesh's policy, run the injection red-team corpus in all three limits modes, run the egress
# probe, and print one summary table.
#
#   scripts/openshell_run.sh [POLICY_FILE]
#
# POLICY_FILE defaults to deploy/openshell/privatemesh.policy.yaml. Pass
# deploy/openshell/demo.policy.yaml to run under the public demo's narrower policy instead.
#
# This script NEVER installs OpenShell. If `openshell` is not on PATH it refuses with the install
# command from OpenShell's own README (github.com/NVIDIA/OpenShell) and stops; it does not run
# that command for you.
#
# The ONE network step is on the HOST, stated as it happens below: `pip download` of the
# manylinux aarch64 cp312 wheels this project's dependencies need. Nothing inside the sandbox ever
# reaches a package registry — the wheels are uploaded pre-downloaded and unpacked with
# `python3 -m zipfile -e`, because the sandbox's system Python has no pip.
#
# Working shape, learned by running this by hand on 2026-09-21 (docs/NVIDIA_STACK.md, "What
# running it found"):
#   sandbox create --name N --policy F   (no command — `-- <cmd>` ends MainProcessExited)
#   sandbox upload N <local> <dest>
#   sandbox exec -n N --no-tty --timeout S -- ...   < /dev/null   (exec hangs forever without it)
#   sandbox delete N
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
POLICY_FILE="${1:-$REPO_ROOT/deploy/openshell/privatemesh.policy.yaml}"
SANDBOX_NAME="pm-osrun-$$"
ARCHIVE_PATHS=(src redteam deploy scripts tests pyproject.toml)
DEPS=("httpx>=0.27" "fastapi>=0.115" "uvicorn>=0.30" "pydantic>=2.7" "pytest>=8" "respx>=0.21")
MODES=(ask_always within_limits off)

if ! command -v openshell >/dev/null 2>&1; then
    cat >&2 <<'EOF'
openshell_run.sh: `openshell` is not on PATH.

This script never installs it for you. To install OpenShell yourself:

    curl -LsSf https://raw.githubusercontent.com/NVIDIA/OpenShell/main/install.sh | sh

(from github.com/NVIDIA/OpenShell's own README — read it before running it). Once `openshell` is
on PATH, re-run this script with no other changes.
EOF
    exit 1
fi

if [ ! -f "$POLICY_FILE" ]; then
    echo "openshell_run.sh: policy file not found: $POLICY_FILE" >&2
    exit 1
fi

case "$(basename "$POLICY_FILE")" in
    privatemesh.policy.yaml) POLICY_NAME=product ;;
    demo.policy.yaml) POLICY_NAME=demo ;;
    *)
        echo "openshell_run.sh: unrecognised policy file $(basename "$POLICY_FILE") — the egress" \
             "probe's expectations are keyed to privatemesh.policy.yaml or demo.policy.yaml" >&2
        exit 1 ;;
esac

cd "$REPO_ROOT"

# A tracked file named `.env` or anything under `secrets/` must never ride into a sandbox archive.
# Checked against what `git archive` would actually produce, not against the working tree, so a
# gitignored-but-tracked-anyway file cannot slip past this. This is the whole surface: both policy
# files set `include_workdir: false`, so this archive — never the host checkout, where an untracked
# `.env` or `secrets/google_client.json` actually lives — is the only way anything from this
# machine reaches the sandbox.
if git archive HEAD -- "${ARCHIVE_PATHS[@]}" | tar -tf - | grep -Eq '(^|/)\.env$|(^|/)secrets/'; then
    echo "openshell_run.sh: refusing — the archive of HEAD contains a .env file or a secrets/ path." \
         "Remove it from the tree (or from what's tracked) before running this." >&2
    exit 1
fi

WORK="$(mktemp -d "${TMPDIR:-/tmp}/pm-osrun.XXXXXX")"
cleanup() {
    openshell sandbox delete "$SANDBOX_NAME" >/dev/null 2>&1 || true
    rm -rf "$WORK"
}
trap cleanup EXIT

echo "openshell_run.sh: policy=$POLICY_NAME ($POLICY_FILE)" >&2

echo "openshell_run.sh: archiving tracked sources (git archive HEAD -- ${ARCHIVE_PATHS[*]})" >&2
git archive HEAD -- "${ARCHIVE_PATHS[@]}" > "$WORK/privatemesh-src.tar"

echo "openshell_run.sh: [network, host-side] pip download of ${DEPS[*]} (manylinux aarch64 cp312)" >&2
mkdir -p "$WORK/wheels"
python3 -m pip download --only-binary=:all: \
    --platform manylinux2014_aarch64 --platform manylinux_2_17_aarch64 \
    --platform manylinux_2_28_aarch64 --python-version 3.12 --implementation cp \
    -d "$WORK/wheels" "${DEPS[@]}" >&2

echo "openshell_run.sh: creating sandbox '$SANDBOX_NAME'" >&2
openshell sandbox create --name "$SANDBOX_NAME" --policy "$POLICY_FILE" >&2

echo "openshell_run.sh: uploading source archive, wheels, and the egress probe" >&2
openshell sandbox upload "$SANDBOX_NAME" "$WORK/privatemesh-src.tar" privatemesh-src.tar >&2
# No DEST: `sandbox upload` always nests a directory under ITS OWN basename below whatever DEST
# is, so a DEST of "wheels" for a local dir named "wheels" would land at wheels/wheels/*.whl.
# Omitting DEST places it at the workdir directly under that basename — /sandbox/wheels/*.whl.
openshell sandbox upload "$SANDBOX_NAME" "$WORK/wheels" >&2
# No DEST here either: `sandbox upload` treats a DEST that differs from the local file's own
# basename as a DIRECTORY to nest the file under, not a rename — omitting it places the file
# directly at the workdir under its own name.
openshell sandbox upload "$SANDBOX_NAME" "$SCRIPT_DIR/openshell_egress_probe.py" >&2

echo "openshell_run.sh: unpacking inside the sandbox (no network; wheels are already local)" >&2
# /usr/bin/python3 explicitly: bare `python3` resolves to the image's own `.venv` (Python 3.14,
# not the system 3.12 our cp312 wheels target) AND is not the binary either policy file grants
# network access to — see docs/NVIDIA_STACK.md, "What running it found".
openshell sandbox exec -n "$SANDBOX_NAME" --no-tty --timeout 120 -- bash -c '
set -euo pipefail
mkdir -p privatemesh deps
tar -xf privatemesh-src.tar -C privatemesh
for w in wheels/*.whl; do /usr/bin/python3 -m zipfile -e "$w" deps; done
' < /dev/null

# macOS ships bash 3.2 (no associative arrays), so results are kept as one "mode cases held"
# line per mode rather than in a dict.
RESULTS=()
for mode in "${MODES[@]}"; do
    echo "openshell_run.sh: running the red-team corpus, mode=$mode" >&2
    out="$(openshell sandbox exec -n "$SANDBOX_NAME" --no-tty --timeout 180 \
        --env "PYTHONPATH=/sandbox/privatemesh/src:/sandbox/deps" \
        -- /usr/bin/python3 -m privatemesh.cli redteam --mode "$mode" < /dev/null)"
    echo "$out" >&2
    line="$(echo "$out" | grep -m1 'cases,.*held')"
    cases="$(echo "$line" | sed -nE 's/.*[^0-9]([0-9]+) cases,.*/\1/p')"
    held="$(echo "$line" | sed -nE 's/.*, ([0-9]+) held.*/\1/p')"
    RESULTS+=("$mode ${cases:-?} ${held:-?}")
done

echo "openshell_run.sh: running the egress probe (policy=$POLICY_NAME)" >&2
set +e
PROBE_OUT="$(openshell sandbox exec -n "$SANDBOX_NAME" --no-tty --timeout 60 -- \
    /usr/bin/python3 openshell_egress_probe.py --policy "$POLICY_NAME" < /dev/null)"
PROBE_STATUS=$?
set -e
echo "$PROBE_OUT" >&2

# Per-binary enforcement, checked as its OWN top-level `sandbox exec` command — not as a
# subprocess of the (granted) python3 probe above, which inherits its parent's network grant and
# so cannot exercise this check (measured today: docs/NVIDIA_STACK.md, "What running it found").
# Neither policy's `binaries:` list names curl, so this must be blocked at the tunnel.
echo "openshell_run.sh: checking per-binary enforcement (curl, run as its own exec command)" >&2
set +e
CURL_OUT="$(openshell sandbox exec -n "$SANDBOX_NAME" --no-tty --timeout 30 -- \
    curl -sS -o /dev/null -w '%{http_code}' -X POST --max-time 10 \
    -H 'content-type: application/json' -d '{}' \
    https://gmail.googleapis.com/gmail/v1/users/me/messages/send < /dev/null 2>&1)"
CURL_STATUS=$?
set -e
echo "$CURL_OUT" >&2
if [ "$CURL_STATUS" -eq 0 ] && [ "${CURL_OUT##*$'\n'}" != "000" ]; then
    CURL_VERDICT="REACHED ${CURL_OUT##*$'\n'}  <-- SURPRISE (curl is not a granted binary)"
    CURL_FAIL=1
else
    CURL_VERDICT="BLOCKED (curl exit $CURL_STATUS)"
    CURL_FAIL=0
fi

echo
echo "=== summary: policy=$POLICY_NAME sandbox=$SANDBOX_NAME ==="
printf '%-16s %8s %8s\n' "mode" "cases" "held"
FAIL=0
for result in "${RESULTS[@]}"; do
    set -- $result
    mode="$1"; cases="$2"; held="$3"
    printf '%-16s %8s %8s\n' "$mode" "$cases" "$held"
    # An unparsed count ("?") or an empty corpus is a failure, not a match of "?" with "?".
    case "$cases" in ""|0|*[!0-9]*) FAIL=1 ;; esac
    if [ "$cases" != "$held" ]; then FAIL=1; fi
done
probe_line="$(echo "$PROBE_OUT" | tail -1)"
printf '%-16s %s (exit %d)\n' "egress probe" "$probe_line" "$PROBE_STATUS"
printf '%-16s %s\n' "curl (binary)" "$CURL_VERDICT"
echo "==========================================="

if [ "$FAIL" -ne 0 ] || [ "$PROBE_STATUS" -ne 0 ] || [ "$CURL_FAIL" -ne 0 ]; then
    exit 1
fi
