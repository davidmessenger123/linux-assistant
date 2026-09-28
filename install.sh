#!/usr/bin/env bash
# Install the assistant as a desktop entry, or check the machine can run it.
set -euo pipefail
script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

usage() {
    cat <<'USAGE'
Usage: install.sh [options]

  --check          Only run the doctor and exit
  --prefix DIR     Where to install the code (default: ~/.local/share/linux-assistant)
  --desktop DIR    Where to install the .desktop file
                   (default: ~/.local/share/applications)
  --uninstall      Remove an installation
  -h, --help       Show this help

Without options the code is copied to the prefix, a desktop entry is written,
and the icon and appdata are installed. Nothing outside those three places is
touched, and no system directory is written to.
USAGE
}

prefix="${XDG_DATA_HOME:-$HOME/.local/share}/linux-assistant"
desktop_dir="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
icon_dir="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"
meta_dir="${XDG_DATA_HOME:-$HOME/.local/share}/metainfo"
mode=install

while (($# > 0)); do
    case "$1" in
        --check) python3 -m assistant.doctor; exit $? ;;
        --prefix) prefix="${2:?--prefix needs a directory}"; shift ;;
        --desktop) desktop_dir="${2:?--desktop needs a directory}"; shift ;;
        --uninstall) mode=uninstall ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'Unknown option: %s\n\n' "$1" >&2; usage >&2; exit 1 ;;
    esac
    shift
done

if [[ "$mode" == uninstall ]]; then
    rm -rf "$prefix"
    rm -f "$desktop_dir/dev.linuxassistant.Assistant.desktop"
    rm -f "$icon_dir/dev.linuxassistant.Assistant.svg"
    rm -f "$meta_dir/dev.linuxassistant.Assistant.metainfo.xml"
    printf 'Removed the assistant.\n'
    exit 0
fi

printf 'Checking this machine first.\n\n'
python3 -m assistant.doctor || true
printf '\nInstalling anyway. Anything marked FAIL above will still not work.\n\n'

mkdir -p "$prefix" "$desktop_dir" "$icon_dir" "$meta_dir"
for item in assistant data README.md; do
    cp -r "$script_dir/$item" "$prefix/"
done

cat > "$desktop_dir/dev.linuxassistant.Assistant.desktop" <<ENTRY
[Desktop Entry]
Type=Application
Name=Assistant
GenericName=Desktop Assistant
Comment=Chat with a model that can see your windows and act on your desktop
Exec=env PYTHONPATH=$prefix python3 -m assistant
Icon=dev.linuxassistant.Assistant
Terminal=false
Categories=Utility;Development;Network;
StartupNotify=true
ENTRY

cp "$script_dir/data/dev.linuxassistant.Assistant.svg" "$icon_dir/"
cp "$script_dir/data/dev.linuxassistant.Assistant.metainfo.xml" "$meta_dir/"

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$desktop_dir" >/dev/null 2>&1 || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f -t "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor" >/dev/null 2>&1 || true
fi

printf 'Installed to %s\n' "$prefix"
printf 'Look for "Assistant" in your application menu.\n'
printf 'Run "python3 -m assistant.doctor" from that directory to re-check this machine.\n'
