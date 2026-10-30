#!/usr/bin/env bash
# Real bin/fm-supervision-host.sh (default READY_TIMEOUT), real arm and watcher,
# attended fixture home: a main-only (needs-decision) close passes straight to
# main and the host must leave a VERIFIED successor watcher behind
# (start_successor). The successor watcher's singleton-lock creation
# (ln -s ... state/.watch.lock) is stalled DELAY seconds by a PATH shim - a busy
# machine. No engine turn runs on this path.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/supervision-host-helpers.sh"
DELAY=${DELAY:-28}

test_slow_successor_on_main_only_pass_through() {
  local home first t0 t1 pid
  home=$(make_home slow-successor attended)
  real_ln=$(command -v ln)
  cat > "$home/fakebin/ln" <<SH
#!/bin/sh
for a in "\$@"; do last="\$a"; done
case "\$last" in
  */.watch.lock)
    if [ -e "$home/delay-armed" ] && [ "\$PPID" != "\$(cat "$home/first-watcher")" ] && [ ! -e "$home/delayed.\$PPID" ]; then
      : > "$home/delayed.\$PPID"; echo "\$(date +%s) delaying lock acquisition of watcher \$PPID by ${DELAY}s" >> "$home/shim.log"; sleep $DELAY
    fi ;;
esac
exec $real_ln "\$@"
SH
  chmod +x "$home/fakebin/ln"
  start_host "$home"
  wait_until 150 watcher_live "$home" || fail "the host never started a watcher cycle"
  first=$(cat "$home/state/.watch.lock/pid"); echo "# first watcher pid=$first (host READY_TIMEOUT default; FM_SUPERVISION_HOST_READY_TIMEOUT unset=${FM_SUPERVISION_HOST_READY_TIMEOUT-unset})"
  echo "$first" > "$home/first-watcher"; : > "$home/delay-armed"
  t0=$(date +%s)
  append_status "$home" 'which export format?' needs-decision
  wait_until_deadline 90 host_exited "$home" || fail "the decision close did not reach main"
  t1=$(date +%s)
  echo "# host exited rc=$(cat "$home/host.rc") after $((t1-t0))s"
  echo "# shim: $(cat "$home/shim.log" 2>/dev/null)"
  echo "# host.out:"; sed 's/^/#   /' "$home/host.out"
  echo "# ledger:"; cut -f2- "$home/state/.supervision-host.log" | sed 's/^/#   /'
  if grep -q 'successor-unverified' "$home/state/.supervision-host.log"; then
    fail "host abandoned the slow successor as unverified"
  fi
  watcher_live "$home" || fail "no live successor watcher after the pass-through"
  pid=$(cat "$home/state/.watch.lock/pid")
  [ "$pid" != "$first" ] && [ -e "$home/delayed.$pid" ] || fail "live watcher $pid is not the delayed successor"
  pass "host left verified slow successor watcher pid=$pid (lock acquisition stalled ${DELAY}s)"
}
fm_test_run test_slow_successor_on_main_only_pass_through
