from __future__ import annotations

import asyncio
from queue import Empty, Queue
from dataclasses import dataclass
from itertools import count
from time import monotonic
from typing import Any

from bleak import BleakClient
from PySide6.QtCore import QThread, Signal
from winrt.windows.devices.bluetooth import BluetoothLEDevice

from .domain import BleEvent, Evidence, PacketType


@dataclass(frozen=True, slots=True)
class ConnectionParametersSnapshot:
    interval_units: int
    latency_events: int
    timeout_units: int

    @property
    def interval_ms(self) -> float:
        return self.interval_units * 1.25

    @property
    def timeout_ms(self) -> int:
        return self.timeout_units * 10

    @property
    def minimum_safe_timeout_ms(self) -> float:
        return 2 * (1 + self.latency_events) * self.interval_ms

    @property
    def is_valid(self) -> bool:
        return self.interval_units > 0 and self.timeout_units > 0

    def as_fields(self) -> dict[str, str]:
        relation = "满足" if self.timeout_ms > self.minimum_safe_timeout_ms else "不满足"
        return {
            "Connection Interval": f"{self.interval_ms:g} ms (0x{self.interval_units:04X})",
            "Peripheral Latency": f"{self.latency_events} connection events",
            "Supervision Timeout": f"{self.timeout_ms:g} ms (0x{self.timeout_units:04X})",
            "Timeout Constraint": (
                f"{relation}: {self.timeout_ms:g} ms > "
                f"2 x (1 + {self.latency_events}) x {self.interval_ms:g} ms"
            ),
            "Source": "Windows WinRT connection parameters",
        }


@dataclass(frozen=True, slots=True)
class ConnectionPhySnapshot:
    le_1m: bool
    le_2m: bool
    le_coded: bool

    def as_fields(self) -> dict[str, str]:
        active = [
            name
            for enabled, name in (
                (self.le_1m, "LE 1M"),
                (self.le_2m, "LE 2M"),
                (self.le_coded, "LE Coded"),
            )
            if enabled
        ]
        return {
            "PHY": ", ".join(active) or "Unknown",
            "LE 1M": "Yes" if self.le_1m else "No",
            "LE 2M": "Yes" if self.le_2m else "No",
            "LE Coded": "Yes" if self.le_coded else "No",
            "Source": "Windows WinRT connection PHY",
        }


def parameters_from_winrt(value: Any) -> ConnectionParametersSnapshot:
    return ConnectionParametersSnapshot(
        interval_units=int(value.connection_interval),
        latency_events=int(value.connection_latency),
        timeout_units=int(value.link_timeout),
    )


def phy_from_winrt(value: Any) -> ConnectionPhySnapshot:
    return ConnectionPhySnapshot(
        le_1m=bool(value.is_uncoded1_m_phy),
        le_2m=bool(value.is_uncoded2_m_phy),
        le_coded=bool(value.is_coded_phy),
    )


@dataclass(frozen=True, slots=True)
class GattCharacteristicInfo:
    service_uuid: str
    uuid: str
    handle: int
    properties: str
    descriptors: tuple[str, ...]


def gatt_characteristics_from_services(services: Any) -> tuple[GattCharacteristicInfo, ...]:
    result: list[GattCharacteristicInfo] = []
    for service in services:
        for characteristic in service.characteristics:
            result.append(
                GattCharacteristicInfo(
                    service_uuid=str(service.uuid),
                    uuid=str(characteristic.uuid),
                    handle=int(characteristic.handle),
                    properties=", ".join(sorted(str(value) for value in characteristic.properties)),
                    descriptors=tuple(str(descriptor.uuid) for descriptor in characteristic.descriptors),
                )
            )
    return tuple(result)


class ConnectionThread(QThread):
    events_received = Signal(object)
    services_received = Signal(object)
    connected = Signal()
    session_finished = Signal(bool)

    def __init__(
        self,
        address: str,
        name: str,
        base_timestamp_ms: int = 0,
        initiating_hint: str = "Unknown",
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self.address = address
        self.name = name
        self.base_timestamp_ms = base_timestamp_ms
        self.initiating_hint = initiating_hint
        self._started_at = 0.0
        self._disconnect_requested = False
        self._bleak_disconnected = False
        self._connected_once = False
        self._event_sequence = count(1)
        self._commands: Queue[tuple[str, str, bytes | None, bool | None]] = Queue()
        self._client: BleakClient | None = None

    def request_disconnect(self) -> None:
        self._disconnect_requested = True

    def request_read(self, characteristic_uuid: str) -> None:
        self._commands.put(("read", characteristic_uuid, None, None))

    def request_write(self, characteristic_uuid: str, data: bytes, response: bool | None) -> None:
        self._commands.put(("write", characteristic_uuid, data, response))

    def request_notify(self, characteristic_uuid: str, enabled: bool) -> None:
        self._commands.put(("notify", characteristic_uuid, None, enabled))

    def request_discover_services(self) -> None:
        self._commands.put(("discover_services", "", None, None))

    def run(self) -> None:
        self._started_at = monotonic()
        try:
            asyncio.run(self._connection_session())
        except Exception as error:
            if self._connected_once:
                self._emit(
                    PacketType.DISCONNECTED,
                    f"连接会话异常结束：{self.name}",
                    "OS API -> App",
                    fields={
                        "Reason": "Connection session error",
                        "Error": f"{type(error).__name__}: {error}",
                        "HCI Error Code": "Not exposed by Bleak/WinRT",
                    },
                )
            else:
                self._emit(
                    PacketType.CONNECTION_FAILED,
                    f"连接失败：{self.name}",
                    "OS API -> App",
                    fields={"Error": f"{type(error).__name__}: {error}", "Address": self.address},
                )
        finally:
            self.session_finished.emit(self._connected_once)

    async def _connection_session(self) -> None:
        self._emit(
            PacketType.CONNECTION_STARTED,
            f"请求连接 {self.name}",
            "App -> Bleak",
            fields={
                "Address": self.address,
                "Transport": "Bluetooth LE",
                "Last Connectable Advertisement": self.initiating_hint,
                "Protocol Reference": (
                    "CONNECT_IND likely" if self.initiating_hint in {"ADV_IND", "ADV_DIRECT_IND"}
                    else "AUX_CONNECT_REQ possible" if self.initiating_hint == "EXTENDED_ADVERTISEMENT"
                    else "CONNECT_IND or AUX_CONNECT_REQ; API cannot confirm"
                ),
            },
        )
        client = BleakClient(
            self.address,
            disconnected_callback=self._on_bleak_disconnected,
            timeout=20.0,
        )
        self._client = client
        winrt_device: BluetoothLEDevice | None = None
        tokens: list[tuple[str, Any]] = []
        try:
            await client.connect()
            if not client.is_connected:
                raise RuntimeError("Bleak connect completed without a connected state")
            self._connected_once = True
            self._emit(
                PacketType.CONNECTION_SUCCEEDED,
                f"已连接 {self.name}",
                "Bleak -> App",
                fields={
                    "Address": self.address,
                    "Status": "Connected",
                    "MTU (API)": str(client.mtu_size),
                    "Service Discovery": "Deferred from the learning flow; Windows may prepare GATT internally",
                },
            )
            self.connected.emit()

            try:
                winrt_device = await BluetoothLEDevice.from_bluetooth_address_async(self._address_as_int())
                if winrt_device is not None:
                    tokens = self._subscribe_winrt(winrt_device)
                    self._emit_current_parameters(winrt_device, changed=False)
                    self._emit_current_phy(winrt_device, changed=False)
                else:
                    self._emit_observation_unavailable(
                        "WinRT Connection Observer",
                        RuntimeError("BluetoothLEDevice was not available"),
                    )
            except Exception as error:
                self._emit_observation_unavailable("WinRT Connection Observer", error)
                winrt_device = None
                tokens = []

            while client.is_connected and not self._bleak_disconnected:
                await self._process_one_command(client)
                if self._disconnect_requested:
                    self._emit(
                        PacketType.DISCONNECT_REQUESTED,
                        f"请求断开 {self.name}",
                        "App -> Bleak",
                        fields={"Reason": "User requested disconnect"},
                    )
                    await client.disconnect()
                    break
                await asyncio.sleep(0.1)

            reason = "User requested disconnect" if self._disconnect_requested else "Remote device or Windows ended the connection"
            self._emit(
                PacketType.DISCONNECTED,
                f"已断开 {self.name}",
                "Bleak / WinRT -> App",
                fields={
                    "Reason": reason,
                    "HCI Error Code": "Not exposed by Bleak/WinRT",
                },
            )
        finally:
            if winrt_device is not None:
                self._unsubscribe_winrt(winrt_device, tokens)
                winrt_device.close()
            if client.is_connected:
                await client.disconnect()
            self._client = None

    async def _process_one_command(self, client: BleakClient) -> None:
        try:
            operation, characteristic_uuid, data, response = self._commands.get_nowait()
        except Empty:
            await asyncio.sleep(0.05)
            return
        if operation == "discover_services":
            try:
                services = client.services
                characteristics = gatt_characteristics_from_services(services)
                self._emit(
                    PacketType.GATT_SERVICES_DISCOVERED,
                    f"发现 {len(services.services)} 个 Service，{len(characteristics)} 个 Characteristic",
                    "Bleak -> App",
                    {
                        "Services": str(len(services.services)),
                        "Characteristics": str(len(characteristics)),
                        "Discovery": "User-triggered Bleak GATT service collection",
                    },
                )
                self.services_received.emit(characteristics)
            except Exception as error:
                self._emit(
                    PacketType.GATT_SERVICES_DISCOVERED,
                    "GATT 服务发现不可用",
                    "Bleak -> App",
                    {"Error": f"{type(error).__name__}: {error}"},
                )
        elif operation == "read":
            self._emit(PacketType.GATT_READ_REQUESTED, f"读取 Characteristic {characteristic_uuid}", "App -> Bleak", {"Characteristic": characteristic_uuid})
            try:
                value = bytes(await client.read_gatt_char(characteristic_uuid))
                self._emit(PacketType.GATT_READ_COMPLETED, f"读取完成 {characteristic_uuid}", "Bleak -> App", {"Characteristic": characteristic_uuid, "Value HEX": value.hex(" ").upper()}, value)
            except Exception as error:
                self._emit(PacketType.GATT_READ_COMPLETED, f"读取失败 {characteristic_uuid}", "Bleak -> App", {"Characteristic": characteristic_uuid, "Error": f"{type(error).__name__}: {error}"})
        elif operation == "write" and data is not None:
            self._emit(PacketType.GATT_WRITE_REQUESTED, f"写入 Characteristic {characteristic_uuid}", "App -> Bleak", {"Characteristic": characteristic_uuid, "Value HEX": data.hex(" ").upper(), "Response": str(response)})
            try:
                await client.write_gatt_char(characteristic_uuid, data, response=response)
                self._emit(PacketType.GATT_WRITE_COMPLETED, f"写入完成 {characteristic_uuid}", "Bleak -> App", {"Characteristic": characteristic_uuid, "Value HEX": data.hex(" ").upper()})
            except Exception as error:
                self._emit(PacketType.GATT_WRITE_COMPLETED, f"写入失败 {characteristic_uuid}", "Bleak -> App", {"Characteristic": characteristic_uuid, "Error": f"{type(error).__name__}: {error}"})
        elif operation == "notify":
            if response:
                callback = self._notification_callback
                try:
                    await client.start_notify(characteristic_uuid, callback)
                    self._emit(PacketType.GATT_NOTIFY_ENABLED, f"已订阅 {characteristic_uuid}", "Bleak -> App", {"Characteristic": characteristic_uuid})
                except Exception as error:
                    self._emit(PacketType.GATT_NOTIFY_ENABLED, f"订阅失败 {characteristic_uuid}", "Bleak -> App", {"Characteristic": characteristic_uuid, "Error": f"{type(error).__name__}: {error}"})
            else:
                try:
                    await client.stop_notify(characteristic_uuid)
                    self._emit(PacketType.GATT_NOTIFY_DISABLED, f"已取消订阅 {characteristic_uuid}", "App -> Bleak", {"Characteristic": characteristic_uuid})
                except Exception as error:
                    self._emit(PacketType.GATT_NOTIFY_DISABLED, f"取消订阅失败 {characteristic_uuid}", "Bleak -> App", {"Characteristic": characteristic_uuid, "Error": f"{type(error).__name__}: {error}"})

    def _notification_callback(self, characteristic: Any, data: bytearray) -> None:
        value = bytes(data)
        self._emit(PacketType.GATT_NOTIFICATION_RECEIVED, f"收到 Notification {characteristic.uuid}", "Peripheral -> App", {"Characteristic": str(characteristic.uuid), "Value HEX": value.hex(" ").upper()}, value)

    def _subscribe_winrt(self, device: BluetoothLEDevice) -> list[tuple[str, Any]]:
        return [
            ("parameters", device.add_connection_parameters_changed(self._on_parameters_changed)),
            ("phy", device.add_connection_phy_changed(self._on_phy_changed)),
            ("status", device.add_connection_status_changed(self._on_status_changed)),
        ]

    @staticmethod
    def _unsubscribe_winrt(device: BluetoothLEDevice, tokens: list[tuple[str, Any]]) -> None:
        for kind, token in tokens:
            if kind == "parameters":
                device.remove_connection_parameters_changed(token)
            elif kind == "phy":
                device.remove_connection_phy_changed(token)
            else:
                device.remove_connection_status_changed(token)

    def _on_bleak_disconnected(self, client: BleakClient) -> None:
        self._bleak_disconnected = True

    def _on_parameters_changed(self, sender: Any, args: Any) -> None:
        self._emit_current_parameters(sender, changed=True)

    def _on_phy_changed(self, sender: Any, args: Any) -> None:
        self._emit_current_phy(sender, changed=True)

    def _on_status_changed(self, sender: Any, args: Any) -> None:
        if sender.connection_status.name == "DISCONNECTED":
            self._bleak_disconnected = True

    def _emit_current_parameters(self, device: BluetoothLEDevice, changed: bool) -> None:
        try:
            snapshot = parameters_from_winrt(device.get_connection_parameters())
            if not snapshot.is_valid:
                return
            event_type = (
                PacketType.CONNECTION_PARAMETERS_CHANGED
                if changed
                else PacketType.CONNECTION_PARAMETERS_OBSERVED
            )
            summary = "连接参数已更新" if changed else "读取初始连接参数"
            self._emit(event_type, summary, "WinRT -> App", fields=snapshot.as_fields())
        except Exception as error:
            if not changed:
                self._emit_observation_unavailable("Connection Parameters", error)

    def _emit_current_phy(self, device: BluetoothLEDevice, changed: bool) -> None:
        try:
            snapshot = phy_from_winrt(device.get_connection_phy())
            event_type = PacketType.CONNECTION_PHY_CHANGED if changed else PacketType.CONNECTION_PHY_OBSERVED
            summary = "连接 PHY 已更新" if changed else "读取初始连接 PHY"
            self._emit(event_type, summary, "WinRT -> App", fields=snapshot.as_fields())
        except Exception as error:
            if not changed:
                self._emit_observation_unavailable("Connection PHY", error)

    def _emit_observation_unavailable(self, feature: str, error: Exception) -> None:
        self._emit(
            PacketType.CONNECTION_OBSERVATION_UNAVAILABLE,
            f"{feature} 不可用",
            "WinRT -> App",
            fields={"Feature": feature, "Error": f"{type(error).__name__}: {error}"},
        )

    def _emit(
        self,
        packet_type: PacketType,
        summary: str,
        direction: str,
        fields: dict[str, str],
        raw_data: bytes = b"",
    ) -> None:
        timestamp_ms = self.base_timestamp_ms + int((monotonic() - self._started_at) * 1000)
        event = BleEvent(
            event_id=f"connection-{next(self._event_sequence)}-{packet_type.value}",
            timestamp_ms=timestamp_ms,
            packet_type=packet_type,
            device_id=self.address,
            direction=direction,
            summary=summary,
            evidence=Evidence.API,
            fields=fields,
            raw_data=raw_data,
        )
        self.events_received.emit([event])

    def _address_as_int(self) -> int:
        return int(self.address.replace(":", "").replace("-", ""), 16)