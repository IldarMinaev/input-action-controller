# Handy on GNOME Wayland

Handy is the recommended voice-input example for `input-action-controller`. The
controller invokes one Handy command;
Handy owns recording, recognition, result delivery, clipboard handling, and
insertion into the focused application.

This guide uses Handy's [README](https://github.com/cjpais/Handy#readme),
[CLI parameters](https://github.com/cjpais/Handy#cli-parameters), and
[Linux notes](https://github.com/cjpais/Handy#linux-notes). It documents only
upstream-supported Handy commands.

## Install and check

Handy upstream publishes release artifacts but does not name an official Arch
package. Search repositories and the AUR,
inspect the provider, and install the package or release artifact you selected:

```bash
pacman -Ss handy
yay -Ss handy
yay -S <verified-package-name>
command -v handy
handy --help
```

Install a Handy speech model and complete one recognition cycle from Handy
before configuring the controller. If startup
reports `libgtk-layer-shell.so.0` missing, install the `gtk-layer-shell` runtime
library named in Handy's Linux notes.

## Dependency matrix

| Dependency | Status |
| --- | --- |
| `input-action-controller` | required |
| Handy executable and model | required |
| `wtype` | optional for selected insertion mode |
| `dotool` and its `input` group access | optional for selected insertion mode |
| `wl-clipboard` | optional for selected insertion mode |
| `ydotool` and its daemon/socket | optional for selected insertion mode |
| Speech Note delivery tools | not required by input-action-controller |

Check the controller with `input-action-controller --help`. Check Handy with
`command -v handy` and `handy --help`. Check the direct Wayland path with
`command -v wtype`. For `dotool`, run `command -v dotool` and log out and back
in after group changes. For a clipboard wrapper, run `command -v wl-copy` and
`command -v wl-paste`. For an operator-owned ydotool wrapper, run
`command -v ydotool` and the configured daemon/socket check.
Handy does not document it.

If the selected insertion path uses `ydotool`, configure its separate uinput
access. Review and run the privileged commands yourself:

```bash
printf '%s\n' \
  'ACTION!="remove", SUBSYSTEM=="misc", KERNEL=="uinput", TAG+="uaccess"' \
  > /tmp/70-uinput-uaccess.rules
sudo install -m 0644 /tmp/70-uinput-uaccess.rules \
  /etc/udev/rules.d/70-uinput-uaccess.rules
printf '%s\n' uinput | sudo tee /etc/modules-load.d/uinput.conf >/dev/null
sudo udevadm control --reload-rules
if [[ -e /sys/class/misc/uinput ]]; then
  sudo udevadm trigger --action=change \
    --subsystem-match=misc --sysname-match=uinput
else
  sudo modprobe uinput
fi
sudo udevadm settle
getfacl -cp /dev/uinput
test -r /dev/uinput && test -w /dev/uinput
systemctl --user enable --now ydotool.service
ydotool type 'YDOTOOL_UACCESS_TEST'
```

Reloading rules must happen before the first module load. If the module is
already loaded, the targeted change event applies the new rule to the existing
device. `ydotoold` needs effective `rw` access to `/dev/uinput`; this is
independent of the controller's access to its configured input source.

The controller neither installs nor starts any recognition, insertion, or
clipboard software. Discover optional Arch
package names before installing them:

```bash
pacman -Ss '^(wtype|wl-clipboard|ydotool)$'
yay -Ss '^(dotool|ydotool)$'
```

Choose the Wayland insertion tools by the selected insertion path. Handy
documents `wtype` for reliable direct Wayland
typing and `dotool` as an alternative that needs the `input` group. Do not add
`ydotool` or `wl-clipboard` merely for
this controller. A wrapper needs them only when it actually invokes their
commands.

For a clipboard-paste workflow, Handy warns that its overlay can take focus and
send a paste to the wrong target. Set
**Settings > Advanced > Overlay Position** to **None**, then test delivery in
every target application. Keep the target
field focused throughout recording and result delivery. A GNOME shortcut is
unnecessary for the controller and can
conflict with the same button; do not use it mid-cycle.

## Configure the controller

The only upstream-documented controller command is
`handy --toggle-transcription`. It toggles an
already-running Handy instance. `handy --toggle-post-process` is a different
mode, and `handy --cancel` discards the
current operation; neither is a paired result-preserving controller off command.

Use setup as the primary path:

```bash
input-action-controller setup
input-action-controller config-check
```

For an existing manually maintained configuration, use the same toggle command
for both transitions. A wrapper is
optional but gives the user service a stable executable:

```bash
mkdir -p ~/.local/bin
install -m 0755 /dev/stdin ~/.local/bin/input-action-handy-toggle <<'EOF'
#!/usr/bin/bash
set -euo pipefail
exec /usr/bin/handy --toggle-transcription
EOF
~/.local/bin/input-action-handy-toggle
```

Replace `/usr/bin/handy` with the result of `command -v handy`. Do not add shell
pipelines, sleeps, clipboard commands,
or ydotool commands unless the selected insertion path requires and has
independently tested them. A wrapper must not
daemonize or leave the controller process group.

```toml
[actions.voice_input]
on_command = ["/home/example/.local/bin/input-action-handy-toggle"]
off_command = ["/home/example/.local/bin/input-action-handy-toggle"]
skip_off_after_failed_on = true
skip_on_after_failed_off = true
off_on_shutdown = true

[[devices]]
name = "Headset button"
action = "voice_input"
transport = "evdev"
mode = "toggle"
vendor_id = "1234"
product_id = "5678"
toggle_events = ["KEY_F13"]
```

The failure-skip policies are appropriate only because both transitions toggle
the same application state. Add the
device profile through setup, or use the
[device-discovery guide](../device-discovery.md) for the advanced manual path.

## Enable and verify

```bash
input-action-controller config-check
systemctl --user enable --now input-action-controller.service
input-action-controller status
journalctl --user -u input-action-controller.service --since '-5 min' --no-pager
```

### End-to-end check

Focus a target field, press the configured device once, speak, and press it
again. Verify one Handy result appears in
that focused target. Repeat after disconnecting and reconnecting the device, and
repeat in each target application.
The controller cannot verify text insertion or Handy state; it reports only
device resolution and command execution.

## State limitation

The controller assumes Handy is off when the daemon starts and cannot query
Handy's application state. Do not use
Handy's UI or hotkey in the middle of a controller-managed cycle. Mixing the UI
or hotkey with controller toggles can
leave the controller and Handy desynchronized. Finish a cycle with the control
that started it, or restart both into a
known idle state.

## Diagnose failures

```bash
command -v handy
handy --help
input-action-controller config-check
input-action-controller status
journalctl --user -u input-action-controller.service -f
```

If Handy records but does not deliver text, leave the controller configuration
unchanged and inspect focus, Handy's
selected insertion path, the selected Wayland tool, and only the wrappers that
actually run clipboard or ydotool
commands.
