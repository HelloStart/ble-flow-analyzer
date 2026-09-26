from __future__ import annotations

from dataclasses import dataclass, replace
from itertools import count
from threading import Lock
from time import monotonic
from typing import Any

from PySide6.QtCore import QThread, Signal
from winrt.windows.devices.bluetooth.advertisement import (
    BluetoothLEAdvertisementType,
    BluetoothLEAdvertisementWatcher,
    BluetoothLEScanningMode,
)

from .domain import BleEvent, Evidence, PacketType


ADVERTISEMENT_TYPE_MAP = {
    "CONNECTABLE_UNDIRECTED": PacketType.ADV_IND,
    "CONNECTABLE_DIRECTED": PacketType.ADV_DIRECT_IND,
    "SCANNABLE_UNDIRECTED": PacketType.ADV_SCAN_IND,
    "NON_CONNECTABLE_UNDIRECTED": PacketType.ADV_NONCONN_IND,
    "SCAN_RESPONSE": PacketType.SCAN_RESPONSE,
    "EXTENDED": PacketType.EXTENDED_ADVERTISEMENT,
}


@dataclass(frozen=True, slots=True)
class WinRTAdvertisementRecord:
    bluetooth_address: int
    advertisement_type: str
    rssi: int
    local_name: str
    is_connectable: bool
    is_scannable: bool
    is_scan_response: bool
    is_directed: bool
    tx_power: int | None
    service_uuids: tuple[str, ...]
    data_sections: tuple[tuple[int, bytes], ...]


def format_bluetooth_address(value: int) -> str:
    octets = value.to_bytes(6, "big")
    return ":".join(f"{octet:02X}" for octet in octets)


def encode_data_sections(sections: tuple[tuple[int, bytes], ...]) -> bytes:
    encoded: list[bytes] = []
    for data_type, data in sections:
        length = len(data) + 1
        if length > 0xFF:
            raise ValueError("Advertising data section exceeds one-byte Length")
        encoded.append(bytes((length, data_type)) + data)
    return b"".join(encoded)


def record_to_event(record: WinRTAdvertisementRecord, event_id: str, timestamp_ms: int) -> BleEvent:
    packet_type = ADVERTISEMENT_TYPE_MAP.get(record.advertisement_type, PacketType.ADVERTISEMENT_UPDATE)
    address = format_bluetooth_address(record.bluetooth_address)
    fields = {
        "Address": address,
        "RSSI": f"{record.rssi} dBm",
        "Connectable": "Yes" if record.is_connectable else "No",
        "Scannable": "Yes" if record.is_scannable else "No",
        "Directed": "Yes" if record.is_directed else "No",
        "Scan Response": "Yes" if record.is_scan_response else "No",
        "Local Name": record.local_name or "Unknown",
        "Source": "Windows WinRT Advertisement Watcher",
        "Reported Type": record.advertisement_type,
    }
    if record.tx_power is not None:
        fields["TX Power"] = f"{record.tx_power} dBm"
    if record.service_uuids:
        fields["Service UUIDs"] = ", ".join(record.service_uuids)
    return BleEvent(
        event_id=event_id,
        timestamp_ms=timestamp_ms,
        packet_type=packet_type,
        device_id=address,
        direction="Peripheral -> Windows API",
        summary=f"{packet_type.value}：{record.local_name or address}",
        evidence=Evidence.API,
        raw_data=encode_data_sections(record.data_sections),
        fields=fields,
    )


def received_args_to_record(args: Any) -> WinRTAdvertisementRecord:
    advertisement = args.advertisement
    return WinRTAdvertisementRecord(
        bluetooth_address=int(args.bluetooth_address),
        advertisement_type=args.advertisement_type.name,
        rssi=int(args.raw_signal_strength_in_dbm),
        local_name=str(advertisement.local_name or ""),
        is_connectable=bool(args.is_connectable),
        is_scannable=bool(args.is_scannable),
        is_scan_response=bool(args.is_scan_response),
        is_directed=bool(args.is_directed),
        tx_power=args.transmit_power_level_in_dbm,
        service_uuids=tuple(str(uuid) for uuid in advertisement.service_uuids),
        data_sections=tuple((int(section.data_type), bytes(section.data)) for section in advertisement.data_sections),
    )


class WinRTScanThread(QThread):
    advertisements_received = Signal(object)
    scan_failed = Signal(str)
    scan_stopped = Signal()

    def __init__(self, scanning_mode: str = "active", parent: Any = None) -> None:
        super().__init__(parent)
        self.scanning_mode = scanning_mode
        self._stop_requested = False
        self._sequence = count(1)
        self._started_at = 0.0
        self._last_emitted: dict[tuple[str, PacketType], tuple[float, bytes, int]] = {}
        self._pending_events: list[BleEvent] = []
        self._pending_lock = Lock()

    def request_stop(self) -> None:
        self._stop_requested = True

    def run(self) -> None:
        self._stop_requested = False
        self._started_at = monotonic()
        watcher: BluetoothLEAdvertisementWatcher | None = None
        token: Any = None
        try:
            watcher = BluetoothLEAdvertisementWatcher()
            watcher.scanning_mode = (
                BluetoothLEScanningMode.ACTIVE
                if self.scanning_mode == "active"
                else BluetoothLEScanningMode.PASSIVE
            )
            watcher.allow_extended_advertisements = True
            token = watcher.add_received(self._on_advertisement)
            watcher.start()
            while not self._stop_requested:
                self.msleep(250)
                self._flush_pending_events()
        except Exception as error:
            self.scan_failed.emit(f"{type(error).__name__}: {error}")
        finally:
            if watcher is not None:
                try:
                    watcher.stop()
                    if token is not None:
                        watcher.remove_received(token)
                except Exception as error:
                    self.scan_failed.emit(f"停止 WinRT 扫描失败：{type(error).__name__}: {error}")
            self._flush_pending_events()
            self.scan_stopped.emit()

    def _on_advertisement(self, sender: Any, args: Any) -> None:
        try:
            record = received_args_to_record(args)
            event = record_to_event(
                record,
                f"winrt-{next(self._sequence)}",
                int((monotonic() - self._started_at) * 1000),
            )
            now = monotonic()
            key = (event.device_id or "", event.packet_type)
            previous = self._last_emitted.get(key)
            if previous is not None and previous[1] == event.raw_data and now - previous[0] < 2.0:
                return
            self._last_emitted[key] = (now, event.raw_data, record.rssi)
            if event.packet_type == PacketType.SCAN_RESPONSE and self.scanning_mode == "active":
                request_id = f"{event.event_id}-inferred-request"
                request = BleEvent(
                    event_id=request_id,
                    timestamp_ms=max(0, event.timestamp_ms - 1),
                    packet_type=PacketType.SCAN_REQUEST,
                    device_id=event.device_id,
                    direction="Windows Controller -> Peripheral",
                    summary=f"推断的 SCAN_REQ：{event.fields['Local Name']}",
                    evidence=Evidence.INFERRED,
                    fields={
                        "Source": "Inferred from WinRT ScanResponse",
                        "Raw Data": "Not exposed by Windows",
                    },
                )
                self._queue_event(request)
                event = replace(event, related_event_id=request_id)
            self._queue_event(event)
        except Exception as error:
            self.scan_failed.emit(f"解析 WinRT 广播失败：{type(error).__name__}: {error}")

    def _queue_event(self, event: BleEvent) -> None:
        with self._pending_lock:
            self._pending_events.append(event)

    def _flush_pending_events(self) -> None:
        with self._pending_lock:
            if not self._pending_events:
                return
            events = self._pending_events
            self._pending_events = []
        self.advertisements_received.emit(events)