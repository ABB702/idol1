"""Markdown-aware chunks with bounded context; no model download required."""

import re


def token_cost(text):
    # Conservative sizing heuristic, not an exact model tokenizer count.
    return len(re.findall(r"[\u3400-\u9fff]|[A-Za-z0-9_]{1,4}|[^\w\s]", text))


def split_document(text: str, size=300) -> list[dict]:
    if size <= 0:
        raise ValueError("Invalid chunk size")
    result, headings, paragraph = [], [], []
    lines = text.splitlines(keepends=True)
    previous = ""

    def fits(value):
        return len(value) <= 1500 and token_cost(value) <= size

    def pieces(value, code=False):
        if fits(value):
            return [value]
        units = (
            value.splitlines(keepends=True)
            if code
            else re.split(r"(?<=[。！？!?])|(?<=\.)(?=\s)|(?<=\n)", value)
        )
        output, current = [], ""
        for unit in units:
            if code and current and not unit.strip() and token_cost(current) >= size // 2:
                output.append(current)
                current = ""
                continue
            # Hard splitting is only a fallback for a single oversized sentence/line.
            atoms = [unit] if fits(unit) else re.findall(r"\S+\s*|\s+", unit)
            for atom in atoms:
                while atom:
                    part, atom = (atom, "") if fits(atom) else (atom[:size], atom[size:])
                    if current and not fits(current + part):
                        output.append(current)
                        current = ""
                    current += part
        if current:
            output.append(current)
        return output

    def emit(value, code=False, fence="```", language=""):
        nonlocal previous
        value = value.strip("\n") if code else value.strip()
        if not value:
            return
        title = " > ".join(part for part in headings if part)[-300:]
        table_lines = value.splitlines()
        if (
            not code
            and len(table_lines) >= 2
            and re.fullmatch(r"\s*\|?[\s:|-]+\|?\s*", table_lines[1])
            and "|" in table_lines[0]
        ):
            header = "\n".join(table_lines[:2]) + "\n"
            parts, current = [], header
            for row in table_lines[2:]:
                if current != header and not fits(current + row):
                    parts.append(current)
                    current = header
                current += row + "\n"
            if current != header:
                parts.append(current)
            if not parts:
                parts = [value]
        else:
            parts = pieces(value, code)
        for part in parts:
            child = f"{fence}{language}\n{part.rstrip(chr(10))}\n{fence}" if code else part.strip()
            # Include short explanatory prose with code without slicing through it.
            context = child
            if code and previous and len(previous) <= 240 and len(previous + child) < 1700:
                context = previous + "\n\n" + child
            if title and len(title + context) < 1800:
                context = title + "\n\n" + context
            result.append(
                {"text": child, "parent_text": context, "heading": title, "offset": 0, "is_code": int(code)}
            )
        previous = "" if code else value

    def flush():
        if paragraph:
            emit("".join(paragraph))
            paragraph.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        opening = re.match(r"^\s{0,3}(`{3,}|~{3,})([^\n]*)", line)
        heading = re.match(r"^(#{1,6})\s+(.+)", line)
        if opening:
            flush()
            fence, language = opening.groups()
            body = []
            i += 1
            while i < len(lines):
                if re.fullmatch(
                    r"\s{0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*", lines[i]
                ):
                    break
                body.append(lines[i])
                i += 1
            emit("".join(body), True, fence, language.strip())
        elif heading:
            flush()
            level, title = len(heading[1]), heading[2].strip()
            headings = headings[: level - 1]
            headings.extend([""] * max(0, level - 1 - len(headings)))
            headings.append(title)
            previous = ""
        elif not line.strip():
            flush()
        else:
            paragraph.append(line)
        i += 1
    flush()
    return result
