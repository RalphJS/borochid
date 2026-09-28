# Borochid

A modular peripheral platform: a Python service that detects devices and
fetches their device packages on demand, and a PyQt6 control center that
renders each device's controls from its package.

**Security model.** Device packages are signed *data*, downloaded when a
matching device appears. Code is never downloaded: drivers are Python plugins
shipped as RPM/deb packages, so the system package manager handles signing,
root and updates. When a device needs a driver that isn't installed, the GUI
offers to install it through PackageKit (signed repositories only; the
desktop's own authentication prompt).

```
 udev / BLE / sim ──► Detector ──► DeviceManager ──► Registry (local dir, then HTTP registries)
                                        │                 └─ sharded index, sha256 + ed25519
                                        ▼
                         Channel (hid|serial|libusb|ble) ◄─► Driver (declarative|plugin|code)
                                        │
                              JSON-RPC over Unix socket
                                        ▼
                                PyQt6 GUI (renders package `ui` schema)
```

## Repository layout

Three distributions share the `borochid.*` import namespace (PEP 420, so
there is no `borochid/__init__.py`):

| Directory | Distribution | Depends on | Contains |
|---|---|---|---|
| `packages/common` | `borochid-common` | nothing | RPC framing + protocol version, models, manifest schema, paths |
| `packages/service` | `borochid-service` | common, pyudev (+ extras `serial`, `libusb`, `ble`, `signing`) | detectors, channels, drivers, registry client, `borochid-registry` tool |
| `packages/gui` | `borochid-gui` | common, PyQt6 | control center |

The GUI never imports service code. They only talk over the socket, and on
connect they check `rpc.PROTOCOL_VERSION`, so each side can be released on its
own. Shared test fixtures live in the root `conftest.py`, and example packages
in `examples/`.

## Quick start

```sh
python3 -m venv --system-site-packages .venv   # system PyQt6 is fine
.venv/bin/pip install -r requirements-dev.txt  # editable installs of all three
.venv/bin/pytest

# Terminal 1: service with a simulated device, using the example packages
.venv/bin/borochid-service --sim --local-packages examples/packages
# Terminal 2
.venv/bin/borochid
```

For real hardware as an unprivileged user service, install
`packaging/70-borochid.rules` into `/etc/udev/rules.d/` and
`packaging/borochid.service` as a systemd user unit.

## How it scales

* **Sharded registry index.** The index is split per USB vendor ID
  (`match/usb/046d.json`) and per BLE service/company ID. A lookup fetches
  one small file however many packages the registry holds.
* **Negative caching + ETags.** "No shard" (404) is cached like a hit
  (`index_ttl_seconds`), so unknown devices such as hubs and webcams don't
  keep hitting the network. Stale shards revalidate with `If-None-Match`, and
  the service falls back to cache when offline.
* **Coalescing.** Concurrent lookups and downloads for the same shard or
  package share one request. Each device's bring-up runs in its own task.
* **Multiple registries.** Registries are tried in priority order, and the
  first one with a match wins, so a private registry can override the public
  one. Packages in `local-packages/` override every registry, which is the
  development loop.
* **Content-addressed package cache** (`<id>/<version>-<sha>`), so a
  republished version can't serve a stale cache.
* **One extension mechanism.** Detectors, channels and drivers are all entry
  points (`borochid.detectors`, `borochid.channels`, `borochid.drivers`).
  Built-ins are registered the same way third-party plugins are.

## Devices that change identity

Some devices re-enumerate to change mode; a Corsair dongle becomes another
USB product while its headset is off. When a device drops off the bus it
shows as "Reconnecting…", and if the same device (same port, same serial)
reappears within the grace period (3 s) it continues as the same entry, with
its settings and GUI selection. Only when it stays away is it removed.

## Devices on a wireless receiver

A mouse or keyboard paired to a receiver (Logitech LIGHTSPEED/Unifying and
the like) has no USB device of its own: the receiver's kernel driver creates
a HID device for it under the receiver's. The udev detector reports each of
those as a device, `usb:<receiver port>/<slot>`, with the paired device's own
product ID and serial, so packages match it like any USB device. Its channel
opens only its own hidraw node, never the receiver's, which carries the
traffic of every device paired to it (keyboards included). The receiver is
reported too, as an ordinary USB device.

## One device, several connections

A device can reach the computer more than one way: a keyboard through its
receiver, on its USB cable and over Bluetooth. A driver that can read the
device's own ID from it (a unit ID, the same over every connection) calls
`Driver.identify(id)`, and then:

* **One set of settings.** They are kept under `id-<ID>` instead of the
  USB serial or port, so they follow the device across connections and
  ports. The first time, what the connection kept so far moves there;
  after that the device's own settings win and the connection's file is
  removed. Host services (`host.audio`) follow the same ID.
* **One card.** Connections sharing an ID are one device: the one in use
  (ready and available first, then cable over wireless over Bluetooth) is
  shown and owns the settings; the others are `shadowed` in their summary,
  hidden by the GUI, and `passive` (they don't save). One that takes over
  reloads the settings its twin may have changed.
* **Known ahead.** The service remembers which USB serial belongs to which
  ID (`device-ids.json` in the data directory; serials only, never ports),
  so a connection seen before is hidden from the moment it appears, not
  after its driver has set up.

Summaries carry `device_id`, `connection` and `shadowed`.

## Profiles

Profiles ("Work", "Gaming") belong to the service and are shared by every
device that supports them: switching profile at the top right of the GUI
switches them all, and a device that connects later starts in the active
profile. Each device keeps its own settings per profile (a mouse its DPI
and buttons). The list lives in `<data_dir>/profiles.json` with stable ids,
so renaming never touches device settings; a duplicated profile records
its source, so a device that was unplugged at the time still starts from
the right settings. Drivers opt in with `supports_profiles` and
`use_profile()` (see `borochid.service.drivers.Driver`).

## Drivers

| Where the logic lives | Examples | How it's trusted |
|---|---|---|
| Built into the service | `declarative` | ships with the service package |
| Driver plugin (system package) | [`corsair-v2w`](../borochid-driver-corsair-v2w) | distro/repo GPG signature, installed as root by the package manager |
| Device package (data) | [`corsair.virtuoso`](../borochid-corsair-virtuoso) | ed25519 signature by a key pinned per registry |

A device package names its driver and the system package that provides it:

```json
"driver": {"type": "corsair-v2w", "version": ">=0.1,<1", "provided_by": "borochid-driver-corsair-v2w"}
```

If the driver is missing or incompatible, the device reports `needs_driver`
without being opened. `provided_by` must match `borochid-driver-*`, so a
package can't steer the installer toward arbitrary system packages. After
installing, the GUI calls `device.retry` and the service rescans its plugins.

Driver plugins register in the `borochid.drivers` entry point group and
subclass `borochid.service.drivers.Driver`. Drivers get per-device persisted
`settings` and `host` services, each enabled by a manifest section, so
plugins never spawn processes or touch the host themselves:

* `audio` (`host.audio`): ALSA volume and sidetone, PipeWire mic mute,
  feedback tone. Its state and actions are namespaced (`audio.volume`,
  `audio.set_volume`), so manifests can bind UI to them directly.
* `power_supply` (`host.power`): the battery as the kernel already reports
  it (`/sys/class/power_supply`, e.g. from hid-logitech-hidpp), with no
  device access. State `power.level`, `power.charging`, `power.online`.
* `input` (`host.input`): a uinput virtual device, so a driver can replay a
  remapped button as a key chord, another mouse button or a wheel step.
  **Driver code only:** it refuses every RPC action, so nothing on the
  socket can make the service type. Keys come from an allow-list
  (`borochid.common.keys`, no power/sleep/lock keys, at most 6 per chord),
  a chord is held exactly as long as the physical button, and the device
  exists only while a driver has buttons taken over. Needs the `input`
  extra (python-evdev) and access to `/dev/uinput`.

Device access: driver packages ship narrow udev rules (exact vendor and
product IDs, `TAG+="uaccess"`). See `packaging/70-borochid.rules` for
devices on the built-in declarative driver.

## Package format

See `examples/packages/acme.macropad/manifest.json` (built-in declarative
driver) and [`corsair.virtuoso`](../borochid-corsair-virtuoso) (driver
plugin). Key sections:

* `driver`: driver type, compatible versions, `provided_by` (see above).
* `audio`: `{"match": ["vendor", "model"]}` enables the host audio service.
* `display_name`: rules that tidy the name a device reports, tried in
  order, e.g. `{"match": "corsair virtuoso {model:upper} *", "format":
  "Corsair Virtuoso {model}"}`. Matching is word by word (literal words
  ignore case; `{x}` captures a word, `{x:upper}` only a capitalised one;
  a final `*` matches the rest), deliberately not a regex, so package data
  can't make the service spend exponential time. Without a match the
  reported name is shown.
* `category`: what kind of device it is (`headset`, `headphones`,
  `speaker`, `microphone`, `keyboard`, `keypad`, `mouse`, `gamepad`,
  `tablet`, `webcam`, `other`). The GUI shows the matching icon from the
  desktop's icon theme when the package has no picture. Unknown values read
  as `other`. A `keyboard` gets a card two columns wide on the home grid,
  with its picture kept at its own (wide) aspect ratio.
* `image`: a picture of the device, e.g. `"images/virtuoso.png"`. A match
  rule can set its own `image` for models that look different. PNG only, at
  most 384 px on the short side, 768 px on the long one and 256 KiB; `borochid-registry check` and `build` refuse
  anything else. The service re-checks the picture and exports it to a
  content-addressed image store (`<cache>/images/<sha256>.png`); device
  summaries name the digest and the GUI reads the file from there, checking
  the digest and header again before decoding. A bad picture is skipped
  and the device falls back to its category icon.
* `summary`: `{"state": key, "map": {...}}`, the device list's status line
  taken from driver state (e.g. "Headset off" instead of "Ready").
* `match`: rules such as `{"bus": "usb", "vid": "0x1209", "pid": "0xb0c1"}`,
  or `pid_range`, or BLE `service_uuid` / `company_id` (+ `name_prefix`).
  The most specific rule wins. A rule with `"channel": null` recognises a
  device mode that has nothing to talk to (a dongle whose headset is off):
  the driver runs, but nothing is opened. `"connection"` says how a device
  matched by the rule is connected, `wireless` (receiver or dongle),
  `cable` or `bluetooth` (implied for BLE), and the GUI shows it as an icon.
* `channel`: `{"type": "hid", "interface": 1, "report_size": 32}`, `serial`
  (`baudrate`), `libusb` (`in_endpoint`/`out_endpoint`), `ble`
  (`notify`/`write` characteristic UUIDs).
* `state`, `inputs`, `actions`, `init`: declarative protocol (struct formats,
  `$param` bindings). Documented in `packages/service/src/borochid/service/drivers/declarative.py`.
* `battery`: `{"level": key, "charging": key}`, which driver state holds the
  battery. The level is a percentage; the GUI shows it the same way for every
  device (a battery icon on the device's tile and next to its status, with a
  bolt while charging). Drivers keep it current; there is no refresh button.
  Only `level` is required.
* `available`: when a plugged-in device can actually be used, as a state key
  (`"online"`, usable while truthy) or `{"state": "link", "values":
  ["online", "wired"]}`. While it isn't (a wireless headset switched off),
  the GUI fades its picture, hides its battery and disables its settings.
* `ui`: widget list: `readout` (optional `map`), `slider`, `spin`,
  `toggle`, `select`, `color` (a swatch plus a colour-picker button),
  `button`, `stages` (an editable list such as DPI stages: add, remove, pick the
  default and the current one), `buttons` (each button with its action
  spelled out; an editor dialog offers the package's `categories` and a
  shortcut recorder that works by physical key, with clickable modifiers
  for shortcuts the desktop keeps for itself; `packages/gui/src/borochid/gui/binding_editor.py`), `group` (a section; with two or more top-level groups the
  device page shows each as a section picked with an icon button: the
  group's `icon`, with its `label` on hover), `keyboard` (a drawing of the keys from a key map in the package, for
  per-key lighting or for picking keys such as the ones game mode disables;
  `packages/gui/src/borochid/gui/keyboard_widget.py`)
  and `row` (children side by side,
  each child's `label` shown just before it, e.g. a light's colour and
  brightness). Any widget can set `tooltip` and `enabled_if` (a state key; `"!key"`
  enables it while the key is falsy). An item's `"layout": "<section>"`
  names a top-level manifest section (e.g. `"keyboard": {"keys": [...]}`)
  that the service sends along with `ui`, so widgets that share data (and
  the driver) don't repeat it.
  `readout`, `slider` and `button` take an `icon`: a desktop theme icon name
  (`"view-refresh"`), or a list of names to cover different themes; a
  readout's icon can follow its value with `{"map": {value: name}}`. The
  device page shows settings on the left and the device (picture, name,
  status, battery) on the right: a top-level item joins the device column
  with `"section": "status"`, and top-level `readout`s go there by default. New widget types go in
  `packages/gui/src/borochid/gui/widgets.py` via `@widget("name")`.
* `simulation`: how the fake device behaves under `--sim`.

## Publishing a registry

```sh
borochid-registry keygen publisher             # publisher.key (secret), publisher.pub
borochid-registry check examples/packages
borochid-registry build examples/packages -o registry-out --key publisher.key
# upload registry-out/ to any static host (S3, GitHub Pages, nginx...)
```

Every archive is signed, and files other than data (`.json`, images, text)
are refused. `build` is deterministic and incremental: publishing one package
keeps the other packages' index entries.

## Configuration

`~/.config/borochid/config.toml`:

```toml
detectors = ["udev", "ble"]

[detector.ble]
stale_seconds = 45

[[registries]]
name = "official"
url = "https://registry.example.com/v1/"       # https required
keys = ["<base64 ed25519 public key>"]          # pinned, never fetched
```

Packages in `~/.local/share/borochid/local-packages/` are used unsigned and
take precedence over registries (they're data, and anything that can write
there already runs as the user).

## Service RPC

Newline-delimited JSON-RPC 2.0 at `$XDG_RUNTIME_DIR/borochid.sock`.

Methods: `service.info` (returns `version`, `protocol`), `devices.list {include_unsupported}`,
`device.get {uid}`, `device.invoke {uid, action, params}`, `device.retry {uid?}`,
`registry.refresh`, `profiles.list`, `profiles.select {id}`,
`profiles.add {name, duplicate}`, `profiles.rename {id, name}`, `profiles.remove {id}`.
Notifications: `device.added`, `device.changed`, `device.removed`,
`device.state {uid, changes}`, `profiles.changed`.

## Known limitations / next steps

* The platform itself isn't packaged yet (RPM/deb for `borochid-service`
  and `borochid-gui`, plus a systemd user unit and a polkit-friendly
  install), and driver packages depend on it.
* HID interface selection is by USB interface number. Usage-page matching
  needs report-descriptor parsing.
* Detection is Linux-only (udev, BlueZ). The `Detector`/`Channel` split is
  where other OS backends would plug in.
