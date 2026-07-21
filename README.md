# Input action controller

`input-action-controller` turns a hardware button into a stateful command. For example, a headset control that sends
only raw `hidraw` reports can start and stop a voice-input command even when the desktop does not expose it as a key.

## Why use it?

- Supports `hidraw` and `evdev` input sources.
- Uses `on-off` and `toggle` input trigger modes for stateful actions shared by multiple devices.
- Uses stable udev-backed device resolution and hotplug recovery.
- Captures a trigger and proposes a narrow `uaccess` rule during interactive setup.
- Executes direct argv commands with bounded process-group cleanup.

## What it does not do

The controller does not perform speech recognition, manage a clipboard, insert text, or configure a desktop
environment. Configure the target application separately, then use this project to run its command from an input.

## Install on Arch Linux

Install the [AUR package](https://aur.archlinux.org/packages/input-action-controller) with an AUR helper:

```bash
yay -S input-action-controller
```

Review the AUR `PKGBUILD` before installing it. The package installs the CLI, user service, commented configuration
template, documentation, and license. It does not install an active configuration, a broad udev rule, or
application-specific settings.

### Build from source

Clone this repository, review `PKGBUILD`, and build the same Arch package locally:

```bash
./scripts/makepkg -si
```

This command requires the Arch `base-devel` package group and the dependencies declared in `PKGBUILD`.

## Quick start

1. Confirm that the target application's command works as your desktop user.
2. Run setup to capture an input and preview the configuration and permission changes:

   ```bash
   input-action-controller setup
   ```

3. Validate the saved configuration:

   ```bash
   input-action-controller config-check
   ```

4. Enable the user service and inspect its state:

   ```bash
   systemctl --user enable --now input-action-controller.service
   systemctl --user status input-action-controller.service
   input-action-controller status
   ```

Setup is the primary configuration path. It asks before running privileged commands and requests a reconnect before
checking a new permission rule.

Read the [configuration reference](docs/configuration.md) for manual TOML configuration and runtime details. Use the
[device-discovery guide](docs/device-discovery.md) for device access, capture, recovery, and advanced fallback steps.
The [Handy GNOME Wayland guide](docs/examples/handy-gnome-wayland.md) and
[Speech Note GNOME Wayland guide](docs/examples/dsnote-gnome-wayland.md) show application-specific examples.
Project release and dependency-update procedures are in the [maintainer guide](docs/maintaining.md).

## Actions

Each action runs direct argv commands, without a shell. Use the `on-off` input trigger mode for separate on and off
commands. Use the `toggle` input trigger mode when one command changes the target application's state. See the
[configuration reference](docs/configuration.md) for failure policies, timeouts, and shutdown behavior.

## Inputs and selection

Use `evdev` when Linux reports a symbolic `KEY_*` or `BTN_*` event. Use `hidraw` only for a control that has no suitable
evdev event. Setup records stable selectors and recovers a profile when its device is reconnected. The
[device-discovery guide](docs/device-discovery.md) covers manual inspection and capture.

## Discover and monitor devices

List candidate devices and stable selectors:

```bash
input-action-controller devices
```

Stop the service before monitoring one configured profile:

```bash
systemctl --user stop input-action-controller.service
input-action-controller monitor --device "Plantronics Blackwire C3220"
```

`monitor` prints raw reports or symbolic events and does not execute an action. Keyboard-class profiles require an
interactive confirmation because they can expose ordinary key events.

## Run the service

Inspect logs when a configured action does not run:

```bash
journalctl --user -u input-action-controller.service --since '-10 min' --no-pager
journalctl --user -u input-action-controller.service -f
```

## Upgrade

Upgrade the AUR package, validate the configuration, and restart the service:

```bash
yay -Syu input-action-controller
systemctl --user daemon-reload
input-action-controller config-check
systemctl --user restart input-action-controller.service
input-action-controller status
```

When using a source checkout instead, pull the desired release and run `./scripts/makepkg -si` before the remaining
commands.

## Remove

Stop and disable the user service, then remove the package:

```bash
systemctl --user disable --now input-action-controller.service
sudo pacman -R input-action-controller
```

Package removal does not remove your XDG configuration. Remove it only when it is no longer needed:

```bash
config_home=${XDG_CONFIG_HOME:-$HOME/.config}/input-action-controller
rm -r "$config_home"
```

## Development

Run the test suite without writing Python bytecode:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python -m unittest discover -s tests -v
```
