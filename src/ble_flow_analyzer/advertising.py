from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from .domain import BleEvent, Evidence, PacketType


BASE_UUID_SUFFIX = "-0000-1000-8000-00805f9b34fb"

FLAGS_BITS = (
    (0, "LE Limited Discoverable Mode"),
    (1, "LE General Discoverable Mode"),
    (2, "BR/EDR Not Supported"),
    (3, "Simultaneous LE and BR/EDR（Controller）"),
    (4, "Simultaneous LE and BR/EDR（Host）"),
)


@dataclass(frozen=True, slots=True)
class AdvertisementSnapshot:
    device_id: str
    address: str
    name: str
    rssi: int
    local_name: str | None = None
    tx_power: int | None = None
    connectable: bool | None = None
    service_uuids: tuple[str, ...] = ()
    service_data: dict[str, bytes] = field(default_factory=dict)
    manufacturer_data: dict[int, bytes] = field(default_factory=dict)


def decode_flags(value: bytes) -> tuple[str, ...]:
    if len(value) != 1:
        return (f"Flags 长度应为 1 byte，当前为 {len(value)} bytes",)
    flags = value[0]
    lines = [f"Flags: 0x{flags:02X} = {flags:08b}b"]
    for bit, name in FLAGS_BITS:
        bit_value = (flags >> bit) & 1
        lines.append(f"bit{bit} = {bit_value}  {name}")
    reserved = (flags >> 5) & 0x07
    reserved_note = "" if reserved == 0 else "（存在非零保留位）"
    lines.append(f"bit5-7 = {reserved:03b}  Reserved{reserved_note}")
    discoverability = (
        "LE Limited Discoverable Mode"
        if flags & 0x01
        else "LE General Discoverable Mode"
        if flags & 0x02
        else "未声明可发现模式"
    )
    transport = "仅支持 LE，不支持 BR/EDR" if flags & 0x04 else "未声明不支持 BR/EDR"
    lines.append(f"结论：{discoverability}；{transport}")
    return tuple(lines)


def _ad_structure(ad_type: int, value: bytes) -> bytes:
    if len(value) > 254:
        raise ValueError("AD Structure value exceeds 254 bytes")
    return bytes((len(value) + 1, ad_type)) + value


def _uuid_width_and_bytes(value: str) -> tuple[int, bytes]:
    normalized = value.lower()
    if normalized.startswith("0000") and normalized.endswith(BASE_UUID_SUFFIX):
        return 16, int(normalized[4:8], 16).to_bytes(2, "little")
    uuid = UUID(value)
    return 128, uuid.bytes_le


def reconstruct_advertising_data(snapshot: AdvertisementSnapshot) -> bytes:
    structures: list[bytes] = []
    uuids_16: list[bytes] = []
    uuids_128: list[bytes] = []
    for uuid in snapshot.service_uuids:
        width, encoded = _uuid_width_and_bytes(uuid)
        (uuids_16 if width == 16 else uuids_128).append(encoded)
    if uuids_16:
        structures.append(_ad_structure(0x03, b"".join(uuids_16)))
    if uuids_128:
        structures.append(_ad_structure(0x07, b"".join(uuids_128)))
    if snapshot.local_name:
        structures.append(_ad_structure(0x09, snapshot.local_name.encode("utf-8")))
    if snapshot.tx_power is not None:
        structures.append(_ad_structure(0x0A, snapshot.tx_power.to_bytes(1, "little", signed=True)))
    for uuid, data in snapshot.service_data.items():
        width, encoded = _uuid_width_and_bytes(uuid)
        structures.append(_ad_structure(0x16 if width == 16 else 0x21, encoded + data))
    for company_id, data in snapshot.manufacturer_data.items():
        structures.append(_ad_structure(0xFF, company_id.to_bytes(2, "little") + data))
    return b"".join(structures)


def snapshot_to_event(snapshot: AdvertisementSnapshot, event_id: str, timestamp_ms: int) -> BleEvent:
    connectable = {True: "Yes", False: "No", None: "Unknown"}[snapshot.connectable]
    fields = {
        "Address": snapshot.address,
        "RSSI": f"{snapshot.rssi} dBm",
        "Connectable": connectable,
        "Local Name": snapshot.local_name or snapshot.name or "Unknown",
        "Source": "Bleak / OS Bluetooth API",
        "PDU Type": "Not exposed by API",
    }
    if snapshot.tx_power is not None:
        fields["TX Power"] = f"{snapshot.tx_power} dBm"
    if snapshot.service_uuids:
        fields["Service UUIDs"] = ", ".join(snapshot.service_uuids)
    return BleEvent(
        event_id=event_id,
        timestamp_ms=timestamp_ms,
        packet_type=PacketType.ADVERTISEMENT_UPDATE,
        device_id=snapshot.device_id,
        direction="OS API -> App",
        summary=f"扫描结果更新：{fields['Local Name']}",
        evidence=Evidence.API,
        raw_data=reconstruct_advertising_data(snapshot),
        fields=fields,
    )