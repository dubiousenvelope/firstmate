#!/usr/bin/env bash
# Drive the real bin/fm-watch-arm.sh (with its default confirm budget, no
# FM_ARM_CONFIRM_TIMEOUT) in a disposable lab home while the watcher's first
# liveness beat is delayed by DELAY seconds (PATH shim on touch(1) that stalls
# only the first write of state/.last-watcher-beat, modelling a busy machine).
# Usage: arm-slow-first-beat.sh <bin-dir> <delay-seconds> <label>
set -u
BIN=$1 DELAY=$2 LABEL=$3
WT={run-worktree}
LAB=$(cd "$(mktemp -d "${TMPDIR:-/tmp}/fm-lab.XXXXXX")" && pwd -P)
"$WT/bin/fm-lab-home.sh" create "$LAB" >/dev/null
SHIM="$LAB/shim"; mkdir -p "$SHIM"
REAL_TOUCH=$(command -v touch)
cat > "$SHIM/touch" <<SH
#!/bin/sh
for a in "\$@"; do
  case "\$a" in
    */.last-watcher-beat)
      if [ ! -e "$LAB/.first-beat-delayed" ]; then
        : > "$LAB/.first-beat-delayed"; sleep $DELAY
      fi ;;
  esac
done
exec $REAL_TOUCH "\$@"
SH
chmod +x "$SHIM/touch"
unset FM_ARM_CONFIRM_TIMEOUT NO_MISTAKES_GATE FM_GATE_REFUSE_BYPASS FM_ROOT_OVERRIDE FM_STATE_OVERRIDE FM_DATA_OVERRIDE FM_CONFIG_OVERRIDE FM_PROJECTS_OVERRIDE
start=$(date +%s)
out="$LAB/arm.out"
PATH="$SHIM:$PATH" FM_HOME="$LAB" FM_CHECK_INTERVAL=999999 FM_HEARTBEAT=999999 \
  "$BIN/fm-watch-arm.sh" > "$out" 2>&1 &
arm=$!
# The arm stays attached on success; stop polling once it reports.
while kill -0 "$arm" 2>/dev/null && ! grep -Eq '^watcher: (started|attached|FAILED)' "$out"; do sleep 0.2; done
end=$(date +%s)
echo "== [$LABEL] bin=$BIN first-beat-delay=${DELAY}s elapsed=$((end-start))s"
sed 's/^/   arm> /' "$out"
# Teardown: stop arm and watcher of this lab only.
kill "$arm" 2>/dev/null
wpid=$(cat "$LAB/state/.watch.lock/pid" 2>/dev/null || true)
[ -n "$wpid" ] && kill "$wpid" 2>/dev/null
sleep 1
[ -n "$wpid" ] && kill -9 "$wpid" 2>/dev/null
wait 2>/dev/null
rm -rf "$LAB"
