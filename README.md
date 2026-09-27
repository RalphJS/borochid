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
`settings` and `host` services: `host.audio` (ALSA volume and sidetone,
PipeWire mic mute, feedback tone) is enabled by a manifest `audio` section,
so plugins never spawn processes. Its state and actions are namespaced
(`audio.volume`, `audio.set_volume`), so manifests can bind UI to them
directly.

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
* `summary`: `{"state": key, "map": {...}}`, the device list's status line
  taken from driver state (e.g. "Headset off" instead of "Ready").
* `match`: rules such as `{"bus": "usb", "vid": "0x1209", "pid": "0xb0c1"}`,
  or `pid_range`, or BLE `service_uuid` / `company_id` (+ `name_prefix`).
  The most specific rule wins. A rule with `"channel": null` recognises a
  device mode that has nothing to talk to (a dongle whose headset is off):
  the driver runs, but nothing is opened.
* `channel`: `{"type": "hid", "interface": 1, "report_size": 32}`, `serial`
  (`baudrate`), `libusb` (`in_endpoint`/`out_endpoint`), `ble`
  (`notify`/`write` characteristic UUIDs).
* `state`, `inputs`, `actions`, `init`: declarative protocol (struct formats,
  `$param` bindings). Documented in `packages/service/src/borochid/service/drivers/declarative.py`.
* `ui`: widget list (`readout` with an optional `map`, `slider`, `spin`,
  `toggle`, `select`, `color`, `button`, `group`). Any widget can set
  `enabled_if` to a state key. New widget types go in `packages/gui/src/borochid/gui/widgets.py` via `@widget("name")`.
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
`registry.refresh`. Notifications: `device.added`, `device.changed`, `device.removed`,
`device.state {uid, changes}`.

## Known limitations / next steps

* The platform itself isn't packaged yet (RPM/deb for `borochid-service`
  and `borochid-gui`, plus a systemd user unit and a polkit-friendly
  install), and driver packages depend on it.
* HID interface selection is by USB interface number. Usage-page matching
  needs report-descriptor parsing.
* Detection is Linux-only (udev, BlueZ). The `Detector`/`Channel` split is
  where other OS backends would plug in.
