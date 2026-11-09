#!/bin/bash
# Make the private copy of a Hyperwheel image: the same image with the shared hotspot
# password on its boot partition, for the sites to flash. Runs on the admin's Mac.
#
#     tools/image/add-hotspot-password.sh ~/Downloads/hyperwheel-image-1.img.zip
#
# It writes hyperwheel-image-1-private.img.zip beside the original, which stays as it
# was. On the Pi, the first boot starts the hotspot with the password, and
# `hyperwheel setup` moves it into the site settings (root-only) and deletes the copy.
#
# The password is asked for twice, hidden. It's handled only by the shell's own
# commands (read, printf), so it never appears on a command line or in a process
# list, and it's never printed. Share the private copy only with the sites: anyone
# who has it can read the password from it.
#
# Uses only what macOS has: unzip, hdiutil, zip, shasum.

set -euo pipefail
export LC_ALL=C

say() { printf '%s\n' "$*"; }
stop() { say "Stopped: $*" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || stop "this runs on a Mac (it uses hdiutil)."
[ $# -eq 1 ] || { say "Usage: $0 <hyperwheel-image>.img.zip"; exit 2; }

original=$1
[ -f "$original" ] || stop "there's no file $original."
case "$original" in
  *.img.zip) ;;
  *) stop "$original isn't a .img.zip image." ;;
esac
private="${original%.img.zip}-private.img.zip"
[ ! -e "$private" ] || stop "$private already exists. Move it away first."

work=$(mktemp -d "${TMPDIR:-/tmp}/hyperwheel-image.XXXXXX")
device=""
cleanup() {
  if [ -n "$device" ]; then
    hdiutil detach -quiet "$device" >/dev/null 2>&1 || hdiutil detach -force "$device" >/dev/null 2>&1 || true
  fi
  rm -rf "$work"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

# ---- the password -------------------------------------------------------------------------------

problem() {  # why a hotspot password can't be used (as setup checks it), or nothing
  local pw=$1
  if [ ${#pw} -lt 8 ] || [ ${#pw} -gt 63 ]; then say "it must be 8 to 63 characters long"; return; fi
  if ! [[ "$pw" =~ ^[\ -~]+$ ]]; then
    say "it may use only letters, digits, spaces and the symbols on a keyboard"; return
  fi
  case "$pw" in " "*|*" ") say "it can't start or end with a space"; return ;; esac
}

password=""
for _ in 1 2 3; do
  IFS= read -rs -p "Hotspot password (8 to 63 characters, hidden as you type): " first || stop "no password given."
  printf '\n'
  why=$(problem "$first")
  if [ -n "$why" ]; then say "  That can't be used: $why."; continue; fi
  IFS= read -rs -p "The same again: " second || stop "no password given."
  printf '\n'
  if [ "$first" != "$second" ]; then say "  The two don't match."; continue; fi
  password=$first
  break
done
unset first second
[ -n "$password" ] || stop "no hotspot password was set. Nothing was written."

# ---- the image ------------------------------------------------------------------------------------

say "Unpacking a copy of the image ..."
unzip -q "$original" -d "$work/unpacked" || stop "the image couldn't be unpacked."
images=()
while IFS= read -r f; do images+=("$f"); done < <(find "$work/unpacked" -type f -name '*.img' ! -path '*/__MACOSX/*')
[ ${#images[@]} -eq 1 ] || stop "the zip should hold one .img file, and it holds ${#images[@]}."
image=${images[0]}

say "Opening its boot partition ..."
mkdir "$work/mnt"
attached=$(hdiutil attach -imagekey diskimage-class=CRawDiskImage -nobrowse -noverify -mountroot "$work/mnt" "$image") \
  || stop "macOS couldn't open the image."
device=$(printf '%s\n' "$attached" | awk 'NR == 1 { print $1 }')
boot=$(printf '%s\n' "$attached" | awk -F'\t' '$2 ~ /FAT|DOS/ { sub(/[ \t]+$/, "", $3); print $3; exit }')
[ -n "$boot" ] && [ -d "$boot" ] || stop "the image has no boot partition macOS can open."
[ -f "$boot/config.txt" ] && [ -f "$boot/cmdline.txt" ] \
  || stop "that isn't a Raspberry Pi image (no config.txt and cmdline.txt)."
[ ! -e "$boot/hyperwheel/hotspot-password" ] \
  || stop "this image already has a hotspot password. Use the original image."

mkdir -p "$boot/hyperwheel"
printf '%s\n' "$password" > "$boot/hyperwheel/hotspot-password"
unset password
# What macOS may leave on a FAT partition: none of it belongs on the Pi.
rm -rf "$boot/.fseventsd" "$boot/.Spotlight-V100" "$boot/.Trashes" "$boot/.TemporaryItems"
find "$boot" -name '._*' -delete 2>/dev/null || true
sync
hdiutil detach -quiet "$device" || stop "the image couldn't be closed."
device=""

say "Packing the private copy ..."
(umask 077 && cd "$(dirname "$image")" && zip -q -X "$work/private.img.zip" "$(basename "$image")") \
  || stop "the private copy couldn't be packed."
mv "$work/private.img.zip" "$private"
chmod 600 "$private"

say ""
say "Written: $private"
say "SHA-256: $(shasum -a 256 "$private" | awk '{ print $1 }')"
say "It holds the hotspot password. Share it only with the sites, privately. The original is unchanged."
