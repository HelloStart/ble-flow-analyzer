from __future__ import annotations

import asyncio
from itertools import count
from time import monotonic
from typing import Any

from bleak import BleakScanner
from PySide6.QtCore import QThread, Signal

from .advertising import AdvertisementSnapshot, snapshot_to_event
from .domain import BleEvent


def advertisement_to_snapshot(device: Any, advertisement: Any) -> AdvertisementSnapshot:
    address = str(device.address)
    return AdvertisementSnapshot(
        device_id=address,
        address=address,
        name=str(device.name or advertisement.local_name or "Unknown"),
        local_name=advertisement.local_name,
        rssi=int(advertisement.rssi),
        tx_power=advertisement.tx_power,
        connectable=None,
        service_uuids=tuple(advertisement.service_uuids),
        service_data=dict(advertisement.service_data),
        manufacturer_data=dict(advertisement.manufacturer_data),
    )


class BleakScanThread(QThread):
    advertisement_received = Signal(object)
    scan_failed = Signal(str)
    scan_stopped = Signal()

    def __init__(self, scanning_mode: str = "active", parent: Any = None) -> None:
        super().__init__(parent)
        self.scanning_mode = scanning_mode
        self._stop_requested = False
        self._sequence = count(1)
        self._started_at = 0.0
        self._last_emitted: dict[str, tuple[float, tuple[Any, ...]]] = {}

    def request_stop(self) -> None:
        self._stop_requested = True

    def run(self) -> None:
        self._stop_requested = False
        self._started_at = monotonic()
        try:
            asyncio.run(self._scan())
        except Exception as error:
            self.scan_failed.emit(f"{type(error).__name__}: {error}")
        finally:
            self.scan_stopped.emit()

    async def _scan(self) -> None:
        scanner = BleakScanner(self._on_advertisement, scanning_mode=self.scanning_mode)
        await scanner.start()
        try:
            while not self._stop_requested:
                await asyncio.sleep(0.1)
        finally:
            await scanner.stop()

    def _on_advertisement(self, device: Any, advertisement: Any) -> None:
        snapshot = advertisement_to_snapshot(device, advertisement)
        now = monotonic()
        fingerprint = (
            snapshot.local_name,
            snapshot.tx_power,
            snapshot.service_uuids,
            tuple(sorted(snapshot.service_data.items())),
            tuple(sorted(snapshot.manufacturer_data.items())),
        )
        previous = self._last_emitted.get(snapshot.device_id)
        if previous is not None and previous[1] == fingerprint and now - previous[0] < 5.0:
            return
        self._last_emitted[snapshot.device_id] = (now, fingerprint)
        sequence = next(self._sequence)
        timestamp_ms = int((now - self._started_at) * 1000)
        event = snapshot_to_event(snapshot, f"api-{sequence}", timestamp_ms)
        self.advertisement_received.emit(event)