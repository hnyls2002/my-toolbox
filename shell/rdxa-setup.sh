#!/usr/bin/env bash
# One-shot setup for a headless DGX Spark node: keep it reachable over
# Tailscale, and size its swap for unified-memory model loading.
#
# Written for DGX Spark (Ubuntu 24.04 + GNOME, WiFi-only), but nothing here is
# Spark-specific. Two failures it prevents:
#
#   going dark    a desktop-profile machine with no monitor attached goes idle ->
#                 GNOME suspends it -> NIC powers down -> the node shows up as
#                 "offline" in Tailscale and cannot be woken remotely.
#   dead on boot  the boot-time WiFi association hits a 4WAY_HANDSHAKE_TIMEOUT,
#                 which NetworkManager misreads as a wrong PSK. It asks a secret
#                 agent for a new one; a headless node has no agent, so the
#                 activation fails with 'no-secrets' and NM stops retrying
#                 entirely -- the node stays unreachable until someone logs into
#                 GNOME, whose NetworkAgent registration finally unblocks it.
#                 The watchdog's re-dial is what breaks that dependency.
#
# Six independent steps, each idempotent and individually revertable:
#
#   suspend   mask sleep/suspend/hibernate targets, disable GNOME idle suspend
#   tailscale enable at boot, Restart=always (SSH takeover is opt-in)
#   wifi      disable driver power-save, retry autoconnect forever
#   watchdog  systemd timer that re-dials WiFi / restarts tailscaled when down
#   swap      grow /swap.img to 256G (model-load spikes on unified memory)
#   motd      login banner: machine identity + the "work lives under /data" rule
#
# Usage:
#   ./rdxa-setup.sh              # report only, changes nothing (default)
#   ./rdxa-setup.sh --apply
#   ./rdxa-setup.sh --apply --only suspend,wifi
#   ./rdxa-setup.sh --apply --tailscale-ssh   # also turn on Tailscale SSH
#   ./rdxa-setup.sh --revert --dry-run   # preview the undo
#   ./rdxa-setup.sh --revert
#
# Run it directly on the node. Needs sudo (will prompt).

set -uo pipefail

MODE=check
DRY=0
ONLY=""
WANT_TSSSH=0
STEPS=(suspend tailscale wifi watchdog swap motd)

SLEEP_TARGETS=(sleep.target suspend.target hibernate.target hybrid-sleep.target)
TS_DROPIN=/etc/systemd/system/tailscaled.service.d/override.conf
WD_SCRIPT=/usr/local/bin/net-watchdog.sh
WD_SERVICE=/etc/systemd/system/net-watchdog.service
WD_TIMER=/etc/systemd/system/net-watchdog.timer
SWAP_FILE=/swap.img
SWAP_TARGET_G=256   # apply target
SWAP_DEFAULT_G=16   # Ubuntu installer default for a 64-256G RAM box; revert target
MOTD_DIR=/etc/update-motd.d

RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; DIM=$'\033[2m'; RST=$'\033[0m'
CHANGED=0
FAILED=0

# Print the header docstring: every comment line after the shebang, stopping at
# the first line of actual code. No hard-coded line numbers to drift.
usage() { awk 'NR==1{next} /^#/{sub(/^# ?/, ""); print; next} {exit}' "$0"; exit 0; }

while [ $# -gt 0 ]; do
    case "$1" in
        --apply)   MODE=apply ;;
        --revert)  MODE=revert ;;
        --check)   MODE=check ;;
        --dry-run) DRY=1 ;;
        --only)    ONLY="${2:-}"; shift ;;
        --tailscale-ssh) WANT_TSSSH=1 ;;
        -h|--help) usage ;;
        *) echo "unknown arg: $1 (try --help)" >&2; exit 2 ;;
    esac
    shift
done

# True when nothing should actually be executed: plain check, or any mode
# paired with --dry-run. Keeps --revert previewable, which matters because
# revert is what you reach for when something is already broken.
dry() { [ "$MODE" = check ] || [ "$DRY" = 1 ]; }

wanted() {
    [ -z "$ONLY" ] && return 0
    case ",$ONLY," in *",$1,"*) return 0 ;; *) return 1 ;; esac
}

section() { printf '\n%s== %s ==%s\n' "$YLW" "$1" "$RST"; }
ok()      { printf '  %s[ ok ]%s %s\n' "$GRN" "$RST" "$1"; }
did()     { printf '  %s[ set ]%s %s\n' "$GRN" "$RST" "$1"; CHANGED=$((CHANGED + 1)); }
todo()    { printf '  %s[todo ]%s %s\n' "$YLW" "$RST" "$1"; CHANGED=$((CHANGED + 1)); }
fail()    { printf '  %s[fail ]%s %s\n' "$RED" "$RST" "$1"; FAILED=$((FAILED + 1)); }
note()    { printf '  %s%s%s\n' "$DIM" "$1" "$RST"; }

# Run a privileged command, or just announce it in check mode.
# apply/revert -> execute; check -> print what would happen.
run() {
    local desc="$1"; shift
    if dry; then
        todo "$desc"
        note "    $*"
        return 0
    fi
    local out
    if out=$(sudo "$@" 2>&1); then
        did "$desc"
    else
        fail "$desc"
        note "    cmd: $*"
        [ -n "$out" ] && note "    err: ${out%%$'\n'*}"
    fi
}

# sudo tee wrapper: write heredoc content to a root-owned path.
write_file() {
    local desc="$1" path="$2" content="$3"
    # plain cat: these live at 0644/0755, and check mode must never need sudo
    if [ -f "$path" ] && [ "$(cat "$path" 2>/dev/null)" = "$content" ]; then
        ok "$desc (already current)"
        return 0
    fi
    if dry; then
        todo "$desc"
        note "    write $path"
        return 0
    fi
    sudo mkdir -p "$(dirname "$path")" 2>/dev/null
    if printf '%s\n' "$content" | sudo tee "$path" >/dev/null; then
        did "$desc"
    else
        fail "$desc"
    fi
}

active_wifi_con() {
    nmcli -t -f TYPE,NAME con show --active 2>/dev/null \
        | awk -F: '$1=="802-11-wireless"{print $2; exit}'
}

# Prefer the live connection, fall back to any saved WiFi profile: this script is
# most useful exactly when WiFi is down, so it must not require WiFi to be up.
wifi_con() {
    local con
    con=$(active_wifi_con)
    if [ -n "$con" ]; then
        printf '%s\n' "$con"
        return
    fi
    nmcli -t -f TYPE,NAME con show 2>/dev/null \
        | awk -F: '$1=="802-11-wireless"{print $2; exit}'
}

# Not restricted to connected: power_save is readable on an idle device too.
wifi_dev() {
    nmcli -t -f DEVICE,TYPE dev status 2>/dev/null \
        | awk -F: '$2=="wifi"{print $1; exit}'
}

# ---------------------------------------------------------------- suspend ---
step_suspend() {
    section "suspend  (the usual cause of a headless box going dark)"

    local masked=1
    for t in "${SLEEP_TARGETS[@]}"; do
        [ "$(systemctl is-enabled "$t" 2>/dev/null)" = masked ] || masked=0
    done

    if [ "$MODE" = revert ]; then
        if [ "$masked" = 1 ]; then
            run "unmask sleep targets" systemctl unmask "${SLEEP_TARGETS[@]}"
        else
            ok "sleep targets not masked"
        fi
    elif [ "$masked" = 1 ]; then
        ok "sleep/suspend/hibernate targets masked"
    else
        run "mask sleep targets" systemctl mask "${SLEEP_TARGETS[@]}"
    fi

    if ! command -v gsettings >/dev/null 2>&1; then
        note "gsettings absent -- no GNOME session to configure"
        return
    fi

    local want_type want_idle
    if [ "$MODE" = revert ]; then want_type=suspend; want_idle=300
    else want_type=nothing; want_idle=0
    fi

    local cur
    cur=$(gsettings get org.gnome.settings-daemon.plugins.power \
        sleep-inactive-ac-type 2>/dev/null | tr -d "'")
    if [ -z "$cur" ]; then
        note "GNOME power schema not readable from this session"
    elif [ "$cur" = "$want_type" ]; then
        ok "GNOME sleep-inactive-ac-type=$cur"
    elif dry; then
        todo "GNOME sleep-inactive-ac-type: $cur -> $want_type"
    else
        gsettings set org.gnome.settings-daemon.plugins.power \
            sleep-inactive-ac-type "$want_type" 2>/dev/null \
            && did "GNOME sleep-inactive-ac-type: $cur -> $want_type" \
            || fail "GNOME sleep-inactive-ac-type"
        gsettings set org.gnome.desktop.session idle-delay "$want_idle" 2>/dev/null \
            && did "GNOME idle-delay=$want_idle"
    fi

    note "masking is the hard stop; gsettings only keeps GNOME from retrying"
}

# -------------------------------------------------------------- tailscale ---
step_tailscale() {
    section "tailscale  (start at boot, restart on any exit, SSH fallback)"

    if ! command -v tailscale >/dev/null 2>&1; then
        fail "tailscale not installed"
        return
    fi

    if [ "$MODE" = revert ]; then
        [ -f "$TS_DROPIN" ] && run "remove Restart=always drop-in" rm -f "$TS_DROPIN"
        run "reload systemd" systemctl daemon-reload
        ok "leaving tailscaled enabled and --ssh untouched (revert those by hand)"
        return
    fi

    if [ "$(systemctl is-enabled tailscaled 2>/dev/null)" = enabled ]; then
        ok "tailscaled enabled at boot"
    else
        run "enable tailscaled" systemctl enable --now tailscaled
    fi

    # Default unit ships Restart=on-failure, which does NOT cover a clean exit.
    if [ "$(systemctl show tailscaled -p Restart --value 2>/dev/null)" = always ]; then
        ok "tailscaled Restart=always"
    else
        write_file "tailscaled Restart=always drop-in" "$TS_DROPIN" \
"[Service]
Restart=always
RestartSec=5"
        run "reload systemd" systemctl daemon-reload
        run "restart tailscaled" systemctl restart tailscaled
    fi

    # Tailscale SSH does not go through sshd -- it survives a broken sshd
    # config, an occupied port 22, and a wedged PAM stack.
    local runssh
    runssh=$(tailscale debug prefs 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin).get("RunSSH"))' 2>/dev/null)
    if [ "$runssh" = True ]; then
        ok "Tailscale SSH on"
    elif [ "$WANT_TSSSH" = 1 ]; then
        # --accept-risk is mandatory: enabling this over a tailnet ssh session
        # drops that session as tailscaled takes over port 22.
        run "enable Tailscale SSH" \
            tailscale set --ssh=true --accept-risk=lose-ssh
        note "reconnect may need an 'ssh' rule in the tailnet ACL"
    else
        note "Tailscale SSH off -- opt in with --tailscale-ssh"
        note "  it reroutes tailnet port 22 from sshd to tailscaled; with no"
        note "  'ssh' rule in the tailnet ACL that locks you out over tailscale"
    fi

    if [ "$(systemctl is-enabled NetworkManager-wait-online.service 2>/dev/null)" = enabled ]; then
        ok "NetworkManager-wait-online enabled"
    else
        run "enable NetworkManager-wait-online" \
            systemctl enable NetworkManager-wait-online.service
    fi
}

# ------------------------------------------------------------------- wifi ---
step_wifi() {
    section "wifi  (power-save is on by default and will drop inbound reachability)"

    local con dev
    con=$(wifi_con)
    dev=$(wifi_dev)

    if [ -z "$con" ]; then
        note "no saved WiFi connection -- skipping (wired node?)"
        return
    fi
    note "connection '$con' on ${dev:-unknown device}"
    [ "$con" = "$(active_wifi_con)" ] || note "  (not currently active -- settings land on next connect)"

    local want_ps want_retries
    if [ "$MODE" = revert ]; then want_ps=0; want_retries=-1
    else want_ps=2; want_retries=0
    fi

    # NetworkManager 0 means "leave the driver default alone", and the driver
    # default is power-save ON. Only 2 actually disables it.
    # nmcli takes a number on write but reports the enum name on read
    # (0=default, 1=ignore, 2=disable, 3=enable), so compare against both.
    local cur_ps want_name
    cur_ps=$(nmcli -g 802-11-wireless.powersave con show "$con" 2>/dev/null | awk '{print $1}')
    case "$want_ps" in 2) want_name=disable ;; *) want_name=default ;; esac
    if [ "$cur_ps" = "$want_ps" ] || [ "$cur_ps" = "$want_name" ]; then
        ok "802-11-wireless.powersave=$cur_ps"
    else
        run "powersave $cur_ps -> $want_name" \
            nmcli con mod "$con" 802-11-wireless.powersave "$want_ps"
    fi

    local cur_re
    cur_re=$(nmcli -g connection.autoconnect-retries con show "$con" 2>/dev/null | awk '{print $1}')
    if [ "$cur_re" = "$want_retries" ]; then
        ok "autoconnect-retries=$cur_re"
    else
        run "autoconnect-retries $cur_re -> $want_retries (0 = forever)" \
            nmcli con mod "$con" connection.autoconnect-retries "$want_retries"
    fi

    if [ -n "$dev" ] && command -v iw >/dev/null 2>&1; then
        local live
        live=$(iw dev "$dev" get power_save 2>/dev/null | awk '{print $NF}')
        if ! dry && [ "$live" = on ] && [ "$want_ps" = 2 ]; then
            if [ -n "${SSH_CONNECTION:-}" ]; then
                # Bouncing the only uplink would drop this very session and
                # SIGHUP the script mid-run. The setting is already saved; it
                # lands on the next reconnect.
                note "over ssh: not bouncing the link (would kill this session)"
                note "powersave applies on next reconnect/reboot, or locally run:"
                note "  sudo nmcli con down '$con' && sudo nmcli con up '$con'"
            else
                note "driver still reports power_save on; bouncing the connection"
                sudo nmcli con down "$con" >/dev/null 2>&1
                sudo nmcli con up "$con" >/dev/null 2>&1
                sleep 3
                live=$(iw dev "$dev" get power_save 2>/dev/null | awk '{print $NF}')
            fi
        fi
        [ "$live" = off ] && ok "driver power_save=off" || note "driver power_save=$live"
    fi
}

# --------------------------------------------------------------- watchdog ---
step_watchdog() {
    section "watchdog  (Restart=always covers a dead process, not a dead link)"

    if [ "$MODE" = revert ]; then
        run "stop watchdog timer" systemctl disable --now net-watchdog.timer
        run "remove watchdog units" rm -f "$WD_TIMER" "$WD_SERVICE" "$WD_SCRIPT"
        run "reload systemd" systemctl daemon-reload
        return
    fi

    write_file "watchdog script" "$WD_SCRIPT" \
'#!/bin/bash
# Re-dial the network and/or tailscaled when connectivity is gone.
# Installed by my-toolbox/shell/rdxa-setup.sh
CON=$(nmcli -t -f TYPE,NAME con show 2>/dev/null | awk -F: "\$1==\"802-11-wireless\"{print \$2; exit}")
STATE=$(nmcli -t -f TYPE,STATE dev status 2>/dev/null | awk -F: "\$1==\"wifi\"{print \$2; exit}")
GW=$(ip route | awk "/^default/{print \$3; exit}")

# Three shapes of down, one fix. Testing reachability alone is not enough: a link
# that never came up has no default route, so a gateway ping cannot even be
# attempted -- that is the boot-time no-secrets deadlock, and the case where the
# re-dial matters most. nmcli con up is what clears NM blocked autoconnect flag.
if [ -n "$CON" ] && { [ "$STATE" != connected ] || [ -z "$GW" ] \
        || ! ping -c2 -W3 "$GW" >/dev/null 2>&1; }; then
    nmcli con up "$CON" >/dev/null 2>&1
    sleep 10
fi
tailscale status --json 2>/dev/null | grep -q "\"BackendState\":\"Running\"" \
    || systemctl restart tailscaled'

    if [ -x "$WD_SCRIPT" ]; then
        ok "watchdog script executable"
    else
        run "chmod +x watchdog" chmod +x "$WD_SCRIPT"
    fi

    write_file "watchdog service" "$WD_SERVICE" \
"[Unit]
Description=Recover WiFi and Tailscale connectivity
After=network.target

[Service]
Type=oneshot
ExecStart=$WD_SCRIPT"

    write_file "watchdog timer" "$WD_TIMER" \
"[Unit]
Description=Run the connectivity watchdog every 2 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=2min

[Install]
WantedBy=timers.target"

    if [ "$(systemctl is-active net-watchdog.timer 2>/dev/null)" = active ]; then
        ok "net-watchdog.timer running"
    else
        run "reload systemd" systemctl daemon-reload
        run "enable watchdog timer" systemctl enable --now net-watchdog.timer
    fi
}

# ------------------------------------------------------------------- swap ---
step_swap() {
    section "swap  (installer default 16G; unified-memory model loads need room to spill)"

    local want_g
    if [ "$MODE" = revert ]; then want_g=$SWAP_DEFAULT_G; else want_g=$SWAP_TARGET_G; fi

    if [ ! -f "$SWAP_FILE" ]; then
        fail "$SWAP_FILE not found -- expected the stock Ubuntu swap file"
        return
    fi

    local cur_g
    cur_g=$(( $(stat -c %s "$SWAP_FILE" 2>/dev/null || echo 0) / 1073741824 ))
    if [ "$cur_g" -eq "$want_g" ]; then
        ok "$SWAP_FILE already ${want_g}G"
    else
        # swapoff pulls everything in swap back into RAM; refuse when it
        # cannot fit, instead of OOM-killing whatever is running.
        local used_m avail_m
        used_m=$(awk '/SwapTotal/{t=$2} /SwapFree/{f=$2} END{print int((t-f)/1024)}' /proc/meminfo)
        avail_m=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo)
        if ! dry && [ "$used_m" -gt "$avail_m" ]; then
            fail "swap in use (${used_m}M) exceeds available RAM (${avail_m}M) -- retry when idle"
            return
        fi
        run "resize $SWAP_FILE ${cur_g}G -> ${want_g}G" \
            bash -c "swapoff $SWAP_FILE && fallocate -l ${want_g}G $SWAP_FILE \
                && chmod 600 $SWAP_FILE && mkswap $SWAP_FILE >/dev/null && swapon $SWAP_FILE"
    fi

    # fstab references the path only; the size lives in the file itself.
    if grep -qE "^$SWAP_FILE[[:space:]]" /etc/fstab 2>/dev/null; then
        ok "fstab entry present"
    else
        fail "no fstab entry for $SWAP_FILE -- swap will not survive a reboot"
    fi
}

# ------------------------------------------------------------------- motd ---
# One template for every platform; @NAME@ placeholders keep the body a literal
# /bin/sh script so nothing expands at install time.
motd_template() {
    cat <<'TPL'
#!/bin/sh
# Site banner: machine identity + the /data convention.
# Rendered by pam_motd on interactive logins only (not on `ssh host <cmd>`).

# @COLORNAME@, else the nearest xterm-256 index. pam_motd runs this
# with a bare environment, so COLORTERM is usually unset and 256 is the branch
# that actually fires.
case "$COLORTERM" in
    truecolor|24bit) @VAR@='\033[38;2;@TRUECOLOR@m' ;;
    *)               @VAR@='\033[38;5;@XTERM256@m' ;;
esac
Y='\033[33m'; B='\033[1m'; D='\033[2m'; R='\033[0m'
MEM=$(free -g | awk '/^Mem:/{print $2}')
BAR='======================================================================'

printf '%b\n' "${@VAR@}${BAR}${R}"
printf '%b\n' " ${B}@BRAND@ -- $(hostname)${R}   @SPEC@   ${MEM} GB unified memory"
printf '%b\n' "${@VAR@}${BAR}${R}"
printf '%b\n' " ${Y}PUT EVERYTHING UNDER /data${R} -- including your own files and caches."
printf '%b\n' ""
printf '%b\n' "   /data/<name>/    your workspace          ${D}e.g. /data/lsyin${R}"
printf '%b\n' "   /data/.cache/    shared pip / HF cache   ${D}model weights go here${R}"
printf '%b\n' ""
printf '%b\n' " \$HOME is not the place for work -- keep it empty."
printf '%b\n' ""
printf '%b\n' " ${D}The 'rdxa' account is in the docker group, so docker needs no sudo.${R}"
printf '%b\n' "${@VAR@}${BAR}${R}"
TPL
}

# Keyed off the GPU stack, not the arch: the amdgpu compute node exists on Strix
# Halo and never on Spark, so a future x86 NVIDIA box still resolves correctly.
site_key() {
    if [ -e /sys/class/kfd/kfd ] \
        || grep -qi 'AMD Ryzen AI Max' /proc/cpuinfo 2>/dev/null; then
        echo halo
    else
        echo spark
    fi
}

step_motd() {
    section "motd  (login banner: machine identity + the /data rule)"

    local site name var tc x256 brand spec colorname path
    site=$(site_key)
    case "$site" in
        halo)
            name=99-halo-site;  var=AMD; tc='237;28;36'; x256=160
            brand='AMD Strix Halo';   spec='Ryzen AI Max+ 395 x86_64'
            colorname='AMD red #ED1C24'
            ;;
        *)
            name=99-spark-site; var=NV;  tc='118;185;0'; x256=106
            brand='NVIDIA DGX Spark'; spec='GB10 arm64'
            colorname='NVIDIA green #76B900'
            ;;
    esac
    path="$MOTD_DIR/$name"
    note "site=$site -> $path"

    if [ "$MODE" = revert ]; then
        if [ -f "$path" ]; then
            run "remove site banner" rm -f "$path"
        else
            ok "no site banner installed"
        fi
        return
    fi

    if [ ! -d "$MOTD_DIR" ]; then
        fail "$MOTD_DIR not found -- host has no pam_motd drop-in dir"
        return
    fi

    write_file "site banner" "$path" "$(motd_template | sed \
        -e "s,@COLORNAME@,$colorname," \
        -e "s,@VAR@,$var,g" \
        -e "s,@TRUECOLOR@,$tc," \
        -e "s,@XTERM256@,$x256," \
        -e "s,@BRAND@,$brand," \
        -e "s,@SPEC@,$spec,")"

    if [ -x "$path" ]; then
        ok "banner executable"
    else
        run "chmod +x banner" chmod +x "$path"
    fi

    note "pam_motd renders it on interactive logins only, not on 'ssh host <cmd>'"
}

# ------------------------------------------------------------------- main ---
printf '%srdxa-setup%s  host=%s  mode=%s%s%s\n' \
    "$YLW" "$RST" "$(hostname)" "$MODE" \
    "$(dry && [ "$MODE" != check ] && echo ' (dry-run)')" "${ONLY:+  only=$ONLY}"

if ! dry; then
    sudo -v || { echo "sudo required" >&2; exit 1; }
fi

for s in "${STEPS[@]}"; do
    wanted "$s" && "step_$s"
done

section "summary"
if dry; then
    if [ "$CHANGED" -eq 0 ]; then
        printf '  %snothing to do -- node is already hardened%s\n' "$GRN" "$RST"
    else
        printf '  %s%d change(s) pending%s -- rerun as --%s\n' \
            "$YLW" "$CHANGED" "$RST" "$([ "$MODE" = revert ] && echo revert || echo apply)"
    fi
else
    printf '  %d applied, %d failed\n' "$CHANGED" "$FAILED"
fi

if [ "$MODE" = apply ] && ! dry; then
    cat <<'EOF'

  verify from another machine:
    tailscale status | grep <this-host>       # want "active", not "offline"
    ssh <this-host> "systemctl is-enabled sleep.target"     # want "masked"
    ssh <this-host> "iw dev \$(nmcli -t -f DEVICE,TYPE dev status |
      awk -F: '\$2==\"wifi\"{print \$1;exit}') get power_save"   # want "off"

  a symmetric-NAT network still forces Tailscale onto a DERP relay; that is a
  property of the network, not something this script can fix.
EOF
fi

[ "$FAILED" -gt 0 ] && exit 1
exit 0
