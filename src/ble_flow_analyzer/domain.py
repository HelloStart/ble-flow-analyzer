from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class PacketType(StrEnum):
    SCAN_STARTED = "SCAN_STARTED"
    SCAN_STOPPED = "SCAN_STOPPED"
    ADVERTISEMENT_UPDATE = "ADVERTISEMENT_UPDATE"
    ADV_IND = "ADV_IND"
    ADV_DIRECT_IND = "ADV_DIRECT_IND"
    ADV_NONCONN_IND = "ADV_NONCONN_IND"
    ADV_SCAN_IND = "ADV_SCAN_IND"
    EXTENDED_ADVERTISEMENT = "EXTENDED_ADVERTISEMENT"
    SCAN_REQUEST = "SCAN_REQ"
    SCAN_RESPONSE = "SCAN_RSP"
    DEVICE_SELECTED = "DEVICE_SELECTED"
    CONNECTION_STARTED = "CONNECTION_STARTED"
    CONNECTION_SUCCEEDED = "CONNECTION_SUCCEEDED"
    CONNECTION_FAILED = "CONNECTION_FAILED"
    CONNECTION_PARAMETERS_OBSERVED = "CONNECTION_PARAMETERS_OBSERVED"
    CONNECTION_PARAMETERS_CHANGED = "CONNECTION_PARAMETERS_CHANGED"
    CONNECTION_PHY_OBSERVED = "CONNECTION_PHY_OBSERVED"
    CONNECTION_PHY_CHANGED = "CONNECTION_PHY_CHANGED"
    CONNECTION_OBSERVATION_UNAVAILABLE = "CONNECTION_OBSERVATION_UNAVAILABLE"
    DISCONNECT_REQUESTED = "DISCONNECT_REQUESTED"
    DISCONNECTED = "DISCONNECTED"
    GATT_SERVICES_DISCOVERED = "GATT_SERVICES_DISCOVERED"
    GATT_READ_REQUESTED = "GATT_READ_REQUESTED"
    GATT_READ_COMPLETED = "GATT_READ_COMPLETED"
    GATT_WRITE_REQUESTED = "GATT_WRITE_REQUESTED"
    GATT_WRITE_COMPLETED = "GATT_WRITE_COMPLETED"
    GATT_NOTIFY_ENABLED = "GATT_NOTIFY_ENABLED"
    GATT_NOTIFY_DISABLED = "GATT_NOTIFY_DISABLED"
    GATT_NOTIFICATION_RECEIVED = "GATT_NOTIFICATION_RECEIVED"


class Evidence(StrEnum):
    CAPTURED = "captured"
    API = "api"
    INFERRED = "inferred"
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class BleEvent:
    event_id: str
    timestamp_ms: int
    packet_type: PacketType
    device_id: str | None
    direction: str
    summary: str
    evidence: Evidence
    raw_data: bytes = b""
    fields: dict[str, str] = field(default_factory=dict)
    related_event_id: str | None = None


@dataclass(slots=True)
class DiscoveredDevice:
    device_id: str
    name: str
    address: str
    rssi: int
    connectable: bool | None
    packet_count: int = 0
    last_seen_ms: int = 0

    def observe(self, event: BleEvent) -> None:
        self.packet_count += 1
        self.last_seen_ms = event.timestamp_ms
        if "RSSI" in event.fields:
            self.rssi = int(event.fields["RSSI"].removesuffix(" dBm"))
        observed_connectable = {"Yes": True, "No": False}.get(event.fields.get("Connectable", "Unknown"))
        if observed_connectable is True or self.connectable is None:
            self.connectable = observed_connectable


def aggregate_devices(events: list[BleEvent]) -> list[DiscoveredDevice]:
    devices: dict[str, DiscoveredDevice] = {}
    for event in events:
        if event.device_id is None or event.packet_type not in {
            PacketType.ADVERTISEMENT_UPDATE,
            PacketType.ADV_IND,
            PacketType.ADV_DIRECT_IND,
            PacketType.ADV_NONCONN_IND,
            PacketType.ADV_SCAN_IND,
            PacketType.EXTENDED_ADVERTISEMENT,
            PacketType.SCAN_RESPONSE,
        }:
            continue
        device = devices.get(event.device_id)
        if device is None:
            device = DiscoveredDevice(
                device_id=event.device_id,
                name=event.fields.get("Local Name", "Unknown"),
                address=event.fields.get("Address", event.device_id),
                rssi=int(event.fields.get("RSSI", "-127 dBm").removesuffix(" dBm")),
                connectable={"Yes": True, "No": False}.get(event.fields.get("Connectable", "Unknown")),
            )
            devices[event.device_id] = device
        if event.fields.get("Local Name"):
            device.name = event.fields["Local Name"]
        device.observe(event)
    return list(devices.values())


def events_for_device(events: list[BleEvent], device_id: str | None) -> list[BleEvent]:
    if device_id is None:
        return events
    return [event for event in events if event.device_id in {None, device_id}]
