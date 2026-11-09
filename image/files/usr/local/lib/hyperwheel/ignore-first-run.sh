#!/bin/sh
# Hyperwheel: Raspberry Pi Imager's customisation is never used on a Hyperwheel Pi. If a site answers Yes by
# mistake, Imager may add a first-run script and boot options to the card. This runs instead of that script:
# it removes the options and the script, and the Pi restarts normally (systemd.run_success_action=reboot).
# Nothing the script asked for (a user, Wi-Fi, a password) is done.
#
#     ignore-first-run.sh [cmdline.txt] [boot folder]
cmdline=${1:-/boot/firmware/cmdline.txt}
boot=${2:-/boot/firmware}
if [ -f "$cmdline" ]; then
    cleaned=$(sed -E -e 's/[[:space:]]+systemd\.run(_success_action|_failure_action)?=[^[:space:]]*//g' \
                     -e 's/[[:space:]]+systemd\.unit=kernel-command-line\.target//g' "$cmdline")
    printf '%s\n' "$cleaned" > "$cmdline"
fi
rm -f "$boot/firstrun.sh"
sync
exit 0
