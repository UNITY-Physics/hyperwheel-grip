#!/usr/bin/env bash
# Bad-network test for the dev Pi: shapes traffic leaving eth0 (the scanner cable,
# and so the Pi's internet) in timed phases, then puts eth0 back as it was.
# The hotspot (wlan0), and SSH over it, are never touched. Dev Pi only.
#
#   sudo bash netem.sh                  every phase, in order (about 22 minutes)
#   sudo bash netem.sh lossy            only the lossy phase (3 minutes), then normal again
#   sudo bash netem.sh lossy:5 normal   a phase with its own length in minutes
#   bash netem.sh --dry-run lossy       show the plan, change nothing (no sudo needed)
#   sudo tc qdisc del dev eth0 root     undo at once, any time
#
# Phases: baseline throttled lossy latency cut slow cut-short normal
set -u
IF=eth0
TC=/usr/sbin/tc
LOG="$(cd "$(dirname "$0")" && pwd)/netem.log"

declare -A MINUTES=([baseline]=2 [throttled]=4 [lossy]=3 [latency]=3 [cut]=3 [slow]=3 [cut-short]=1 [normal]=3)
declare -A WHAT=(
  [baseline]="baseline"
  [throttled]="throttled 2 Mbit/s, 200 ms delay"
  [lossy]="lossy: 2 Mbit/s, 300 ms, 15% loss"
  [latency]="high latency: 1.5 s"
  [cut]="cut: every packet out of eth0 dropped"
  [slow]="very slow: 256 kbit/s, 600 ms"
  [cut-short]="cut again, short"
  [normal]="back to normal"
)
declare -A NETEM=(
  [baseline]=""
  [throttled]="rate 2mbit delay 200ms 50ms"
  [lossy]="rate 2mbit delay 300ms 100ms loss 15%"
  [latency]="delay 1500ms 500ms"
  [cut]="loss 100%"
  [slow]="rate 256kbit delay 600ms"
  [cut-short]="loss 100%"
  [normal]=""
)
ALL=(baseline throttled lossy latency cut slow cut-short normal)

dry=0
if [[ "${1:-}" == "--dry-run" ]]; then dry=1; shift; fi
phases=("$@")
[[ ${#phases[@]} -gt 0 ]] || phases=("${ALL[@]}")

# Check the whole list before changing anything.
for p in "${phases[@]}"; do
  name=${p%%:*}; mins=${p#*:}; [[ "$p" == *:* ]] || mins=${MINUTES[$name]:-}
  [[ -n "${WHAT[$name]:-}" ]] || { echo "Unknown phase '$name'. Phases: ${ALL[*]}"; exit 2; }
  [[ "$mins" =~ ^[0-9]+$ && "$mins" -ge 1 && "$mins" -le 30 ]] || { echo "Minutes for '$name' must be 1 to 30"; exit 2; }
done

if [[ $dry -eq 1 ]]; then
  for p in "${phases[@]}"; do
    name=${p%%:*}; mins=${p#*:}; [[ "$p" == *:* ]] || mins=${MINUTES[$name]}
    echo "PHASE ${WHAT[$name]} for $mins min (${NETEM[$name]})"
  done
  echo "then eth0 back to normal"
  exit 0
fi

[[ ${EUID:-$(id -u)} -eq 0 ]] || { echo "Please run with sudo: sudo bash $0 $*"; exit 1; }
say() { echo "$(date +%H:%M:%S) $*" | tee -a "$LOG"; }
restore() { "$TC" qdisc del dev "$IF" root 2>/dev/null; say "restored: eth0 back to normal"; }
trap 'restore; exit' INT TERM
trap restore EXIT

: > "$LOG"; chmod 644 "$LOG"
say "start; eth0 now: $("$TC" qdisc show dev "$IF" | head -1)"
for p in "${phases[@]}"; do
  name=${p%%:*}; mins=${p#*:}; [[ "$p" == *:* ]] || mins=${MINUTES[$name]}
  opts=${NETEM[$name]}
  if [[ -n "$opts" ]]; then
    # shellcheck disable=SC2086  # the options are separate words on purpose
    "$TC" qdisc replace dev "$IF" root netem $opts || { say "tc failed: $opts"; exit 1; }
  else
    "$TC" qdisc del dev "$IF" root 2>/dev/null
  fi
  say "PHASE ${WHAT[$name]} for $mins min ($opts)"
  sleep $((mins * 60))
done
say "done"
