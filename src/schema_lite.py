"""零依赖的 JSON Schema 子集校验器。

项目环境未安装 jsonschema，合约只用到 draft 2020-12 的常见关键字，
因此在此实现刚好够用的子集：$ref(仅限 #/$defs)、type、const、enum、
required、properties、additionalProperties=false、items、minItems、
minimum/maximum、minLength、pattern，以及类型数组（含 null）。
"""

import re
from typing import Any

_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def _resolve(schema_root: dict, schema: dict) -> dict:
    ref = schema.get("$ref")
    if not ref:
        return schema
    assert ref.startswith("#/$defs/"), f"暂不支持的引用: {ref}"
    name = ref.split("/", 2)[2]
    return schema_root["$defs"][name]


def validate(instance: Any, schema: dict, schema_root: dict | None = None,
             path: str = "$") -> list[str]:
    """返回错误信息列表；为空表示通过。"""
    root = schema_root or schema
    errors: list[str] = []
    schema = _resolve(root, schema)

    declared = schema.get("type")
    if declared is not None:
        types = declared if isinstance(declared, list) else [declared]
        if not any(_TYPE_CHECKS[t](instance) for t in types):
            errors.append(f"{path}: 类型应为 {declared}，实际为 {type(instance).__name__}")
            return errors  # 类型不符后后续关键字无意义

    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: 应恒等于 {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} 不在允许范围内")

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(f"{path}: 字符串短于 {schema['minLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            errors.append(f"{path}: {instance!r} 不匹配 {schema['pattern']}")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: {instance} 小于最小值 {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: {instance} 大于最大值 {schema['maximum']}")

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{path}: 元素少于 {schema['minItems']}")
        item_schema = schema.get("items")
        if item_schema:
            for i, item in enumerate(instance):
                errors += validate(item, item_schema, root, f"{path}[{i}]")

    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}: 缺少必要字段 {key!r}")
        props = schema.get("properties", {})
        for key, value in instance.items():
            if key in props:
                errors += validate(value, props[key], root, f"{path}.{key}")
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: 不允许额外字段 {key!r}")

    return errors
