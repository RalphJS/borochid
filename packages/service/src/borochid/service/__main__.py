from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from pathlib import Path

from borochid.service import plugins
from borochid.service.config import Config
from borochid.service.detectors import Detector
from borochid.service.devices import DeviceManager
from borochid.service.registry.client import Registry
from borochid.service.server import RpcServer

log = logging.getLogger("borochid.service")


async def run(cfg: Config) -> None:
    server = RpcServer(cfg.socket_path)
    registry = Registry(cfg)
    manager = DeviceManager(registry, server.broadcast)
    server.manager = manager
    await server.start()

    detectors: list[Detector] = []
    for name in cfg.detectors:
        try:
            det = plugins.load(plugins.DETECTORS, name)(manager, cfg.detector_options.get(name))
            await det.start()
        except ImportError as e:
            log.warning("detector %r unavailable (%s); skipping", name, e)
            continue
        except Exception:
            log.exception("detector %r failed to start; skipping", name)
            continue
        detectors.append(det)
        server.detectors[name] = det
        log.info("detector %s started", name)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    log.info("shutting down")
    for det in detectors:
        await det.stop()
    await manager.shutdown()
    await server.stop()
    await registry.aclose()


def main() -> None:
    ap = argparse.ArgumentParser(prog="borochid-service")
    ap.add_argument("-c", "--config", type=Path, help="config.toml path")
    ap.add_argument("--sim", action="store_true", help="add simulated devices")
    ap.add_argument("--local-packages", type=Path, help="override local package directory")
    ap.add_argument(
        "-v", "--verbose", action="count", default=0, help="-v: Borochid debug output; -vv: also library debug output"
    )
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose >= 2 else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.verbose:
        for name in ("borochid", "borochid_corsair_v2w"):
            logging.getLogger(name).setLevel(logging.DEBUG)
    cfg = Config.load(args.config)
    if args.sim and "sim" not in cfg.detectors:
        cfg.detectors.append("sim")
    if args.local_packages:
        cfg.local_packages_dir = args.local_packages
    asyncio.run(run(cfg))


if __name__ == "__main__":
    main()
