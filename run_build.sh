#!/bin/zsh
# POP — unattended build runner. Invoked by launchd at 09:00 Saturday.
# The machine's system timezone is Europe/Lisbon, so launchd local time IS Lisbon time.
# Runs Phases 1-5. Phase 0 is already complete and is only re-validated, never redone.
#
# NOTE ON PERMISSIONS: this invokes `claude -p` with --permission-mode bypassPermissions.
# That is required for a genuinely unattended run — without it the agent stalls on the
# first file write and the build window is wasted. It means the agent can run tools
# without prompting, in this directory, while you are not present. Read this script
# before arming the launchd job.

set -u
PROJECT=/Users/Jacob/Projects/POP
CLAUDE=/Users/Jacob/.local/bin/claude
export PATH="/Users/Jacob/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

cd "$PROJECT" || exit 1
mkdir -p logs
STAMP=$(date +%Y%m%d-%H%M%S)
LOG="logs/build-$STAMP.log"
exec > "$LOG" 2>&1

echo "=== POP unattended build ==="
echo "started: $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "cwd:     $PROJECT"
echo

if [[ -f .build-complete ]]; then
  echo "ABORT: .build-complete sentinel exists — a build already finished."
  echo "Remove it and re-arm the job if you intend to rebuild."
  exit 0
fi

echo "--- precondition: check_env.py ---"
if ! python3 check_env.py; then
  echo
  echo "ABORT: credential check failed. Not starting the build."
  echo "An unattended build on broken credentials burns the window."
  exit 1
fi
echo

echo "--- launching build agent ---"
"$CLAUDE" -p "$(cat <<'PROMPT'
You are executing the unattended POP build. You start with no memory of any prior conversation.

Do this in order:
1. Read /Users/Jacob/Projects/POP/BUILD-PROMPT.md in full. It contains the operating rules, current status, and Phases 1 through 5.
2. Read /Users/Jacob/Projects/POP/pop-build-brief.md in full. It is the complete specification.
3. Execute Phases 1, 2, 3, 4 and 5 in sequence, exactly as BUILD-PROMPT.md specifies.

Hard constraints, repeated here because they matter most:
- Phase 0 is COMPLETE. .env exists and validates. Do not redo it. Do not run auth_spotify.py.
- Never call the live Spotify or Telegram APIs. Use fixtures and mocks for everything in Phases 1-5.
- Do not ask questions. Where the brief is silent, decide, implement, and record the choice and reasoning in DECISIONS.md.
- Do not deviate from the brief's explicit constraints: the negative-delta clamp on resume_point, soft delete on every rejection path, the why-note required before an item is stored, and the allocation gate refusing promotions that breach the remaining allowance. If you disagree with one, implement it as specified and argue the case in DECISIONS.md.
- Report progress to stdout as you go.
- Finish with Phase 5 deliverables, then stop. Do not add features.
PROMPT
)" \
  --permission-mode bypassPermissions \
  --model opus

STATUS=$?
echo
echo "--- build agent exited with status $STATUS ---"
echo "finished: $(date '+%Y-%m-%d %H:%M:%S %Z')"

if [[ $STATUS -eq 0 ]]; then
  date '+%Y-%m-%d %H:%M:%S %Z' > .build-complete
  echo "sentinel .build-complete written"
fi

# One-shot: disarm so it does not fire again next Saturday.
/bin/launchctl bootout "gui/$(id -u)/com.jacob.pop.build" 2>/dev/null && echo "launchd job disarmed"
exit $STATUS
