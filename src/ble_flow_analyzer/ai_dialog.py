from __future__ import annotations

from html import escape

from PySide6.QtCore import QSettings, QThread, Signal, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .ai import AiClient, OllamaClient, OllamaError, build_event_context, choose_default_model, environment_key, PROVIDERS
from .domain import BleEvent
from .knowledge import KnowledgeEntry


class ModelLoader(QThread):
    loaded = Signal(object)
    failed = Signal(str)

    def run(self) -> None:
        try:
            self.loaded.emit(OllamaClient(timeout_seconds=5).list_models())
        except OllamaError as error:
            self.failed.emit(str(error))


class ChatWorker(QThread):
    chunk_received = Signal(str)
    failed = Signal(str)

    def __init__(self, provider: str, model: str, api_key: str, question: str, context: str) -> None:
        super().__init__()
        self.provider = provider
        self.model = model
        self.api_key = api_key
        self.question = question
        self.context = context

    def run(self) -> None:
        try:
            for chunk in AiClient(self.provider, self.api_key).stream_chat(self.model, self.question, self.context):
                if self.isInterruptionRequested():
                    return
                self.chunk_received.emit(chunk)
        except OllamaError as error:
            self.failed.emit(str(error))


class AiDialog(QWidget):
    def __init__(
        self,
        event: BleEvent | None,
        knowledge: KnowledgeEntry | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.ble_event = event
        self.knowledge = knowledge
        self.model_loader: ModelLoader | None = None
        self.chat_worker: ChatWorker | None = None
        self._answer_started = False
        self._request_generation = 0
        self.settings = QSettings("BLE Flow Analyzer", "BLE Flow Analyzer")

        self.setWindowTitle("问 AI · AI 模型")
        self.resize(720, 680)
        self.setMinimumSize(560, 520)
        self._build_ui()
        self._automatic_context = self._build_automatic_context()
        self._session_context = self._automatic_context
        self._provider_changed()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        model_row = QHBoxLayout()
        model_row.addWidget(QLabel("提供商"))
        self.provider_combo = QComboBox()
        for provider, specification in PROVIDERS.items():
            self.provider_combo.addItem(str(specification["label"]), provider)
        self.provider_combo.currentIndexChanged.connect(self._provider_changed)
        model_row.addWidget(self.provider_combo)
        model_row.addWidget(QLabel("模型"))
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.setMinimumWidth(250)
        self.model_combo.currentTextChanged.connect(self._update_send_enabled)
        model_row.addWidget(self.model_combo, 1)
        self.refresh_button = QPushButton("刷新本地模型")
        self.refresh_button.clicked.connect(self._load_ollama_models)
        model_row.addWidget(self.refresh_button)
        layout.addLayout(model_row)

        self.advanced_toggle = QToolButton()
        self.advanced_toggle.setText("高级模型设置")
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setChecked(False)
        self.advanced_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.advanced_toggle.toggled.connect(self._advanced_settings_toggled)
        self.context_button = QPushButton("上下文详情")
        self.context_button.clicked.connect(self._show_context)
        options_row = QHBoxLayout()
        options_row.addWidget(self.advanced_toggle)
        options_row.addWidget(self.context_button)
        options_row.addStretch()
        layout.addLayout(options_row)

        self.advanced_panel = QWidget()
        advanced_layout = QVBoxLayout(self.advanced_panel)
        advanced_layout.setContentsMargins(8, 0, 0, 0)

        key_row = QHBoxLayout()
        self.key_label = QLabel("API Key")
        key_row.addWidget(self.key_label)
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("环境变量优先；此处输入仅保留在当前对话框")
        self.key_edit.textChanged.connect(self._update_send_enabled)
        key_row.addWidget(self.key_edit, 1)
        advanced_layout.addLayout(key_row)

        self.provider_note = QLabel()
        self.provider_note.setObjectName("aiProviderNote")
        self.cloud_confirm = QCheckBox("确认将当前上下文发送至云端")
        self.cloud_confirm.toggled.connect(self._update_send_enabled)
        self.cloud_confirm.toggled.connect(self._remember_cloud_confirmation)
        self.cloud_confirmed_label = QLabel("已确认发送至云端")
        self.cloud_confirmed_label.setObjectName("cloudConfirmed")
        self.revoke_cloud_button = QPushButton("撤销确认")
        self.revoke_cloud_button.setProperty("secondary", True)
        self.revoke_cloud_button.clicked.connect(self._revoke_cloud_confirmation)
        cloud_row = QHBoxLayout()
        cloud_row.addWidget(self.provider_note, 1)
        cloud_row.addWidget(self.cloud_confirm)
        cloud_row.addWidget(self.cloud_confirmed_label)
        cloud_row.addWidget(self.revoke_cloud_button)
        advanced_layout.addLayout(cloud_row)
        layout.addWidget(self.advanced_panel)

        self.conversation = QTextEdit()
        self.conversation.setReadOnly(True)
        self.conversation.setPlaceholderText("选择模型并提出关于当前 BLE 事件的问题。")
        layout.addWidget(self.conversation, 1)

        self.question_edit = QTextEdit()
        self.question_edit.setPlaceholderText("请输入关于当前事件的问题……")
        self.question_edit.setFixedHeight(86)
        self.question_edit.textChanged.connect(self._update_send_enabled)
        layout.addWidget(self.question_edit)

        action_row = QHBoxLayout()
        self.status_label = QLabel("正在连接本地 Ollama……")
        self.status_label.setObjectName("aiStatus")
        action_row.addWidget(self.status_label, 1)
        self.clear_button = QPushButton("清空")
        self.clear_button.clicked.connect(self.conversation.clear)
        action_row.addWidget(self.clear_button)
        self.stop_button = QPushButton("停止生成")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop_chat)
        action_row.addWidget(self.stop_button)
        self.send_button = QPushButton("发送")
        self.send_button.setEnabled(False)
        self.send_button.clicked.connect(self._send)
        action_row.addWidget(self.send_button)
        layout.addLayout(action_row)

        warning = QLabel("AI 回答可能不准确，请结合实际数据和协议规范判断。")
        warning.setObjectName("aiWarning")
        layout.addWidget(warning)

        self.setStyleSheet("""
            QLabel#aiProviderNote, QLabel#aiStatus { color: #52636a; }
            QLabel#cloudConfirmed { color: #176f65; font-weight: 600; }
            QLabel#aiWarning { color: #8a5a08; background: #fff5dc; padding: 7px; border: 1px solid #e6c878; }
            QPushButton[secondary="true"] { background: #e1ece9; color: #174c47; border: 1px solid #abc7c1; }
        """)

    def _provider_changed(self) -> None:
        provider = self.provider_combo.currentData()
        self._request_generation += 1
        if self.chat_worker is not None and self.chat_worker.isRunning():
            self.chat_worker.requestInterruption()
        if provider != "ollama" and self.model_loader is not None and self.model_loader.isRunning():
            self.model_loader.requestInterruption()
        self.model_combo.clear()
        is_ollama = provider == "ollama"
        self.refresh_button.setVisible(is_ollama)
        self.key_label.setVisible(not is_ollama)
        self.key_edit.setVisible(not is_ollama)
        self.cloud_confirm.setVisible(not is_ollama)
        self._load_cloud_confirmation(provider)
        self.advanced_panel.setVisible(not is_ollama and self.advanced_toggle.isChecked())
        if is_ollama:
            self.provider_note.setText("本地请求仅发送到 http://localhost:11434")
            self._load_ollama_models()
            return
        specification = PROVIDERS[provider]
        for model in specification["models"]:
            label = str(model)
            if provider == "deepseek":
                label = {
                    "deepseek-v4-flash": "deepseek-v4-flash（推荐 · 快速/思考）",
                    "deepseek-chat": "deepseek-chat（快速问答）",
                    "deepseek-reasoner": "deepseek-reasoner（深度推理）",
                }.get(str(model), str(model))
            elif provider == "qwen":
                label = {
                    "qwen3.8-flash": "qwen3.8-flash（推荐 · 快速）",
                    "qwen3.7-plus": "qwen3.7-plus（综合能力）",
                    "qwen-plus": "qwen-plus（兼容）",
                    "qwen-turbo": "qwen-turbo（快速问答）",
                }.get(str(model), str(model))
            self.model_combo.addItem(label, str(model))
        key = environment_key(provider)
        self.key_edit.setText(key)
        key_status = "已从环境变量读取" if key else "未设置"
        self.provider_note.setText(
            f"云端请求发送至 {specification['url']}；API Key 环境变量："
            f"{specification['env']}（{key_status}）"
        )
        self._update_send_enabled()

    def _confirmation_key(self, provider: str | None) -> str:
        return f"ai/cloud_confirmation/{provider or 'unknown'}"

    def _load_cloud_confirmation(self, provider: str | None) -> None:
        confirmed = bool(provider and self.settings.value(self._confirmation_key(provider), False, type=bool))
        self.cloud_confirm.blockSignals(True)
        self.cloud_confirm.setChecked(confirmed)
        self.cloud_confirm.blockSignals(False)
        self.cloud_confirm.setVisible(provider != "ollama" and not confirmed)
        self.cloud_confirmed_label.setVisible(provider != "ollama" and confirmed)
        self.revoke_cloud_button.setVisible(provider != "ollama" and confirmed)

    def _remember_cloud_confirmation(self, confirmed: bool) -> None:
        provider = self.provider_combo.currentData()
        if provider == "ollama":
            return
        self.settings.setValue(self._confirmation_key(provider), confirmed)
        self.cloud_confirm.setVisible(not confirmed)
        self.cloud_confirmed_label.setVisible(confirmed)
        self.revoke_cloud_button.setVisible(confirmed)

    def _revoke_cloud_confirmation(self) -> None:
        provider = self.provider_combo.currentData()
        self.settings.setValue(self._confirmation_key(provider), False)
        self._load_cloud_confirmation(provider)
        self._update_send_enabled()

    def _advanced_settings_toggled(self, expanded: bool) -> None:
        provider = self.provider_combo.currentData()
        self.advanced_toggle.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )
        self.advanced_panel.setVisible(provider != "ollama" and expanded)

    def _load_ollama_models(self) -> None:
        if self.model_loader is not None and self.model_loader.isRunning():
            return
        self.model_combo.clear()
        self.model_combo.addItem("正在读取本地模型……", None)
        self.send_button.setEnabled(False)
        self.refresh_button.setEnabled(False)
        self.status_label.setText("正在连接本地 Ollama……")
        self.model_loader = ModelLoader(self)
        self.model_loader.loaded.connect(self._models_loaded)
        self.model_loader.failed.connect(lambda message: self.status_label.setText(message))
        self.model_loader.finished.connect(lambda: self.refresh_button.setEnabled(True))
        self.model_loader.start()

    def _update_send_enabled(self) -> None:
        if not hasattr(self, "send_button"):
            return
        provider = self.provider_combo.currentData()
        model_ready = bool(self.model_combo.currentText().strip())
        question_ready = bool(self.question_edit.toPlainText().strip())
        cloud_ready = provider == "ollama" or (
            bool(self.key_edit.text().strip()) and self.cloud_confirm.isChecked()
        )
        self.send_button.setEnabled(model_ready and question_ready and cloud_ready)

    def _models_loaded(self, models: list[str]) -> None:
        if self.provider_combo.currentData() != "ollama":
            return
        self.model_combo.clear()
        for model in models:
            label = f"Ollama · {model}"
            if model == "qwen2.5:1.5b":
                label += "（推荐）"
            elif model == "qwen2.5:0.5b":
                label += "（快速）"
            self.model_combo.addItem(label, model)
        default_model = choose_default_model(models)
        if default_model:
            self.model_combo.setCurrentIndex(self.model_combo.findData(default_model))
            self.status_label.setText(f"已连接 Ollama · {len(models)} 个本地模型")
        else:
            self.status_label.setText("Ollama 中没有已安装的模型")
        self._update_send_enabled()

    def _context(self) -> str:
        return self._session_context

    def set_event_context(self, event: BleEvent | None, knowledge: KnowledgeEntry | None) -> None:
        if self.chat_worker is not None and self.chat_worker.isRunning():
            self.chat_worker.requestInterruption()
        self._request_generation += 1
        self.ble_event = event
        self.knowledge = knowledge
        self._automatic_context = self._build_automatic_context()
        self._session_context = self._automatic_context
        self.context_button.setText("上下文详情")
        self.conversation.clear()
        self.status_label.setText("当前事件上下文已更新")

    def _build_automatic_context(self) -> str:
        return build_event_context(
            self.ble_event,
            self.knowledge,
            include_event=True,
            include_raw_data=True,
            include_knowledge=True,
        )

    def _show_context(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("编辑本次发送给 AI 的上下文")
        dialog.resize(680, 520)
        layout = QVBoxLayout(dialog)
        note = QLabel("修改仅对当前问 AI 对话框有效，关闭后不会保存。")
        layout.addWidget(note)
        content = QTextEdit()
        content.setPlainText(self._context())
        layout.addWidget(content)
        action_row = QHBoxLayout()
        restore_button = QPushButton("恢复自动上下文")
        action_row.addWidget(restore_button)
        clear_button = QPushButton("清空")
        clear_button.clicked.connect(content.clear)
        action_row.addWidget(clear_button)
        action_row.addStretch()
        cancel_button = QPushButton("取消")
        cancel_button.clicked.connect(dialog.reject)
        action_row.addWidget(cancel_button)
        apply_button = QPushButton("应用本次修改")
        action_row.addWidget(apply_button)
        layout.addLayout(action_row)

        automatic_context = {"value": self._automatic_context}

        def restore_context() -> None:
            automatic_context["value"] = self._build_automatic_context()
            content.setPlainText(automatic_context["value"])

        def apply_context() -> None:
            self._automatic_context = automatic_context["value"]
            self._session_context = content.toPlainText()
            modified = self._session_context != self._automatic_context
            self.context_button.setText("上下文详情（已修改）" if modified else "上下文详情")
            dialog.accept()

        restore_button.clicked.connect(restore_context)
        apply_button.clicked.connect(apply_context)
        dialog.exec()

    def _send(self) -> None:
        question = self.question_edit.toPlainText().strip()
        model = self._selected_model()
        provider = self.provider_combo.currentData()
        if not question:
            self.status_label.setText("请先输入问题")
            return
        if not model:
            self.status_label.setText("请选择模型")
            return
        if provider != "ollama" and not self.cloud_confirm.isChecked():
            self.status_label.setText("请先确认将当前上下文发送至云端服务")
            return
        self.conversation.append(f"<p><b>你：</b>{escape(question)}</p><p><b>AI：</b></p>")
        self.question_edit.clear()
        self._answer_started = True
        self.send_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.status_label.setText(f"{model} 正在生成……")
        self._request_generation += 1
        request_generation = self._request_generation
        self.chat_worker = ChatWorker(str(provider), model, self.key_edit.text(), question, self._context())
        self.chat_worker.chunk_received.connect(
            lambda chunk, generation=request_generation: self._append_chunk_if_current(generation, chunk)
        )
        self.chat_worker.failed.connect(
            lambda message, generation=request_generation, name=provider: self._chat_failed_if_current(
                generation, str(name), message
            )
        )
        self.chat_worker.finished.connect(
            lambda generation=request_generation: self._chat_finished_if_current(generation)
        )
        self.chat_worker.start()

    def _selected_model(self) -> str:
        model_data = self.model_combo.currentData()
        return str(model_data).strip() if model_data else self.model_combo.currentText().strip()

    def _append_chunk_if_current(self, generation: int, chunk: str) -> None:
        if generation == self._request_generation:
            self._append_chunk(chunk)

    def _append_chunk(self, chunk: str) -> None:
        cursor = self.conversation.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(chunk)
        self.conversation.setTextCursor(cursor)
        self.conversation.ensureCursorVisible()

    def _stop_chat(self) -> None:
        if self.chat_worker is not None and self.chat_worker.isRunning():
            self.chat_worker.requestInterruption()
            self.status_label.setText("正在停止生成……")

    def _chat_failed(self, message: str) -> None:
        self.conversation.append(f"<p><b>请求失败：</b>{escape(message)}</p>")

    def _chat_failed_if_current(self, generation: int, provider: str, message: str) -> None:
        if generation != self._request_generation:
            return
        label = str(PROVIDERS.get(provider, {}).get("label", provider))
        self._chat_failed(f"{label}：{message}")
        self.status_label.setText(f"{label} 请求失败")

    def _chat_finished(self) -> None:
        if self._answer_started:
            self.conversation.append("<br>")
        self._answer_started = False
        self._update_send_enabled()
        self.stop_button.setEnabled(False)
        self.status_label.setText("就绪")

    def _chat_finished_if_current(self, generation: int) -> None:
        if generation == self._request_generation:
            self._chat_finished()

    def shutdown(self) -> None:
        if self.chat_worker is not None and self.chat_worker.isRunning():
            self.chat_worker.requestInterruption()
            self.chat_worker.wait(2000)
        if self.model_loader is not None and self.model_loader.isRunning():
            self.model_loader.wait(5500)

    def closeEvent(self, event: object) -> None:
        self.shutdown()
        super().closeEvent(event)