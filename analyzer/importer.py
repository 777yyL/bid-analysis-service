"""Import the supplied dictionary exports as data; never execute their SQL."""

import hashlib
import re
from pathlib import Path

from analyzer.schemas import BusinessLine, Keyword, KnowledgeInput, ProductKeyword


def parse_insert_rows(text: str, table: str) -> list[dict]:
    """Accept only explicit INSERT ... VALUES literals for one allowlisted table."""
    if table not in {"biz_knowledge", "biz_keyword"}:
        raise ValueError("不支持的导入表")
    header = re.compile(
        r"\s*INSERT\s+INTO\s+(?:public\.)?" + re.escape(table)
        + r"\s*\(([^)]+)\)\s+VALUES\s*", re.IGNORECASE,
    )
    pos, result = 0, []
    while text[pos:].strip():
        matched = header.match(text, pos)
        if not matched:
            raise ValueError("导入内容只能包含指定表的 INSERT VALUES 数据")
        columns = [part.strip() for part in matched[1].split(",")]
        if len(columns) != len(set(columns)) or any(not re.fullmatch(r"[a-z_]+", c) for c in columns):
            raise ValueError("导入列名无效或重复")
        pos = matched.end()
        while True:
            while pos < len(text) and text[pos].isspace():
                pos += 1
            if pos >= len(text) or text[pos] != "(":
                raise ValueError("导入 VALUES 缺少行")
            pos += 1
            values = []
            for column_index in range(len(columns)):
                while pos < len(text) and text[pos].isspace():
                    pos += 1
                if pos < len(text) and text[pos] == "'":
                    pos += 1
                    chars = []
                    while pos < len(text):
                        if text[pos] == "'":
                            if text[pos:pos + 2] == "''":
                                chars.append("'")
                                pos += 2
                                continue
                            pos += 1
                            break
                        chars.append(text[pos])
                        pos += 1
                    else:
                        raise ValueError("导入字符串未闭合")
                    value = "".join(chars)
                else:
                    literal = re.match(r"(?:NULL|true|false|[+-]?\d+(?:\.\d+)?)", text[pos:], re.I)
                    if not literal:
                        raise ValueError("导入值必须为字符串、数值、布尔或 NULL 字面量")
                    token = literal[0]
                    pos += len(token)
                    lowered = token.lower()
                    if lowered in {"null", "true", "false"}:
                        value = {"null": None, "true": True, "false": False}[lowered]
                    else:
                        value = float(token) if "." in token else int(token)
                values.append(value)
                while pos < len(text) and text[pos].isspace():
                    pos += 1
                delimiter = ")" if column_index == len(columns) - 1 else ","
                if pos >= len(text) or text[pos] != delimiter:
                    raise ValueError("导入列数量或分隔符不匹配")
                pos += 1
            result.append(dict(zip(columns, values, strict=True)))
            while pos < len(text) and text[pos].isspace():
                pos += 1
            if pos < len(text) and text[pos] == ",":
                pos += 1
                continue
            if pos >= len(text) or text[pos] != ";":
                raise ValueError("导入 INSERT 必须以分号结束")
            pos += 1
            break
    return result


def _product_tokens(text: str) -> list[str]:
    tokens, current, depth = [], [], 0
    for char in text:
        if char in "（(":
            depth += 1
        elif char in "）)":
            depth -= 1
            if depth < 0:
                raise ValueError("产品词括号不匹配")
        if char in "、，," and depth == 0:
            if "".join(current).strip():
                tokens.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if depth:
        raise ValueError("产品词括号未闭合")
    if "".join(current).strip():
        tokens.append("".join(current).strip())
    return tokens


def _products(text: str, warnings: list[str]) -> list[ProductKeyword]:
    weak = {"塔式", "工控", "监控", "音频", "准入", "黑板", "光谱", "测温", "显示系统",
            "视频会议", "网络安全", "商业密码", "国密", "等保", "人员通道", "人行通道"}
    results, seen = [], set()
    for match in re.finditer(r"【([^】]+)】([^【]*)", text):
        category, entries = match[1].strip(), match[2].strip()
        for token in _product_tokens(entries):
            notes = re.findall(r"[（(]([^）)]*)[）)]", token)
            word = re.sub(r"[（(][^）)]*[）)]", "", token).strip()
            if not word:
                continue
            excludes = []
            for note in notes:
                if note.startswith("排除"):
                    excludes.extend(part.strip() for part in re.split(r"[、，,]", note[2:]) if part.strip())
                else:
                    warnings.append(f"产品词 {word} 的说明待人工确认：{note}")
            if word == "电子警擦":
                warnings.append("产品词“电子警擦”疑似错字，已保留原词，发布前请确认")
            if (category, word) not in seen:
                seen.add((category, word))
                results.append(ProductKeyword(category=category, word=word,
                    strength="weak" if word in weak else "strong", exclude_patterns=excludes))
    if not results:
        raise ValueError("产品词文件没有识别到【产品分类】关键词格式")
    return results


def import_knowledge_files(directory: str) -> KnowledgeInput:
    root = Path(directory)
    files = {name: (root / name).read_text(encoding="utf-8-sig")
             for name in ("业务域.txt", "关键词.txt", "产品关键词.txt")}
    line_rows = parse_insert_rows(files["业务域.txt"], "biz_knowledge")
    keyword_rows = parse_insert_rows(files["关键词.txt"], "biz_keyword")
    warnings = []
    if any("待补充" in str(row.get("body_md", "")) for row in line_rows):
        warnings.append("业务知识含待补充段落；上下文规则需结合实际采购样本验证")
    lines = [BusinessLine(**{name: row.get(name, "")
                            for name in ("business_domain", "business_line", "body_md")})
             for row in line_rows]
    keywords = [Keyword(**{name: row[name] for name in Keyword.model_fields if name in row})
                for row in keyword_rows]
    digest = hashlib.sha256("\n".join(files.values()).encode()).hexdigest()[:16]
    products = _products(files["产品关键词.txt"], warnings)
    return KnowledgeInput(version=f"files-{digest}", lines=lines, keywords=keywords,
                          product_keywords=products,
                          quality="full" if lines and all(line.body_md.strip() for line in lines)
                          else "keywords_only", warnings=warnings,
                          source_rubric={"source": "local_txt_export",
                                         "product_dimension": "independent"})
