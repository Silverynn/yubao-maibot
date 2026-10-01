"""只改变插件配置页的中文显示，不改 TOML 键名、默认值或实际行为。"""


def localize_schema(schema, labels):
    """MaiBot WebUI 使用 section.title 和 field.label，而不是 description 当标题。"""
    sections = schema.get("sections") or {}
    for section_key, (title, field_labels) in labels.items():
        section = sections.get(section_key)
        if not isinstance(section, dict):
            continue
        section["title"] = title
        fields = section.get("fields") or {}
        for field_key, label in field_labels.items():
            if field_key in fields:
                fields[field_key]["label"] = label
    return schema
