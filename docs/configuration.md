# Configuration reference

Use `input-action-controller setup` for normal configuration. It inventories devices, proposes stable selectors,
captures a trigger, previews changes, and offers to preserve or start the packaged user service. Setup stops an active
service while editing and restores the previous service state if setup is cancelled before the configuration is
committed.

## Configuration paths and setup restrictions

An explicit `--config PATH` has highest priority. `INPUT_ACTION_CONTROLLER_CONFIG` is next. The controller then uses
the existing `$XDG_CONFIG_HOME/input-action-controller/config.toml` user file, defaulting `XDG_CONFIG_HOME` to
`$HOME/.config`. If the user file does not exist, it checks `/etc/input-action-controller/config.toml` for a
configuration managed separately by the system administrator. The package does not create that system file.

Run setup as the logged-in desktop user. Setup refuses root execution, does not edit `/etc` configuration, and rejects
a symbolic link destination. For a writable regular file outside `/etc`, run:

```bash
input-action-controller --config PATH setup
```

The packaged `input-action-controller.service` does not use a custom configuration path. Setup therefore does not use a
custom configuration path with that service; it prints the foreground command instead:

```bash
input-action-controller --config /absolute/path/config.toml daemon
```

Keep that process under an operator-managed supervisor when it must survive logout. Do not assume that enabling the
packaged user service loads the custom file.

## Manual configuration

Use manual editing only when you must change an existing configuration outside setup or cannot configure the device
interactively. Create an editable user configuration:

```bash
config_home=${XDG_CONFIG_HOME:-$HOME/.config}/input-action-controller
mkdir -p "$config_home"
cp /usr/share/doc/input-action-controller/config.example.toml "$config_home/config.toml"
${EDITOR:-vi} "$config_home/config.toml"
input-action-controller config-check
```

Uncomment the sections and replace the example commands and device selectors before running `config-check`.

```toml
[runner]
timeout_seconds = 5.0
shutdown_timeout_seconds = 10.0

[device_selection]
strategy = "priority"

[actions.voice_input]
on_command = ["/usr/bin/example-action", "start"]
off_command = ["/usr/bin/example-action", "stop"]
skip_off_after_failed_on = false
skip_on_after_failed_off = false
off_on_shutdown = true

[[devices]]
name = "Example hidraw on-off device"
action = "voice_input"
transport = "hidraw"
mode = "on-off"
vendor_id = "1234"
product_id = "5678"
interface_number = "01"
# serial = "device-serial"
# id_path = "pci-0000:00:00.0-usb-0:1:1.0"
on_reports = ["01 02"]
off_reports = ["01 00"]

[[devices]]
name = "Example evdev toggle button"
action = "voice_input"
transport = "evdev"
mode = "toggle"
vendor_id = "1234"
product_id = "5678"
toggle_events = ["KEY_F13"]
toggle_off_timeout_seconds = 0
```

## Actions and failures

Commands are strict argv arrays. The controller executes each array directly without a shell, so pipelines, redirection,
environment expansion, and multiple commands belong in an executable wrapper script. Run `command -v PROGRAM` and put
the result in each argv array. Do not use a shell command string.

A failed command leaves the action state uncertain and is not retried automatically. The two policies are independent:

- `skip_off_after_failed_on` skips the next opposite transition after a failed on and assumes off.
- `skip_on_after_failed_off` skips the next opposite transition after a failed off and assumes on.

Leave both false for separate idempotent start and stop commands. A toggle-only application can enable both when running
the opposite command after a failed transition would repeat the wrong toggle.

Commands run in a new process group. Timeout and shutdown cleanup terminate descendants that remain in that process
group. A wrapper that daemonizes or moves a child into another process group is unsupported.

## Device selection and triggers

Set `strategy = "priority"` to use the first available profile in configuration order. Discovery continues, and a
higher-priority device preempts a lower-priority device when it appears. Set `strategy = "all"` to monitor all available
profiles concurrently. You can define multiple device profiles for one named action; the action serializes their
commands and shares one state and timer.

A profile must resolve to exactly one node. Add optional `serial` or `id_path` selectors when identical devices are
ambiguous. Prefer `serial`; `id_path` identifies a physical port and can change when the device is moved. Stable
resolution statuses include `unavailable`, `permission-denied`, `ambiguous-device`, and `device-node-conflict`.

`hidraw` supports `mode = "on-off"` with complete `on_reports` and `off_reports`. `evdev` supports separate `on_events`
and `off_events`, or `mode = "toggle"` with `toggle_events`. Evdev names must be symbolic `KEY_*` or `BTN_*` values.
Only press value `1` triggers an action; release and autorepeat are ignored.

`toggle_off_timeout_seconds` defaults to `60.0`. A positive value requests automatic off after that many seconds.
`toggle_off_timeout_seconds = 0` disables automatic off.

## Permission migration

During migration, readable devices normally skip permission changes. If a temporary broad rule or `input` group
membership makes a device readable, setup asks `Create a managed permission rule for this readable device? [y/N/x]`.
Answer yes to install and verify a profile-specific managed rule without first removing the existing access. The
operator removes broad rules and `input` group membership only after reconnect and action tests pass.

See the [device-discovery guide](device-discovery.md) for permission scope, device capture, managed-rule inspection,
backups, recovery artifacts, and the advanced manual fallback.

## Shutdown, status, and exit codes

`shutdown_timeout_seconds` is one global bounded-shutdown budget. The daemon stops new input, lets active transitions
finish within the budget, and requests one final off when `off_on_shutdown = true`. When the deadline expires, it
terminates active process groups and exits without starting more commands.

Validate configuration without opening an input device:

```bash
input-action-controller config-check
```

The success line is:

```text
configuration: valid
```

`status` reports service activity and runtime lock independently. It prints `service activity: active` and
`runtime lock: held` with its owner PID, executable availability, every profile resolution, and the selected `priority`
profile or active `all` profiles. A user service can be activating or failed while the lock is free, and a process can
hold the lock independently of systemd. The command does not report target-application state.

`monitor` refuses to run while any daemon owns the runtime lock. Stopping the packaged service may be insufficient if a
foreground daemon still holds it. Stop that process before retrying; monitor exits with lock-contention status 3.

CLI exit statuses are `0` for success, `1` for runtime failure, `2` for invalid usage or configuration, and `3` for
daemon/monitor lock contention.
