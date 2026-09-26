from .domain import BleEvent, Evidence, PacketType


def demo_scan_events() -> list[BleEvent]:
    return [
        BleEvent("scan-1", 0, PacketType.SCAN_STARTED, None, "APP -> OS", "主动扫描开始", Evidence.SYSTEM),
        BleEvent(
            "thermo-adv-1", 124, PacketType.ADV_IND, "thermo", "Peripheral -> Scanner",
            "首次发现 Thermo", Evidence.CAPTURED, bytes.fromhex("020106030309180708546865726d6f"),
            {"Address": "C1:32:8A:11:20:7F", "RSSI": "-52 dBm", "Connectable": "Yes", "Local Name": "Thermo", "Service UUID": "0x1809"},
        ),
        BleEvent(
            "thermo-req-1", 125, PacketType.SCAN_REQUEST, "thermo", "Scanner -> Peripheral",
            "请求补充广播信息", Evidence.INFERRED, related_event_id="thermo-adv-1",
        ),
        BleEvent(
            "thermo-rsp-1", 127, PacketType.SCAN_RESPONSE, "thermo", "Peripheral -> Scanner",
            "收到完整设备名称", Evidence.CAPTURED, bytes.fromhex("0c09546865726d6f6d65746572"),
            {"Address": "C1:32:8A:11:20:7F", "RSSI": "-50 dBm", "Connectable": "Yes", "Local Name": "Thermometer", "Manufacturer Data": "4C 00 02 15"},
            related_event_id="thermo-adv-1",
        ),
        BleEvent(
            "heart-adv-1", 310, PacketType.ADV_IND, "heart", "Peripheral -> Scanner",
            "首次发现 Heart Rate", Evidence.CAPTURED, bytes.fromhex("02010603030d18"),
            {"Address": "E8:20:37:A4:91:02", "RSSI": "-65 dBm", "Connectable": "Yes", "Local Name": "Heart Rate", "Service UUID": "0x180D"},
        ),
        BleEvent(
            "beacon-adv-1", 486, PacketType.ADV_IND, "beacon", "Peripheral -> Scanner",
            "首次发现 Beacon", Evidence.CAPTURED, bytes.fromhex("0201041aff4c00"),
            {"Address": "70:B3:D5:08:44:10", "RSSI": "-81 dBm", "Connectable": "No", "Local Name": "Beacon", "Manufacturer Data": "4C 00"},
        ),
    ]


def demo_connection_events(device_id: str, name: str, connectable: bool) -> list[BleEvent]:
    events = [
        BleEvent(
            f"{device_id}-selected", 900, PacketType.DEVICE_SELECTED, device_id,
            "User -> App", f"选择设备 {name}", Evidence.SYSTEM,
        ),
        BleEvent(
            "scan-stopped", 910, PacketType.SCAN_STOPPED, None,
            "APP -> OS", "连接前停止扫描", Evidence.SYSTEM,
        ),
        BleEvent(
            f"{device_id}-connect-start", 930, PacketType.CONNECTION_STARTED, device_id,
            "APP -> OS", f"请求连接 {name}", Evidence.SYSTEM,
        ),
    ]
    result_type = PacketType.CONNECTION_SUCCEEDED if connectable else PacketType.CONNECTION_FAILED
    fields = (
        {"Status": "Connected", "Transport": "Bluetooth LE"}
        if connectable
        else {"Reason": "该广播设备不可连接"}
    )
    events.append(
        BleEvent(
            f"{device_id}-connect-result", 1280, result_type, device_id,
            "OS -> APP", f"{'已连接' if connectable else '无法连接'} {name}",
            Evidence.SYSTEM, fields=fields,
            related_event_id=f"{device_id}-connect-start",
        )
    )
    return events
