#!/usr/bin/env bash
# Host tuning for benchmark runs.
#   sudo ./host-tune.sh            apply and show the resulting state
#   sudo ./host-tune.sh --verify   same, then load every core briefly and check the clock stays under the cap
#
# Nothing here survives a reboot: run it after every boot, before any benchmark, and do not change
# any of it between runs you intend to compare.
set -u

FREQ_KHZ="${FREQ_KHZ:-3800000}"   # frequency cap in kHz; also used as the minimum so the clock stays flat
SYS="${SYS_ROOT:-/sys/devices/system/cpu}"   # SYS_ROOT only exists so the script can be tested against a fake tree
warn=0

bad()   { printf 'WARN: %s\n' "$*" >&2; warn=1; }
write() {                          # write VALUE FILE
  local v=$1 f=$2
  [[ -w $f ]] || { bad "cannot write $f"; return 1; }
  echo "$v" > "$f" 2>/dev/null || { bad "writing '$v' to $f failed"; return 1; }
}

if [[ $EUID -ne 0 ]]; then
  echo "run as root: sudo $0 $*" >&2
  exit 1
fi

# 1. SMT off: one hardware thread per core, so roles never share a core
if [[ -e $SYS/smt/control ]]; then
  write off "$SYS/smt/control"
else
  bad "no $SYS/smt/control (SMT cannot be switched here; disable it in the BIOS)"
fi

# CPUs that are online now (cpu0 has no 'online' file)
CPUS=()
for d in "$SYS"/cpu[0-9]*; do
  n=${d##*/cpu}
  if [[ ! -e $d/online || $(<"$d/online") == 1 ]]; then CPUS+=("$n"); fi
done
mapfile -t CPUS < <(printf '%s\n' "${CPUS[@]}" | sort -n)

# 2. performance governor and a flat clock at the cap
for c in "${CPUS[@]}"; do
  p=$SYS/cpu$c/cpufreq
  if [[ ! -d $p ]]; then bad "cpu$c has no cpufreq directory"; continue; fi
  write performance "$p/scaling_governor"
  write "$FREQ_KHZ" "$p/scaling_max_freq"
  write "$FREQ_KHZ" "$p/scaling_min_freq"
done

# 3. shallow idle states only. Waking a core from a deep C-state costs 100+ us and hits some cores more
#    than others, which showed up as a 5x latency gap between two otherwise identical sandboxes.
#    states 0 (POLL) and 1 (C1) stay enabled. DEEP_CSTATES=on skips this step.
if [[ "${DEEP_CSTATES:-off}" != "on" ]]; then
  for c in "${CPUS[@]}"; do
    for s in "$SYS/cpu$c"/cpuidle/state[2-9]*; do
      [[ -e $s/disable ]] && write 1 "$s/disable"
    done
  done
fi

# 4. boost off (the file only exists with some drivers)
if [[ -e $SYS/cpufreq/boost ]]; then write 0 "$SYS/cpufreq/boost"; fi

# 5. connection tracking. The TTFB benchmark creates one tracked flow per connection (125k+ per run at N=50);
#    a full table silently drops new connections, which shows up as timeouts. CT_MAX=0 skips this step.
CT_MAX="${CT_MAX:-2097152}"
CT=/proc/sys/net/netfilter/nf_conntrack_max
HS=/sys/module/nf_conntrack/parameters/hashsize
if (( CT_MAX > 0 )); then
  [[ -e $CT ]] || modprobe nf_conntrack 2>/dev/null
  if [[ -e $CT ]]; then
    (( $(<"$CT") < CT_MAX )) && write "$CT_MAX" "$CT"
    [[ -w $HS ]] && (( $(<"$HS") < CT_MAX / 4 )) && write "$((CT_MAX / 4))" "$HS"
  else
    echo "note: nf_conntrack is not loaded, nothing to raise"
  fi
fi

# ---- report ----
echo
echo "== state =="
echo "SMT active:  $(cat "$SYS/smt/active" 2>/dev/null || echo n/a)   (0 = off)"
echo "online cpus: ${CPUS[*]}"
echo "driver:      $(cat "$SYS/cpu0/cpufreq/scaling_driver" 2>/dev/null || echo n/a)"
echo "boost:       $(cat "$SYS/cpufreq/boost" 2>/dev/null || echo n/a)   (0 = off)"
for c in "${CPUS[@]}"; do
  p=$SYS/cpu$c/cpufreq
  printf 'cpu%-3s governor=%-12s min=%s max=%s kHz\n' "$c" \
    "$(cat "$p/scaling_governor" 2>/dev/null || echo n/a)" \
    "$(cat "$p/scaling_min_freq" 2>/dev/null || echo n/a)" \
    "$(cat "$p/scaling_max_freq" 2>/dev/null || echo n/a)"
done
echo "conntrack:   max=$(cat /proc/sys/net/netfilter/nf_conntrack_max 2>/dev/null || echo n/a) buckets=$(cat "$HS" 2>/dev/null || echo n/a)"
for c in "${CPUS[@]}"; do
  line=""
  for s in "$SYS/cpu$c"/cpuidle/state*; do
    [[ -e $s/name ]] || continue
    st=on; [[ $(cat "$s/disable" 2>/dev/null) == 1 ]] && st=off
    line+="$(cat "$s/name")=$st($(cat "$s/latency" 2>/dev/null)us) "
  done
  printf 'cpu%-3s idle states: %s\n' "$c" "${line:-n/a (none exposed)}"
done
[[ $(cat "$SYS/smt/active" 2>/dev/null || echo 0) == 1 ]] && bad "SMT is still active"

# ---- optional: check the clock under load ----
if [[ "${1:-}" == "--verify" ]]; then
  echo
  echo "== clock under load (cap ${FREQ_KHZ} kHz, 4 s) =="
  pids=()
  for c in "${CPUS[@]}"; do
    taskset -c "$c" sh -c 'while :; do :; done' &
    pids+=($!)
  done
  sleep 4
  awk -v cap="$FREQ_KHZ" '
    /^processor/ {p=$3}
    /^cpu MHz/   {mhz=$4; lim=cap/1000*1.02; st=(mhz>lim)?"ABOVE CAP":"ok";
                  printf "cpu%-3s %8.1f MHz  %s\n", p, mhz, st; if (mhz>lim) over=1}
    END {exit over}' /proc/cpuinfo || bad "a core runs above the cap: switch Core Performance Boost off in the BIOS"
  kill "${pids[@]}" 2>/dev/null
  wait 2>/dev/null
fi

echo
if (( warn )); then
  echo "Finished WITH WARNINGS: fix them before benchmarking."
  exit 1
fi
echo "Host tuned. Settings last until reboot."

