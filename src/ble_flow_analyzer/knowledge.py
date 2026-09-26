from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

import yaml


@dataclass(frozen=True, slots=True)
class KnowledgeEntry:
    description: str
    overview: str = ""
    flow_image: str = ""
    image: str = ""
    header_image: str = ""
    header_title: str = "协议字段展开"
    header_note: str = ""
    structure_note: str = ""
    data_title: str = "HEX 数据"
    data_note: str = ""


class KnowledgeBase:
    def __init__(
        self,
        events: dict[str, KnowledgeEntry],
        ad_types: dict[int, str],
        ad_type_descriptions: dict[int, str],
    ) -> None:
        self._events = events
        self._ad_types = ad_types
        self._ad_type_descriptions = ad_type_descriptions

    @classmethod
    def load(cls, path: Path | None = None) -> KnowledgeBase:
        source = path or Path(str(files("ble_flow_analyzer").joinpath("knowledge", "ble.yaml")))
        with source.open("r", encoding="utf-8") as stream:
            data = yaml.safe_load(stream) or {}

        events = {
            str(name): KnowledgeEntry(
                description=str(value.get("description", "暂无说明。")),
                overview=str(value.get("overview", "")).strip(),
                flow_image=str((source.parent / str(value["flow_image"])).resolve()) if value.get("flow_image") else "",
                image=str((source.parent / str(value["image"])).resolve()) if value.get("image") else "",
                header_image=str((source.parent / str(value["header_image"])).resolve()) if value.get("header_image") else "",
                header_title=str(value.get("header_title", "协议字段展开")),
                header_note=str(value.get("header_note", "")).strip(),
                structure_note=str(value.get("structure_note", "")).strip(),
                data_title=str(value.get("data_title", "HEX 数据")),
                data_note=str(value.get("data_note", "")).strip(),
            )
            for name, value in data.get("events", {}).items()
        }
        ad_types = {
            int(str(code), 0): str(name)
            for code, name in data.get("ad_types", {}).items()
        }
        ad_type_descriptions = {
            int(str(code), 0): str(description)
            for code, description in data.get("ad_type_descriptions", {}).items()
        }
        return cls(events, ad_types, ad_type_descriptions)

    def event(self, packet_type: str) -> KnowledgeEntry:
        return self._events.get(packet_type, KnowledgeEntry("知识库中暂无该事件的说明。"))

    def ad_type_name(self, ad_type: int) -> str:
        return self._ad_types.get(ad_type, "Unknown AD Type")

    def ad_type_description(self, ad_type: int) -> str:
        return self._ad_type_descriptions.get(ad_type, "知识库中暂无该 AD Type 的用途说明。")