from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .skills import SKILL_CATALOG, SKILL_CATEGORY_ORDER


RESERVED_CATEGORY_NAMES = {"全部", "基础能力"}
MAX_CUSTOM_CATEGORIES = 20
MAX_CATEGORY_NAME_LENGTH = 20


def normalize_category_name(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:MAX_CATEGORY_NAME_LENGTH]


@dataclass
class SkillCategoryState:
    custom_categories: list[str]
    overrides: dict[str, str]

    @classmethod
    def from_ui_config(cls, ui: Any) -> SkillCategoryState:
        values = ui if isinstance(ui, dict) else {}
        built_in = set(SKILL_CATEGORY_ORDER)
        custom: list[str] = []
        raw_custom = values.get("custom_skill_categories", [])
        if isinstance(raw_custom, list):
            for value in raw_custom:
                name = normalize_category_name(value)
                if (
                    name
                    and name not in RESERVED_CATEGORY_NAMES
                    and name not in built_in
                    and name not in custom
                    and len(custom) < MAX_CUSTOM_CATEGORIES
                ):
                    custom.append(name)

        definitions = {skill.id: skill for skill in SKILL_CATALOG}
        valid_categories = built_in | set(custom)
        overrides: dict[str, str] = {}
        raw_overrides = values.get("skill_category_overrides", {})
        if isinstance(raw_overrides, dict):
            for skill_id, value in raw_overrides.items():
                definition = definitions.get(str(skill_id))
                if definition is None:
                    continue
                category = (
                    "电脑与浏览器"
                    if value == "基础能力"
                    else normalize_category_name(value)
                )
                if category in valid_categories and category != definition.category:
                    overrides[definition.id] = category
        return cls(custom, overrides)

    @property
    def categories(self) -> list[str]:
        return [*SKILL_CATEGORY_ORDER, *self.custom_categories]

    def category_for(self, skill_id: str, default: str) -> str:
        return self.overrides.get(skill_id, default)

    def add(self, value: str) -> tuple[bool, str]:
        name = normalize_category_name(value)
        if not name:
            return False, "请输入分类名称"
        if name in RESERVED_CATEGORY_NAMES or name in SKILL_CATEGORY_ORDER:
            return False, "这个名称由系统分类使用"
        if name in self.custom_categories:
            return False, "这个分类已经存在"
        if len(self.custom_categories) >= MAX_CUSTOM_CATEGORIES:
            return False, f"最多创建 {MAX_CUSTOM_CATEGORIES} 个自定义分类"
        self.custom_categories.append(name)
        return True, f"已添加“{name}”"

    def rename(self, old_value: str, new_value: str) -> tuple[bool, str]:
        old_name = normalize_category_name(old_value)
        new_name = normalize_category_name(new_value)
        if old_name not in self.custom_categories:
            return False, "找不到要重命名的分类"
        if not new_name:
            return False, "请输入新的分类名称"
        if new_name in RESERVED_CATEGORY_NAMES or new_name in SKILL_CATEGORY_ORDER:
            return False, "这个名称由系统分类使用"
        if new_name in self.custom_categories and new_name != old_name:
            return False, "这个分类已经存在"
        if new_name == old_name:
            return False, "分类名称没有变化"
        self.custom_categories[self.custom_categories.index(old_name)] = new_name
        self.overrides = {
            skill_id: new_name if category == old_name else category
            for skill_id, category in self.overrides.items()
        }
        return True, f"已重命名为“{new_name}”"

    def remove(self, value: str) -> tuple[bool, str]:
        name = normalize_category_name(value)
        if name not in self.custom_categories:
            return False, "找不到要删除的分类"
        self.custom_categories.remove(name)
        self.overrides = {
            skill_id: category
            for skill_id, category in self.overrides.items()
            if category != name
        }
        return True, f"已删除“{name}”，其中技能已恢复内置分类"

    def move(self, value: str, offset: int) -> tuple[bool, str]:
        name = normalize_category_name(value)
        if name not in self.custom_categories or offset not in {-1, 1}:
            return False, "无法调整这个分类"
        old_index = self.custom_categories.index(name)
        new_index = old_index + offset
        if not 0 <= new_index < len(self.custom_categories):
            return False, "这个分类已经在最前或最后"
        self.custom_categories[old_index], self.custom_categories[new_index] = (
            self.custom_categories[new_index],
            self.custom_categories[old_index],
        )
        return True, f"已调整“{name}”的顺序"

    def assign(self, skill_id: str, category: str) -> tuple[bool, str]:
        definition = next((skill for skill in SKILL_CATALOG if skill.id == skill_id), None)
        if definition is None or category not in self.categories:
            return False, "无法保存这个技能分类"
        if category == definition.category:
            self.overrides.pop(skill_id, None)
        else:
            self.overrides[skill_id] = category
        return True, f"已将“{definition.name}”归入“{category}”"

    def as_ui_config(self) -> dict[str, Any]:
        return {
            "custom_skill_categories": list(self.custom_categories),
            "skill_category_overrides": dict(self.overrides),
        }
