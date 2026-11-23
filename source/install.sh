#!/usr/bin/env bash
set -euo pipefail

configure_network=false
sensor=""
light_multiplier=1
while (( $# )); do
    case "$1" in
        --plc-network) configure_network=true; shift ;;
        --sensor) sensor="${2:?Missing sensor IPv4 address}"; shift 2 ;;
        --light-multiplier) light_multiplier="${2:?Missing light multiplier}"; shift 2 ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done

src="$(cd -- "$(dirname -- "$0")" && pwd)"
app="$HOME/.local/share/lab-safety-contest"
entries="$HOME/.local/share/applications"
desktop="$(xdg-user-dir DESKTOP 2>/dev/null || true)"
desktop="${desktop:-$HOME/Desktop}"

/usr/bin/python3 -c 'import fcntl, ipaddress, tkinter'
command -v gst-launch-1.0 > /dev/null
mkdir -p "$app" "$entries" "$desktop"

for file in nano_contest.py nano_bottle_check.py nano_environment.py nano_plc_check.py nano_trends.py nano_virtual_zone.py nano_zone_check.py; do
    install -m 644 "$src/$file" "$app/$file"
done

cat > "$app/launch.sh" <<'SH'
#!/usr/bin/env bash
set -u
app="$(cd -- "$(dirname -- "$0")" && pwd)"
log="$app/desktop.log"
if [[ -f "$log" ]] && (( $(stat -c %s "$log") >= 262144 )); then
    mv -f -- "$log" "$app/desktop.previous.log"
fi
printf '\n--- %s ---\n' "$(date '+%F %T')" >> "$log"
/usr/bin/python3 -u -X faulthandler "$app/nano_contest.py" >> "$log" 2>&1
status=$?
if (( status != 0 )) && command -v zenity >/dev/null; then
    zenity --error --title='实验台安全演示' --text="程序已退出（退出码 $status）。启动日志：$log"
fi
exit "$status"
SH
chmod 755 "$app/launch.sh"

# Generate the desktop entry with a literal, validated argument list.
/usr/bin/python3 - "$app" "$entries" "$desktop" "$sensor" "$light_multiplier" <<'PY'
from ipaddress import IPv4Address
from pathlib import Path
import sys

app, entries, desktop = map(Path, sys.argv[1:4])
sensor, multiplier = sys.argv[4:]
argv = [str(app / "launch.sh")]
config = app / "sensor.conf"
if sensor:
    sensor = str(IPv4Address(sensor))
    if multiplier not in ("1", "100"):
        raise ValueError("Light multiplier must be 1 or 100")
    config.write_text(sensor + "\n" + multiplier + "\n", encoding="utf-8")
def quote(value):
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$")
    return '"' + value.replace("\\", "\\\\").replace("%", "%%") + '"'

contents = (
    "[Desktop Entry]\nType=Application\nName=实验台安全演示\n"
    "Name[en]=Lab Safety Demo\nIcon=camera-video\nTerminal=false\n"
    "Categories=Education;\nExec=" + " ".join(map(quote, argv)) + "\n"
)
for directory in (entries, desktop):
    launcher = directory / "LabSafetyDemo.desktop"
    launcher.write_text(contents, encoding="utf-8")
    launcher.chmod(0o755)
PY

gio set "$desktop/LabSafetyDemo.desktop" metadata::trusted true 2>/dev/null || true
echo "Desktop launcher installed: $desktop/LabSafetyDemo.desktop"

if "$configure_network"; then
    active="$(nmcli -g GENERAL.CONNECTION device show eth0 2>/dev/null || true)"
    method=""
    if [[ -n "$active" && "$active" != "--" ]]; then
        method="$(nmcli -g ipv4.method connection show id "$active")"
    fi
    if [[ "$method" == manual ]]; then
        if ! nmcli -g ipv4.addresses connection show id "$active" | grep -Fq '192.168.3.10/24'; then
            sudo nmcli connection modify id "$active" +ipv4.addresses 192.168.3.10/24
        fi
        sudo ip addr replace 192.168.3.10/24 dev eth0
    else
        profile="LabSafety-PLC-eth0"
        if ! nmcli connection show id "$profile" >/dev/null 2>&1; then
            sudo nmcli connection add type ethernet ifname eth0 con-name "$profile"
        fi
        sudo nmcli connection modify id "$profile" connection.interface-name eth0 \
            connection.autoconnect yes connection.autoconnect-priority 50 \
            ipv4.method manual ipv4.addresses 192.168.3.10/24 ipv4.never-default yes ipv6.method ignore
        sudo nmcli -w 10 connection up id "$profile"
    fi
fi
