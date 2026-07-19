# Speech Note on GNOME Wayland

`input-action-controller` can invoke Speech Note actions, but Speech Note owns
recognition and result delivery. The
controller does not fix Speech Note clipboard delivery; delivery remains Speech
Note's responsibility.

This guide follows Speech Note's
[installation documentation](https://github.com/mkiol/dsnote#how-to-install),
[command-line examples](https://github.com/mkiol/dsnote#command-line-options),
and
[active-window guidance](https://github.com/mkiol/dsnote#insert-into-active-window).

## Install and check

The native package and Flatpak are separate paths. They can have different
executable names, permissions, models, and
action surfaces. Select one path and verify it before configuring the
controller.

```bash
# Native package
pacman -Ss dsnote
yay -Ss '^(dsnote|dsnote-git)$'
yay -S <verified-package-name>
command -v dsnote
dsnote --help
```

```bash
# Flatpak, using upstream's documented application ID
flatpak install net.mkiol.SpeechNote
command -v flatpak
flatpak run net.mkiol.SpeechNote --help
```

Use the command form that succeeds on the installed build. A Flatpak action
example does not prove the equivalent
native command or flags, and a native `--help` failure leaves that native action
surface unverified.

## Dependency matrix

| Dependency | Status |
| --- | --- |
| `input-action-controller` | required |
| Speech Note and its model | required |
| `ydotool` daemon and socket | optional for selected insertion mode |
| Flatpak access to the ydotool socket | optional for selected insertion mode |
| `wl-clipboard` | optional for selected insertion mode |
| Speech Note delivery tools | not required by input-action-controller |

Check the controller with `input-action-controller --help`. Check Speech Note
with the selected native or Flatpak help command. For active-window delivery,
run `command -v ydotool`, check the configured daemon, and run
`test -S "$YDOTOOL_SOCKET"`. For Flatpak active-window delivery, run
`flatpak info --show-permissions net.mkiol.SpeechNote` and confirm access to the
operator-configured socket. For a clipboard wrapper, run `command -v wl-copy`
and `command -v wl-paste`.

Only for `start-listening-active-window`: use the ydotool daemon and socket.
Only for a wrapper that invokes `wl-copy` or `wl-paste`:
use `wl-clipboard`.

`ydotool` checks apply only to active-window delivery. Speech Note upstream does
not prescribe a universal service
unit, socket path, or Flatpak override, so use the daemon and socket configured
by the operator. For example, replace
the placeholder before checking the active path:

```bash
pgrep -a ydotoold
export YDOTOOL_SOCKET=/operator/configured/ydotool.socket
test -S "$YDOTOOL_SOCKET"
```

`wl-clipboard` is not a prerequisite for Speech Note's clipboard action. Use it
only in a wrapper that invokes
`wl-copy` or `wl-paste`; the controller never reads or writes the clipboard. Do
not configure a Speech Note global
shortcut for the same action as the controller button.

## Choose an action

Speech Note documents these Flatpak actions for an already-running application:

```bash
flatpak run net.mkiol.SpeechNote --action start-listening
flatpak run net.mkiol.SpeechNote --action start-listening-clipboard
flatpak run net.mkiol.SpeechNote --action start-listening-active-window
flatpak run net.mkiol.SpeechNote --action cancel
```

`start-listening` keeps the result in Speech Note. `start-listening-clipboard`
publishes a clipboard result but does
not guarantee a paste elsewhere. `start-listening-active-window` targets the
currently focused window and is the only
documented path that needs ydotool on Wayland. `--action cancel` discards the
result and is not an off action.

Do not treat `stop-listening` as portable. Upstream's documented CLI examples
omit it, even though some source builds
list it. Use a paired controller action only after the installed build lists
`stop-listening` and a local end-to-end
test confirms it stops listening without discarding the result. When it is
absent, there is no documented
result-preserving off action for a paired controller configuration.

## Configure the controller

Use setup first:

```bash
input-action-controller setup
input-action-controller config-check
```

Only after the installed build validates a result-preserving start/stop pair,
configure direct argv arrays or wrappers.
For the native path, replace the executable after its help command and action
test succeed. For Flatpak, keep the
upstream launcher prefix:

```toml
[actions.voice_input]
on_command = ["/usr/bin/flatpak", "run", "net.mkiol.SpeechNote", "--action", "start-listening-clipboard"]
off_command = ["/usr/bin/flatpak", "run", "net.mkiol.SpeechNote", "--action", "stop-listening"]
skip_off_after_failed_on = false
skip_on_after_failed_off = false
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

This conditional example is not a generic Flatpak guarantee. Do not replace
`stop-listening` with `cancel`. For a
multi-command delivery wrapper, use an application-tested, bounded completion
signal. Do not append a fixed sleep and
assume that the clipboard is ready, and do not daemonize the wrapper.

## Enable and verify

```bash
input-action-controller config-check
systemctl --user enable --now input-action-controller.service
input-action-controller status
journalctl --user -u input-action-controller.service --since '-5 min' --no-pager
```

### End-to-end check

Focus a target field, perform one complete controller cycle, and verify exactly
one result at that focused target. For
the active-window path, repeat after verifying the ydotool daemon and configured
socket. For a clipboard wrapper,
verify the wrapper's own completion condition and intended target. Repeat after
device reconnect and in every target
application.

## Diagnose failures

```bash
command -v dsnote
dsnote --help
flatpak run net.mkiol.SpeechNote --help
input-action-controller config-check
input-action-controller status
journalctl --user -u input-action-controller.service -f
```

Keep native and Flatpak findings separate when reporting a failure. A controller
log confirms a command transition; it
does not confirm Speech Note state, clipboard contents, ydotool access, or
focused-window delivery.
