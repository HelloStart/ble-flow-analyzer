from __future__ import annotations

from html import escape
import sys

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .advertising import decode_flags
from .ai_dialog import AiDialog
from .connection import ConnectionThread
from .domain import BleEvent, DiscoveredDevice, Evidence, PacketType, aggregate_devices, events_for_device
from .knowledge import KnowledgeBase
from .simulation import demo_connection_events, demo_scan_events
from .winrt_collector import WinRTScanThread


EVENT_COLORS = {
    Evidence.CAPTURED: "#16765d",
    Evidence.API: "#176b91",
    Evidence.INFERRED: "#9a6a08",
    Evidence.SYSTEM: "#556273",
}

MAX_VISIBLE_FLOW_EVENTS = 2000

class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("BLE Flow Analyzer v0.5")
        self.resize(1320, 780)
        self.all_events: list[BleEvent] = []
        self.pending_events: list[BleEvent] = []
        self.selected_device_id: str | None = None
        self.devices: dict[str, DiscoveredDevice] = {}
        self.event_items: dict[str, QTreeWidgetItem] = {}
        self.current_event: BleEvent | None = None
        self.knowledge = KnowledgeBase.load()
        self.real_scan_thread: WinRTScanThread | None = None
        self.real_scan_error = ""
        self.connection_thread: ConnectionThread | None = None
        self.pending_connection_device_id: str | None = None
        self.connected_device_id: str | None = None

        self.scan_timer = QTimer(self)
        self.scan_timer.setInterval(550)
        self.scan_timer.timeout.connect(self._emit_next_scan_event)

        self._build_ui()
        self._apply_style()
        self._set_empty_state()

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("BLE Flow Analyzer v0.5")
        title.setObjectName("title")
        subtitle = QLabel("扫描流程学习工作台")
        subtitle.setObjectName("subtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()
        self.source_badge = QLabel("演示数据")
        self.source_badge.setObjectName("badge")
        header.addWidget(self.source_badge)
        self.source_mode = QComboBox()
        self.source_mode.addItem("演示数据", "demo")
        self.source_mode.addItem("真实 BLE（WinRT）", "real")
        self.source_mode.currentIndexChanged.connect(self._source_changed)
        header.addWidget(self.source_mode)
        self.scan_mode = QComboBox()
        self.scan_mode.addItems(["主动扫描", "被动扫描"])
        self.scan_mode.setToolTip("Windows 通常只支持主动扫描；被动扫描取决于平台后端")
        header.addWidget(self.scan_mode)
        self.scan_button = QPushButton("开始扫描")
        self.scan_button.clicked.connect(self.start_scan)
        header.addWidget(self.scan_button)
        self.stop_button = QPushButton("停止")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_scan)
        header.addWidget(self.stop_button)
        self.import_button = QPushButton("导入")
        self.import_button.setToolTip("导入会话或抓包（尚未实现）")
        self.import_button.clicked.connect(self._show_import_placeholder)
        header.addWidget(self.import_button)
        layout.addLayout(header)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_device_panel())
        splitter.addWidget(self._build_flow_panel())
        splitter.addWidget(self._build_detail_panel())
        splitter.setSizes([290, 520, 420])
        splitter.setChildrenCollapsible(False)
        layout.addWidget(splitter, 1)

        self.status = QLabel("尚无扫描会话")
        self.status.setObjectName("status")
        layout.addWidget(self.status)
        self.setCentralWidget(root)

    def _panel(self, heading: str) -> tuple[QFrame, QVBoxLayout]:
        panel = QFrame()
        panel.setObjectName("panel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 14, 14, 14)
        label = QLabel(heading)
        label.setObjectName("panelTitle")
        layout.addWidget(label)
        return panel, layout

    def _build_device_panel(self) -> QWidget:
        panel, layout = self._panel("发现的设备")
        self.device_list = QListWidget()
        self.device_list.currentItemChanged.connect(self._device_selected)
        layout.addWidget(self.device_list, 1)
        self.show_all_button = QPushButton("显示全部扫描事件")
        self.show_all_button.clicked.connect(self._show_all_events)
        layout.addWidget(self.show_all_button)
        return panel

    def _build_flow_panel(self) -> QWidget:
        panel, layout = self._panel("扫描与连接流程")
        legend = QLabel("● 演示包   ■ API 数据   ◆ 系统事件   ◌ 协议推断")
        legend.setObjectName("legend")
        layout.addWidget(legend)
        self.flow_tree = QTreeWidget()
        self.flow_tree.setHeaderLabels(["时间", "类型", "方向与摘要"])
        self.flow_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.flow_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.flow_tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.flow_tree.currentItemChanged.connect(self._event_selected)
        layout.addWidget(self.flow_tree, 1)
        return panel

    def _build_detail_panel(self) -> QWidget:
        panel, layout = self._panel("事件详情")
        self.detail_title = QLabel("选择一个流程事件")
        self.detail_title.setObjectName("detailTitle")
        layout.addWidget(self.detail_title)
        self.detail_tabs = QTabWidget()

        overview = QWidget()
        self.overview_form = QFormLayout(overview)
        self.overview_labels = {
            key: QLabel("-") for key in ("类型", "时间", "方向", "证据", "关联事件", "错误")
        }
        for key, label in self.overview_labels.items():
            label.setWordWrap(True)
            self.overview_form.addRow(key, label)
        self.overview_knowledge = QTextEdit()
        self.overview_knowledge.setReadOnly(True)
        self.overview_knowledge.setObjectName("overviewKnowledge")
        self.overview_knowledge.setMinimumHeight(260)
        self.overview_form.addRow(self.overview_knowledge)
        self.detail_tabs.addTab(overview, "概览")

        self.learning_view = QTextEdit()
        self.learning_view.setReadOnly(True)
        self.learning_view.setObjectName("learningView")
        learning_page = QWidget()
        learning_layout = QVBoxLayout(learning_page)
        learning_layout.setContentsMargins(0, 0, 0, 0)
        learning_layout.addWidget(self.learning_view, 1)
        self.detail_tabs.addTab(learning_page, "学习解析")

        self.ai_panel = AiDialog(None, None, self)
        self.detail_tabs.addTab(self.ai_panel, "问 AI")

        gatt_panel = QWidget()
        gatt_layout = QVBoxLayout(gatt_panel)
        gatt_layout.setContentsMargins(0, 8, 0, 0)
        gatt_title = QLabel("GATT 服务与特征")
        gatt_title.setObjectName("gattTitle")
        gatt_layout.addWidget(gatt_title)
        self.gatt_tree = QTreeWidget()
        self.gatt_tree.setHeaderLabels(["GATT Service / Characteristic / Descriptor", "Properties"])
        self.gatt_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.gatt_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.gatt_tree.setMinimumHeight(150)
        self.gatt_tree.setAlternatingRowColors(True)
        self.gatt_tree.setUniformRowHeights(False)
        self.gatt_tree.currentItemChanged.connect(self._gatt_selected)
        gatt_layout.addWidget(self.gatt_tree, 1)
        gatt_controls = QHBoxLayout()
        self.discover_button = QPushButton("发现 GATT 服务")
        self.discover_button.clicked.connect(self.discover_services)
        self.read_button = QPushButton("读取")
        self.read_button.clicked.connect(self.read_selected_characteristic)
        self.write_value = QLineEdit()
        self.write_value.setPlaceholderText("写入 HEX，例如 01 02")
        self.write_button = QPushButton("写入")
        self.write_button.clicked.connect(self.write_selected_characteristic)
        self.notify_button = QPushButton("订阅通知")
        self.notify_button.clicked.connect(self.toggle_selected_notification)
        for control in (
            self.discover_button,
            self.read_button,
            self.write_value,
            self.write_button,
            self.notify_button,
        ):
            gatt_controls.addWidget(control)
        gatt_layout.addLayout(gatt_controls)
        self._set_gatt_controls_enabled(False)

        self.detail_tabs.setMinimumHeight(250)
        self.detail_tabs.addTab(gatt_panel, "GATT")
        layout.addWidget(self.detail_tabs, 1)

        self.connect_button = QPushButton("选择可连接设备")
        self.connect_button.setEnabled(False)
        self.connect_button.clicked.connect(self.connect_selected_device)
        layout.addWidget(self.connect_button)
        return panel

    def _apply_style(self) -> None:
        self.setStyleSheet("""
            QMainWindow, QWidget { background: #edf1f3; color: #17232b; font-family: "Segoe UI"; font-size: 13px; }
            QLabel#title { font-family: "Bahnschrift"; font-size: 25px; font-weight: 700; color: #123a3a; }
            QLabel#subtitle { color: #68777f; }
            QLabel#badge { background: #dcefe9; color: #14634e; border: 1px solid #a9d5c8; padding: 6px 9px; border-radius: 4px; font-weight: 600; }
            QFrame#panel { background: #f9fbfb; border: 1px solid #ced8dc; border-radius: 6px; }
            QLabel#panelTitle { font-family: "Bahnschrift"; font-size: 16px; font-weight: 600; color: #213c43; }
            QLabel#detailTitle { font-size: 17px; font-weight: 650; color: #145c55; padding: 3px 0 6px 0; }
            QLabel#gattTitle { color: #236b64; font-size: 14px; font-weight: 650; padding-top: 8px; }
            QLabel#legend, QLabel#status { color: #66757d; }
            QPushButton { background: #176f65; color: white; border: 0; padding: 8px 13px; border-radius: 4px; font-weight: 600; }
            QPushButton:hover { background: #125b54; }
            QPushButton:disabled { background: #c6ced1; color: #78858a; }
            QComboBox { background: white; border: 1px solid #bcc9cd; padding: 7px; border-radius: 4px; }
            QListWidget, QTreeWidget, QTextEdit, QTabWidget::pane { background: white; border: 1px solid #d4dcdf; }
            QListWidget::item { padding: 10px 8px; border-bottom: 1px solid #e7ecee; }
            QListWidget::item:selected, QTreeWidget::item:selected { background: #d8ece7; color: #123e39; }
            QHeaderView::section { background: #e7edef; border: 0; border-bottom: 1px solid #ccd6da; padding: 7px; font-weight: 600; }
            QTabBar::tab { padding: 8px 11px; background: #e8edef; }
            QTabBar::tab:selected { background: white; color: #176f65; font-weight: 600; }
            QTextEdit#learningView { font-family: "Segoe UI"; font-size: 13px; }
            QTextEdit#overviewKnowledge { font-family: "Segoe UI"; font-size: 13px; }
        """)

    def _set_empty_state(self) -> None:
        placeholder = QTreeWidgetItem(["", "", "点击“开始扫描”查看协议流程"])
        placeholder.setForeground(2, QColor("#7b898f"))
        self.flow_tree.addTopLevelItem(placeholder)

    def _show_import_placeholder(self) -> None:
        QMessageBox.information(self, "导入", "导入会话或抓包功能将在后续版本实现。")

    def _show_ai_placeholder(self) -> None:
        knowledge = (
            self.knowledge.event(self.current_event.packet_type.value)
            if self.current_event is not None
            else None
        )
        self.ai_panel.set_event_context(self.current_event, knowledge)
        self.detail_tabs.setCurrentWidget(self.ai_panel)

    def start_scan(self) -> None:
        self.scan_timer.stop()
        if self.connection_thread is not None and self.connection_thread.isRunning():
            self.status.setText("请先断开当前连接，再开始扫描")
            return
        if self.real_scan_thread is not None and self.real_scan_thread.isRunning():
            return
        self.all_events.clear()
        self.devices.clear()
        self.selected_device_id = None
        self.pending_events.clear()
        self.device_list.clear()
        self.flow_tree.clear()
        self.event_items.clear()
        self._clear_detail()
        self.scan_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.source_mode.setEnabled(False)
        self.scan_mode.setEnabled(False)
        self.connect_button.setEnabled(False)
        self.status.setText("正在扫描 · 等待附近 BLE 广播…")
        if self.source_mode.currentData() == "real":
            self._start_real_scan()
        else:
            self.pending_events = list(demo_scan_events())
            self.scan_timer.start()

    def stop_scan(self) -> None:
        if self.real_scan_thread is not None and self.real_scan_thread.isRunning():
            self.real_scan_thread.request_stop()
            self.stop_button.setEnabled(False)
            self.status.setText("正在停止真实 BLE 扫描…")
            return
        self.scan_timer.stop()
        self.pending_events.clear()
        if not self.all_events or self.all_events[-1].packet_type != PacketType.SCAN_STOPPED:
            self._append_event(BleEvent("manual-stop", 700, PacketType.SCAN_STOPPED, None, "APP -> OS", "用户停止扫描", Evidence.SYSTEM))
        self.scan_button.setEnabled(True)
        self.scan_button.setText("再次扫描")
        self.stop_button.setEnabled(False)
        self.source_mode.setEnabled(True)
        self.scan_mode.setEnabled(True)
        self._update_status("扫描已停止")

    def _emit_next_scan_event(self) -> None:
        if not self.pending_events:
            self.scan_timer.stop()
            self.scan_button.setEnabled(True)
            self.scan_button.setText("再次扫描")
            self.stop_button.setEnabled(False)
            self.source_mode.setEnabled(True)
            self.scan_mode.setEnabled(True)
            self._update_status("演示扫描完成")
            return
        self._append_event(self.pending_events.pop(0))

    def _append_event(self, event: BleEvent) -> None:
        self._append_events([event])

    def _append_events(self, events: list[BleEvent]) -> None:
        if not events:
            return
        self.all_events.extend(events)
        self._refresh_devices()
        last_item: QTreeWidgetItem | None = None
        self.flow_tree.setUpdatesEnabled(False)
        try:
            for event in events:
                if self.selected_device_id is None or event.device_id in {None, self.selected_device_id}:
                    last_item = self._add_event_item(event, scroll=False)
            while self.flow_tree.topLevelItemCount() > MAX_VISIBLE_FLOW_EVENTS:
                removed = self.flow_tree.takeTopLevelItem(0)
                event_id = removed.data(0, Qt.ItemDataRole.UserRole)
                if event_id:
                    self.event_items.pop(event_id, None)
        finally:
            self.flow_tree.setUpdatesEnabled(True)
        if last_item is not None:
            self.flow_tree.scrollToItem(last_item)
        real_scanning = self.real_scan_thread is not None and self.real_scan_thread.isRunning()
        self._update_status("正在扫描" if self.scan_timer.isActive() or real_scanning else "会话已更新")

    def _refresh_devices(self) -> None:
        selected = self.selected_device_id
        aggregated = aggregate_devices(self.all_events)
        self.devices = {device.device_id: device for device in aggregated}
        self.device_list.blockSignals(True)
        self.device_list.clear()
        selected_row = -1
        for row, device in enumerate(aggregated):
            state = {True: "可连接", False: "不可连接", None: "可连接性未知"}[device.connectable]
            item = QListWidgetItem(
                f"{device.name}    {device.rssi} dBm\n{device.address}\n{state} · {device.packet_count} 个广播包"
            )
            item.setData(Qt.ItemDataRole.UserRole, device.device_id)
            self.device_list.addItem(item)
            if device.device_id == selected:
                selected_row = row
        if selected_row >= 0:
            self.device_list.setCurrentRow(selected_row)
        self.device_list.blockSignals(False)

    def _add_event_item(self, event: BleEvent, scroll: bool = True) -> QTreeWidgetItem:
        marker = {
            Evidence.CAPTURED: "●",
            Evidence.API: "■",
            Evidence.INFERRED: "◌",
            Evidence.SYSTEM: "◆",
        }[event.evidence]
        time_text = f"{event.timestamp_ms / 1000:07.3f}s"
        item = QTreeWidgetItem([time_text, f"{marker} {event.packet_type.value}", f"{event.direction}  {event.summary}"])
        item.setData(0, Qt.ItemDataRole.UserRole, event.event_id)
        item.setForeground(1, QColor(EVENT_COLORS[event.evidence]))
        if event.evidence == Evidence.INFERRED:
            font = item.font(1)
            font.setItalic(True)
            item.setFont(1, font)
        self.flow_tree.addTopLevelItem(item)
        self.event_items[event.event_id] = item
        if scroll:
            self.flow_tree.scrollToItem(item)
        return item

    def _device_selected(self, current: QListWidgetItem | None) -> None:
        if current is None:
            return
        self.selected_device_id = current.data(Qt.ItemDataRole.UserRole)
        device = self.devices[self.selected_device_id]
        self._rebuild_flow()
        is_demo = self.source_mode.currentData() == "demo"
        if self.connected_device_id == device.device_id:
            self.connect_button.setEnabled(True)
            self.connect_button.setText(f"断开 {device.name}")
        elif not is_demo:
            self.connect_button.setEnabled(device.connectable is True)
            self.connect_button.setText(
                f"连接 {device.name}" if device.connectable is True else "该广播类型不可连接"
            )
        else:
            self.connect_button.setEnabled(bool(device.connectable))
            self.connect_button.setText(f"连接 {device.name}" if device.connectable else "该设备不可连接")
        self.status.setText(f"已选择 {device.name} · 时间线已过滤并保留扫描上下文")

    def _show_all_events(self) -> None:
        self.selected_device_id = None
        self.device_list.clearSelection()
        self.connect_button.setEnabled(False)
        self.connect_button.setText("选择可连接设备")
        self._rebuild_flow()

    def _rebuild_flow(self) -> None:
        self.flow_tree.clear()
        self.event_items.clear()
        for event in events_for_device(self.all_events, self.selected_device_id):
            self._add_event_item(event)

    def _event_selected(self, current: QTreeWidgetItem | None) -> None:
        if current is None:
            return
        event_id = current.data(0, Qt.ItemDataRole.UserRole)
        event = next((candidate for candidate in self.all_events if candidate.event_id == event_id), None)
        if event is not None:
            self._show_event(event)

    def _show_event(self, event: BleEvent) -> None:
        self.current_event = event
        knowledge = self.knowledge.event(event.packet_type.value)
        self.ai_panel.set_event_context(event, knowledge)
        self.detail_title.setText(f"{event.packet_type.value} · {event.summary}")
        evidence = {
            Evidence.CAPTURED: "实际捕获（演示数据）",
            Evidence.API: "Windows WinRT 报告的广播类型与数据（非完整空口包）",
            Evidence.INFERRED: "协议推断（无原始包）",
            Evidence.SYSTEM: "应用 / 操作系统事件",
        }[event.evidence]
        values = {
            "类型": event.packet_type.value,
            "时间": f"{event.timestamp_ms / 1000:.3f} 秒",
            "方向": event.direction,
            "证据": evidence,
            "关联事件": event.related_event_id or "无",
            "错误": event.fields.get("Error", "无"),
        }
        for key, value in values.items():
            self.overview_labels[key].setText(value)

        knowledge = self.knowledge.event(event.packet_type.value)
        self.overview_knowledge.setHtml(
            self._overview_html(knowledge.description, knowledge.overview, knowledge.flow_image)
        )
        explanation = knowledge.description
        if event.related_event_id:
            explanation += f" 关联事件为 {event.related_event_id}，可在时间线中选择关联包进行比较。"
        if event.evidence == Evidence.CAPTURED:
            explanation += " 当前为演示 Sniffer 数据；接入真实采集器后，此处将显示真实 PDU。"
        self.learning_view.setHtml(
            self._learning_html(
                event,
                explanation,
                knowledge.image,
                knowledge.header_image,
                knowledge.header_title,
                knowledge.header_note,
                knowledge.structure_note,
                knowledge.data_title,
                knowledge.data_note,
            )
        )
        self._rebalance_detail_splitter()

    def _rebalance_detail_splitter(self) -> None:
        return

    def _learning_html(
        self,
        event: BleEvent,
        explanation: str,
        image: str,
        header_image: str,
        header_title: str,
        header_note: str,
        structure_note: str,
        data_title: str,
        data_note: str,
    ) -> str:
        raw_hex = self._format_hex(event.raw_data) if event.raw_data else "此事件没有可用的原始字节。"
        structures = self._ad_structure_html(event)
        structure_section = ""
        data_section_number = 2
        if image or structure_note:
            image_html = ""
            if image:
                image_url = QUrl.fromLocalFile(image).toString()
                image_width = max(320, self.learning_view.viewport().width() - 32)
                image_html = f'<div class="diagram"><img src="{escape(image_url)}" width="{image_width}"></div>'
            header_html = ""
            if header_image:
                header_url = QUrl.fromLocalFile(header_image).toString()
                header_width = max(320, self.learning_view.viewport().width() - 48)
                header_html = (
                    f'<h4>{escape(header_title)}</h4>'
                    f'<div class="diagram"><img src="{escape(header_url)}" width="{header_width}"></div>'
                )
            if header_note:
                header_html += f'<pre class="header-note">{escape(header_note)}</pre>'
            structure_note_html = (
                f'<pre class="source-note">{escape(structure_note)}</pre>' if structure_note else ""
            )
            structure_section = f"<h3>2. 协议结构参考</h3>{image_html}{header_html}{structure_note_html}"
            data_section_number = 3
        data_note_html = f'<p class="data-note">{escape(data_note)}</p>' if data_note else ""
        return f"""
            <style>
                body {{ color: #17232b; font-family: 'Segoe UI'; line-height: 1.55; }}
                h3 {{ color: #145c55; margin: 4px 0 8px 0; font-size: 15px; }}
                h4 {{ color: #236b64; margin: 12px 0 7px 0; font-size: 14px; }}
                pre {{ background: #17272b; color: #e8f3ef; padding: 10px; font-family: 'Cascadia Mono'; white-space: pre-wrap; }}
                .note {{ color: #33464d; }}
                pre.source-note {{ color: #33464d; background: #edf4f2; border-left: 3px solid #6b9990; padding: 9px; }}
                pre.header-note {{ color: #33464d; background: #f4f7f7; border-left: 3px solid #4f8790; padding: 9px; }}
                .data-note {{ color: #52636a; margin: 5px 0 10px 0; }}
                .diagram {{ background: #ffffff; border: 1px solid #ced8dc; padding: 6px; margin-bottom: 14px; }}
                .structure-label {{ color: #145c55; font-weight: 600; margin: 12px 0 5px 0; }}
                pre.structure {{ background: #edf4f2; color: #173f3b; border-left: 3px solid #176f65; margin: 0 0 9px 0; }}
                pre.bit-detail {{ background: #f6f8f8; color: #24383d; border-left: 3px solid #7f9499; margin: 0 0 12px 14px; }}
                .ad-type-note {{ color: #33464d; background: #f6f8f8; border-left: 3px solid #9aadb1; padding: 7px; margin: -4px 0 10px 14px; }}
            </style>
            <h3>1. 学习说明</h3>
            <p class="note">{escape(explanation)}</p>
            {structure_section}
            <h3>{data_section_number}. {escape(data_title)}</h3>
            <pre>{escape(raw_hex)}</pre>
            {data_note_html}
            {structures}
        """

    def _overview_html(self, description: str, overview: str, flow_image: str) -> str:
        detail = overview or description
        paragraphs = "".join(
            f"<p>{escape(paragraph)}</p>" for paragraph in detail.split("\n\n") if paragraph
        )
        flow_html = ""
        if flow_image:
            flow_url = QUrl.fromLocalFile(flow_image).toString()
            flow_width = max(320, self.overview_knowledge.viewport().width() - 28)
            flow_html = (
                '<h3>典型流程</h3>'
                f'<div class="flow-diagram"><img src="{escape(flow_url)}" width="{flow_width}"></div>'
            )
        return f"""
            <style>
                body {{ color: #263a40; font-family: 'Segoe UI'; line-height: 1.55; }}
                h3 {{ color: #145c55; margin: 10px 0 7px 0; font-size: 15px; }}
                p {{ margin: 5px 0 9px 0; }}
                .summary {{ background: #edf4f2; border-left: 3px solid #176f65; padding: 8px; }}
                .flow-diagram {{ background: #ffffff; border: 1px solid #ced8dc; padding: 5px; }}
            </style>
            <h3>知识概况</h3>
            <div class="summary">{paragraphs}</div>
            {flow_html}
        """

    @staticmethod
    def _format_hex(data: bytes) -> str:
        lines = []
        for offset in range(0, len(data), 16):
            chunk = data[offset:offset + 16]
            lines.append(f"{offset:04X}  " + " ".join(f"{value:02X}" for value in chunk))
        return "\n".join(lines)

    def _ad_structure_html(self, event: BleEvent) -> str:
        advertising_types = {
            PacketType.ADVERTISEMENT_UPDATE,
            PacketType.ADV_IND,
            PacketType.ADV_DIRECT_IND,
            PacketType.ADV_NONCONN_IND,
            PacketType.ADV_SCAN_IND,
            PacketType.EXTENDED_ADVERTISEMENT,
            PacketType.SCAN_RESPONSE,
        }
        if event.packet_type not in advertising_types:
            fields = "\n".join(f"{key}: {value}" for key, value in event.fields.items())
            content = fields or "此事件没有结构化字段。"
            if event.raw_data:
                content = f"{event.raw_data.hex(' ').upper()}\n\n{content}"
            return f'<div class="structure-label">API 数据</div><pre class="structure">{escape(content)}</pre>'
        if not event.raw_data:
            fields = "\n".join(f"{key}: {value}" for key, value in event.fields.items())
            content = fields or "此事件由应用、系统或协议推断产生，无 AD Structure 可解析。"
            return f'<div class="structure-label">字段信息</div><pre class="structure">{escape(content)}</pre>'

        blocks: list[str] = []
        offset = 0
        while offset < len(event.raw_data):
            length = event.raw_data[offset]
            end = offset + length + 1
            if length == 0 or end > len(event.raw_data):
                remainder = event.raw_data[offset:]
                content = f"{remainder.hex(' ').upper()}\n└─ 长度字段无效，无法继续解析"
                blocks.append(f'<pre class="structure">{escape(content)}</pre>')
                break
            structure = event.raw_data[offset:end]
            ad_type = structure[1]
            value = structure[2:]
            field_name = self.knowledge.ad_type_name(ad_type)
            parsed_value = self._decode_ad_value(ad_type, value)
            content = (
                f"{structure.hex(' ').upper()}\n"
                f"│  │  └─ {field_name}: {parsed_value}\n"
                f"│  └──── AD Type: 0x{ad_type:02X}\n"
                f"└─────── Length: {length}"
            )
            label = f"AD Structure · 0x{offset:02X}-0x{end - 1:02X}"
            blocks.append(
                f'<div class="structure-label">{escape(label)}</div>'
                f'<pre class="structure">{escape(content)}</pre>'
                f'<div class="ad-type-note"><b>AD Type 0x{ad_type:02X}：</b>'
                f'{escape(self.knowledge.ad_type_description(ad_type))}</div>'
            )
            if ad_type == 0x01:
                blocks.append(self._flags_detail_html(value))
            offset = end
        return "".join(blocks)

    @staticmethod
    def _flags_detail_html(value: bytes) -> str:
        decoded = decode_flags(value)
        if len(decoded) == 1:
            content = decoded[0]
        else:
            content = "\n".join(decoded)
        return (
            '<div class="structure-label">Flags 位解析</div>'
            f'<pre class="bit-detail">{escape(content)}</pre>'
        )

    @staticmethod
    def _decode_ad_value(ad_type: int, value: bytes) -> str:
        if ad_type == 0x01 and value:
            return f"0x{value[0]:02X}"
        if ad_type in {0x08, 0x09}:
            return value.decode("utf-8", errors="replace")
        if ad_type == 0x03 and len(value) % 2 == 0:
            return ", ".join(f"0x{int.from_bytes(value[index:index + 2], 'little'):04X}" for index in range(0, len(value), 2))
        return value.hex(" ").upper()

    def _clear_detail(self) -> None:
        self.current_event = None
        self.ai_panel.set_event_context(None, None)
        self.detail_title.setText("选择一个流程事件")
        for label in self.overview_labels.values():
            label.setText("-")
        self.overview_knowledge.clear()
        self.learning_view.clear()
        self.gatt_tree.clear()
        self._set_gatt_controls_enabled(False)

    def connect_selected_device(self) -> None:
        if self.selected_device_id is None:
            return
        device = self.devices[self.selected_device_id]
        if self.source_mode.currentData() == "real":
            if self.connection_thread is not None and self.connection_thread.isRunning():
                if self.connected_device_id == device.device_id:
                    self.connect_button.setEnabled(False)
                    self.connect_button.setText("正在断开…")
                    self.connection_thread.request_disconnect()
                return
            self._append_event(
                BleEvent(
                    f"{device.device_id}-selected-for-connection",
                    self._next_timestamp_ms(),
                    PacketType.DEVICE_SELECTED,
                    device.device_id,
                    "User -> App",
                    f"选择连接 {device.name}",
                    Evidence.SYSTEM,
                    fields={"Address": device.address, "RSSI": f"{device.rssi} dBm"},
                )
            )
            if self.real_scan_thread is not None and self.real_scan_thread.isRunning():
                self.pending_connection_device_id = device.device_id
                self.connect_button.setEnabled(False)
                self.connect_button.setText("正在停止扫描…")
                self.real_scan_thread.request_stop()
                return
            self._start_connection(device)
            return
        self.scan_timer.stop()
        self.pending_events.clear()
        for event in demo_connection_events(device.device_id, device.name, device.connectable):
            self._append_event(event)
        self.scan_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.connect_button.setEnabled(False)
        self.connect_button.setText("已连接" if device.connectable else "连接失败")
        self._update_status("连接阶段完成")
        result = self.all_events[-1]
        item = self.event_items.get(result.event_id)
        if item is not None:
            self.flow_tree.setCurrentItem(item)

    def _update_status(self, prefix: str) -> None:
        packet_count = sum(
            event.packet_type in {
                PacketType.ADVERTISEMENT_UPDATE,
                PacketType.ADV_IND,
                PacketType.ADV_DIRECT_IND,
                PacketType.ADV_NONCONN_IND,
                PacketType.ADV_SCAN_IND,
                PacketType.EXTENDED_ADVERTISEMENT,
                PacketType.SCAN_RESPONSE,
            }
            for event in self.all_events
        )
        connectable_count = sum(device.connectable is True for device in self.devices.values())
        unknown_count = sum(device.connectable is None for device in self.devices.values())
        self.status.setText(
            f"{prefix} · {len(self.devices)} 个设备 · {packet_count} 个广播事件 · "
            f"{connectable_count} 个可连接 · {unknown_count} 个可连接性未知"
        )

    def _source_changed(self) -> None:
        is_real = self.source_mode.currentData() == "real"
        self.source_badge.setText("Windows WinRT" if is_real else "演示数据")
        self.connect_button.setText("选择可连接设备" if is_real else "选择可连接设备")

    def _start_real_scan(self) -> None:
        self.real_scan_error = ""
        scan_mode = "active" if self.scan_mode.currentIndex() == 0 else "passive"
        self._append_event(
            BleEvent(
                "real-scan-started", 0, PacketType.SCAN_STARTED, None,
                "APP -> WinRT", f"开始{self.scan_mode.currentText()}", Evidence.SYSTEM,
                fields={"Source": "Windows WinRT Advertisement Watcher", "Mode": scan_mode},
            )
        )
        thread = WinRTScanThread(scan_mode, self)
        thread.advertisements_received.connect(self._append_events)
        thread.scan_failed.connect(self._real_scan_failed)
        thread.scan_stopped.connect(self._real_scan_stopped)
        thread.finished.connect(thread.deleteLater)
        self.real_scan_thread = thread
        thread.start()

    def _real_scan_failed(self, message: str) -> None:
        self.real_scan_error = message
        self.status.setText(f"真实 BLE 扫描失败：{message}")

    def _real_scan_stopped(self) -> None:
        timestamp_ms = self.all_events[-1].timestamp_ms + 1 if self.all_events else 0
        summary = (
            f"真实扫描异常结束：{self.real_scan_error}"
            if self.real_scan_error
            else "真实扫描已停止"
        )
        fields = {"Error": self.real_scan_error} if self.real_scan_error else {}
        self._append_event(
            BleEvent(
                "real-scan-stopped", timestamp_ms, PacketType.SCAN_STOPPED, None,
                "OS API -> APP", summary, Evidence.SYSTEM, fields=fields,
            )
        )
        self.scan_button.setEnabled(True)
        self.scan_button.setText("再次扫描")
        self.stop_button.setEnabled(False)
        self.source_mode.setEnabled(True)
        self.scan_mode.setEnabled(True)
        self._update_status(summary)
        self.real_scan_thread = None
        if self.pending_connection_device_id is not None:
            device_id = self.pending_connection_device_id
            self.pending_connection_device_id = None
            device = self.devices.get(device_id)
            if device is not None:
                self._start_connection(device)

    def _start_connection(self, device: DiscoveredDevice) -> None:
        self.scan_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.source_mode.setEnabled(False)
        self.scan_mode.setEnabled(False)
        self.connect_button.setEnabled(False)
        self.connect_button.setText("正在连接…")
        initiating_hint = next(
            (
                event.packet_type.value
                for event in reversed(self.all_events)
                if event.device_id == device.device_id
                and event.fields.get("Connectable") == "Yes"
                and event.packet_type in {
                    PacketType.ADV_IND,
                    PacketType.ADV_DIRECT_IND,
                    PacketType.EXTENDED_ADVERTISEMENT,
                }
            ),
            "Unknown",
        )
        thread = ConnectionThread(
            device.address,
            device.name,
            self._next_timestamp_ms(),
            initiating_hint,
            self,
        )
        thread.events_received.connect(self._append_events)
        thread.services_received.connect(self._populate_gatt)
        thread.connected.connect(lambda: self._connection_ready(device.device_id))
        thread.session_finished.connect(self._connection_session_finished)
        thread.finished.connect(self._connection_thread_finished)
        thread.finished.connect(thread.deleteLater)
        self.connection_thread = thread
        thread.start()

    def _connection_ready(self, device_id: str) -> None:
        self.connected_device_id = device_id
        device = self.devices.get(device_id)
        self.connect_button.setEnabled(True)
        self.connect_button.setText(f"断开 {device.name}" if device else "断开连接")
        self.discover_button.setEnabled(True)
        self.status.setText("连接已建立 · 正在观察连接参数和 PHY")

    def _connection_session_finished(self, connected_once: bool) -> None:
        if not connected_once:
            self.status.setText("连接失败 · 可重新扫描或重试连接")

    def _connection_thread_finished(self) -> None:
        self.connection_thread = None
        self.connected_device_id = None
        self.scan_button.setEnabled(True)
        self.source_mode.setEnabled(True)
        self.scan_mode.setEnabled(True)
        if self.selected_device_id and self.selected_device_id in self.devices:
            device = self.devices[self.selected_device_id]
            self.connect_button.setEnabled(device.connectable is True)
            self.connect_button.setText(
                f"连接 {device.name}" if device.connectable is True else "该广播类型不可连接"
            )
        else:
            self.connect_button.setEnabled(False)
            self.connect_button.setText("选择可连接设备")

        self.gatt_tree.clear()
        self._set_gatt_controls_enabled(False)

    def discover_services(self) -> None:
        if self.connection_thread is not None and self.connection_thread.isRunning():
            self.discover_button.setEnabled(False)
            self.status.setText("正在发现 GATT 服务…")
            self.connection_thread.request_discover_services()

    def _populate_gatt(self, characteristics: object) -> None:
        self.gatt_tree.clear()
        services: dict[str, QTreeWidgetItem] = {}
        for characteristic in characteristics:
            service_item = services.get(characteristic.service_uuid)
            if service_item is None:
                service_item = QTreeWidgetItem([f"Service {characteristic.service_uuid}", ""])
                service_item.setData(0, Qt.ItemDataRole.UserRole, None)
                self.gatt_tree.addTopLevelItem(service_item)
                services[characteristic.service_uuid] = service_item
            item = QTreeWidgetItem([
                f"Characteristic {characteristic.uuid} (handle 0x{characteristic.handle:04X})",
                characteristic.properties,
            ])
            item.setData(0, Qt.ItemDataRole.UserRole, characteristic.uuid)
            item.setData(1, Qt.ItemDataRole.UserRole, characteristic.properties)
            service_item.addChild(item)
            for descriptor_uuid in characteristic.descriptors:
                descriptor = QTreeWidgetItem([f"Descriptor {descriptor_uuid}", ""])
                descriptor.setData(0, Qt.ItemDataRole.UserRole, None)
                item.addChild(descriptor)
            service_item.setExpanded(True)
            item.setExpanded(True)

    def _gatt_selected(self, current: QTreeWidgetItem | None) -> None:
        self._set_gatt_controls_enabled(current is not None and self.connection_thread is not None)

    def _selected_characteristic_uuid(self) -> str | None:
        item = self.gatt_tree.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None

    def _set_gatt_controls_enabled(self, enabled: bool) -> None:
        self.discover_button.setEnabled(self.connection_thread is not None and self.connection_thread.isRunning())
        for control in (self.read_button, self.write_value, self.write_button, self.notify_button):
            control.setEnabled(enabled)

    def read_selected_characteristic(self) -> None:
        uuid = self._selected_characteristic_uuid()
        if uuid and self.connection_thread is not None:
            self.connection_thread.request_read(uuid)

    def write_selected_characteristic(self) -> None:
        uuid = self._selected_characteristic_uuid()
        if not uuid or self.connection_thread is None:
            return
        try:
            data = bytes.fromhex(self.write_value.text())
        except ValueError:
            self.status.setText("HEX 格式错误，请使用空格分隔的两位十六进制字节")
            return
        self.connection_thread.request_write(uuid, data, None)

    def toggle_selected_notification(self) -> None:
        uuid = self._selected_characteristic_uuid()
        if uuid and self.connection_thread is not None:
            enabled = self.notify_button.text() == "订阅通知"
            self.connection_thread.request_notify(uuid, enabled)
            self.notify_button.setText("取消订阅" if enabled else "订阅通知")

    def _next_timestamp_ms(self) -> int:
        return self.all_events[-1].timestamp_ms + 1 if self.all_events else 0

    def closeEvent(self, event: QCloseEvent) -> None:
        self.ai_panel.shutdown()
        if self.real_scan_thread is not None and self.real_scan_thread.isRunning():
            self.real_scan_thread.request_stop()
            self.real_scan_thread.wait(3000)
        if self.connection_thread is not None and self.connection_thread.isRunning():
            self.connection_thread.request_disconnect()
            self.connection_thread.wait(5000)
        event.accept()


def main() -> int:
    application = QApplication(sys.argv)
    application.setApplicationName("BLE Flow Analyzer")
    window = MainWindow()
    window.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())