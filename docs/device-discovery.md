# Discover and authorize input devices

Use this procedure as the advanced fallback for `input-action-controller setup`.
Setup is the primary path: it
identifies a device, proposes selectors, captures a trigger, previews the
configuration and permission change, and
offers to preserve or start the user service. Use the manual procedure below
only when setup cannot cover the device
or when you need to review every command and selector yourself. The package
deliberately ships no broad udev rule.

## Start with setup

Run setup as the logged-in desktop user:

```bash
input-action-controller setup
```

If no user configuration exists and `/etc/input-action-controller/config.toml`
exists, setup reads it as a seed for
the user configuration. The seed is deferred until setup saves: it never edits
the `/etc` file. Setup rejects a
symbolic link configuration destination and refuses any destination under
`/etc`; rerun it with a user-writable
regular file or the symlink target named explicitly with `--config`.

Setup prefers `ID_SERIAL_SHORT` when it must distinguish identical devices. It
offers `ID_PATH` only as an explicit,
port-bound fallback. Moving the device to another USB port can invalidate that
selector. For a permission rule, setup
shows its Current scope, its Future scope, the exact privileged commands, and
the rule before it asks to install it.
It does not silently run privileged work.

## Composite evdev devices

One USB device can expose several `/dev/input/event*` nodes with the same USB identity. During setup, the controller
uses a standard udev `ID_INPUT_*` classifier to narrow those nodes before it considers a serial number or `ID_PATH`.
The selected classifier appears in both the managed udev rule and the runtime
profile as `input_classifier`. This keeps runtime identity and permission scope
aligned after a reboot.

The supported classifier registry names are:

- `ID_INPUT_TRACKBALL`
- `ID_INPUT_POINTINGSTICK`
- `ID_INPUT_TOUCHPAD`
- `ID_INPUT_TOUCHSCREEN`
- `ID_INPUT_TABLET_PAD`
- `ID_INPUT_TABLET`
- `ID_INPUT_JOYSTICK`
- `ID_INPUT_KEYBOARD`
- `ID_INPUT_MOUSE`
- `ID_INPUT_KEY`

`ID_INPUT_KEYBOARD` and `ID_INPUT_KEY` trigger the keyboard-class confirmation and permission-scope warning.

After you capture a `KEY_*` or `BTN_*` press, setup resolves the proposed profile with the same capability-based
resolver used by the daemon. Setup stops before saving when the captured event does not identify the selected node
uniquely.

`ID_PATH` is offered only as an explicit fallback when it is unique after classifier narrowing. A profile that stores
`ID_PATH` is bound to that physical port.

The default permission mode is `TAG+="uaccess"`, which grants the active local
session access after a reconnect. The
advanced alternative writes `GROUP="input", MODE="0660"`. Membership in the
input group can expose other matching
input nodes, so choose it only when session ACLs cannot satisfy the device and
the broader access is acceptable.

When setup changes an existing user configuration, it creates a timestamped
sibling backup named
`config.toml.bak.YYYYMMDDTHHMMSSZ` (with a numeric suffix if needed). At the
start of a later run, setup lists those
backups for restoration. If a save or later service step cannot complete, setup
reports any recovery artifact and its
Recovery commands; retain those files until the configuration and service state
are confirmed.

## Safety

This warning: keyboard-class access permits observing ordinary keys, including
text typed in other
applications. Grant access only to the exact interface selected by the profile.
Never create a rule that covers every
`/dev/input/event*` or `/dev/hidraw*` node.

`usbhid-dump` can interfere with normal input because it may temporarily detach
a kernel HID driver. Filter by USB ID
and interface, stop the capture with `Ctrl+C`, and do not run it against the
keyboard or pointer you need to operate
the session.

## List candidates

Start with the controller's property-only inventory:

```bash
input-action-controller devices
lsusb
```

Record the four-digit USB vendor and product IDs. Bus and device numbers from
`lsusb` change after reconnecting and
must not be configuration identities.

Inspect one candidate node and its parent properties:

```bash
node=/dev/hidraw3
udevadm info --query=property --name="$node"
udevadm info --attribute-walk --name="$node"
```

Look for `ID_VENDOR_ID`, `ID_MODEL_ID`, `ID_USB_INTERFACE_NUM`,
`ID_SERIAL_SHORT`, and `ID_PATH`. Configure vendor and
product IDs, then add `interface_number`, `serial`, or `id_path` only when
needed to select exactly one node.

## Prefer standard evdev events

Check whether Linux already exposes the button as an input event:

```bash
sudo libinput debug-events --device /dev/input/event7
sudo evtest /dev/input/event7
```

Press and release only the target control. Use a symbolic `KEY_*` or `BTN_*`
name reported for press value `1`. The
controller ignores release value `0` and repeat value `2`. It does not grab the
device, so an existing desktop action
may still occur.

Add an evdev profile and validate it:

```toml
[[devices]]
name = "Headset media key"
action = "voice_input"
transport = "evdev"
mode = "toggle"
vendor_id = "1234"
product_id = "5678"
interface_number = "01"
toggle_events = ["KEY_F13"]
toggle_off_timeout_seconds = 60.0
```

```bash
input-action-controller config-check
```

### Verified Xiaomi thumb-button example

The tested `Xiaomi Wireless Mouse 3 Colorful Mouse` reports USB ID `2717:5070`.
During capture it resolved to
`/dev/input/event4` on interface `00`. Event node numbers can change after
reconnecting, so the profile uses USB and
interface selectors instead of the observed node path.

Running `evtest` without `--grab` produced these button mappings:

- The rear thumb button produced `BTN_SIDE` with press value `1` and release
  value `0`.
- The front thumb button produced `BTN_EXTRA` with press value `1` and release
  value `0`.

The controller's monitor displayed both buttons and pointer movement because it
reports raw events. The daemon still
matched only the configured press event. The tested profile assigns the rear
button and leaves `BTN_EXTRA` unassigned:

```toml
[device_selection]
strategy = "all"

[[devices]]
name = "Xiaomi Wireless Mouse 3 rear thumb button"
action = "voice_input"
transport = "evdev"
mode = "toggle"
vendor_id = "2717"
product_id = "5070"
interface_number = "00"
toggle_events = ["BTN_SIDE"]
toggle_off_timeout_seconds = 60
```

Use `strategy = "all"` when this profile and another profile, such as a headset,
must remain active together. The
profile resolved to exactly one readable node before it was added to the user
configuration.

The timeout behavior was tested separately. A temporary timeout `5` issued one
automatic off transition after a
single press. A temporary timeout `0` remained on for more than five seconds and
required a second press. The working
configuration was then restored to `60` seconds.

## Capture vendor-specific hidraw reports

Use `hidraw` only when no suitable evdev event exists. Determine the exact USB
interface first, then run a filtered
capture. The example is limited to `047f:c056`, interface 3:

```bash
sudo usbhid-dump -m 047f:c056 -i 3 -e stream -t 0
```

Capture an idle baseline, perform one on/off cycle, stop the tool, and repeat at
least three times. Exclude periodic
reports and reports produced by volume, mute, or unrelated controls. Store
complete byte sequences only:

```toml
[[devices]]
name = "Plantronics Blackwire C3220"
action = "voice_input"
transport = "hidraw"
mode = "on-off"
vendor_id = "047f"
product_id = "c056"
interface_number = "03"
on_reports = ["08 02"]
off_reports = ["08 00"]
```

## Verify through the controller

After access is configured, stop the daemon and monitor only the named profile:

```bash
systemctl --user stop input-action-controller.service
input-action-controller config-check
input-action-controller monitor --device "Plantronics Blackwire C3220"
```

The command acquires the daemon's lock and executes no action. A keyboard-class
profile opens only after an
interactive `yes` confirmation.

## Derive a narrow udev rule

Derive one local rule from every configured selector. Do not silently omit a
selector:

| TOML selector | udev match |
| --- | --- |
| `transport = "hidraw"` | `SUBSYSTEM=="hidraw", KERNEL=="hidraw*"` |
| `transport = "evdev"` | `SUBSYSTEM=="input", KERNEL=="event*"` |
| `vendor_id` | `ATTRS{idVendor}` |
| `product_id` | `ATTRS{idProduct}` |
| `interface_number` | `ENV{ID_USB_INTERFACE_NUM}` |
| `serial` | `ENV{ID_SERIAL_SHORT}` |
| `id_path` | `ENV{ID_PATH}` |
| `input_classifier` | `ENV{ID_INPUT_*}` |

Compare each configured value with `udevadm info --query=property` and
`udevadm info --attribute-walk`. If a
configured selector is absent, differs, or cannot be represented by an exact
udev match, stop. Do not generate a
broader rule. Event and report triggers are action matchers, not device-identity
selectors.

### Scope review

Before any installation, record the rule's Current scope: the nodes that
presently match every configured selector.
Also record its Future scope: any later node matching the same udev predicate
receives the same access. A selector
that cannot be represented exactly is a reason to stop, not to omit it. `evdev`
trigger codes are not device identity
and cannot narrow a udev rule; review keyboard-class scope especially carefully.

### Hidraw example

Create a staging file as your normal user:

```bash
rule_file=$(mktemp)
printf '%s%s%s\n' \
  'ACTION!="remove", SUBSYSTEM=="hidraw", KERNEL=="hidraw*"' \
  ', ATTRS{idVendor}=="047f", ATTRS{idProduct}=="c056"' \
  ', ENV{ID_USB_INTERFACE_NUM}=="03", TAG+="uaccess"' >"$rule_file"
cat "$rule_file"
```

If the profile also has `serial` or `id_path`, add exact
`ENV{ID_SERIAL_SHORT}=="..."` and `ENV{ID_PATH}=="..."`
matches before `TAG+="uaccess"`.

### Evdev example

```bash
rule_file=$(mktemp)
printf '%s%s%s\n' \
  'ACTION!="remove", SUBSYSTEM=="input", KERNEL=="event*"' \
  ', ATTRS{idVendor}=="1234", ATTRS{idProduct}=="5678"' \
  ', ENV{ID_USB_INTERFACE_NUM}=="01", ENV{ID_INPUT_MOUSE}=="1"' \
  ', TAG+="uaccess"' >"$rule_file"
cat "$rule_file"
```

Replace `ID_INPUT_MOUSE` with the exact `input_classifier` stored in the
profile. Omit that clause only when the profile has no classifier.

Inspect the generated rule and compare every match with the TOML profile.
Continue only when none is missing or
broader than the configured selector.

## Install and test the rule

Install the inspected staging file, then test it against the exact sysfs node:

```bash
sudo install -o root -g root -m 0644 "$rule_file" \
  /etc/udev/rules.d/70-input-action-controller-local.rules
sys_path=$(udevadm info --query=path --name="$node")
sudo udevadm test --action=add "$sys_path"
```

Read the `udevadm test` output and confirm that the local rule matches. Reload
rules and trigger only this node:

```bash
sudo udevadm control --reload-rules
sudo udevadm trigger --action=add "$sys_path"
```

Reconnect the physical device so the active desktop session receives `uaccess`,
then rediscover its possibly changed
node. Set `node` to the rediscovered path before checking access:

```bash
input-action-controller devices
node=/dev/hidrawN
# Replace with the rediscovered /dev/hidrawN or /dev/input/eventN path.
getfacl "$node"
test -r "$node" && printf 'read access: yes\n'
```

Do not run the daemon as root to bypass a failed rule. Correct the selectors or
local rule instead.

## Manual recovery

For a configuration edited by setup, inspect sibling backups before replacing
anything:

```bash
config_home=${XDG_CONFIG_HOME:-$HOME/.config}/input-action-controller
ls -1t "$config_home"/config.toml.bak.*
```

Use the exact recovery artifact and command reported by setup. A normal backup
recovery has this form, with the
timestamp replaced by the reported file name:

```bash
mv -- "$config_home/config.toml.bak.YYYYMMDDTHHMMSSZ" "$config_home/config.toml"
sync -- "$config_home"
input-action-controller config-check
```

Do not guess a rollback path if setup reported a different recovery artifact.
For a failed permission installation,
use the displayed rollback and udev reload commands, then reconnect the device
before checking access again.

## Finish verification

```bash
input-action-controller config-check
input-action-controller status
systemctl --user enable --now input-action-controller.service
journalctl --user -u input-action-controller.service -f
```

Exercise every trigger three times, disconnect and reconnect the device, and
confirm one command transition per
physical state change. Replace a device by repeating discovery; do not reuse a
selector until it resolves to exactly
one node.
