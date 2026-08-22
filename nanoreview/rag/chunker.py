"""Multi-language code chunking using Tree-sitter."""

from __future__ import annotations

import re
from typing import Any

from loguru import logger

from nanoreview.rag.utils import IndexedChunk

_SYMBOL_RE = re.compile(r"\b[A-Z][A-Za-z0-9_]*|[a-z_][A-Za-z0-9_]*(?=\s*\()")

LANGUAGE_MAP: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".cs": "c_sharp",
    ".rb": "ruby",
    ".kt": "kotlin",
    ".swift": "swift",
}

DEFINITION_TYPES: dict[str, list[str]] = {
    "python": ["function_definition", "class_definition"],
    "javascript": [
        "function_declaration", "class_declaration",
        "method_definition", "arrow_function",
    ],
    "typescript": [
        "function_declaration", "class_declaration",
        "method_definition", "arrow_function",
    ],
    "go": ["function_declaration", "method_declaration", "type_declaration"],
    "rust": ["function_item", "impl_item", "struct_item", "enum_item"],
    "java": ["method_declaration", "class_declaration", "interface_declaration"],
    "c": ["function_definition", "struct_specifier"],
    "cpp": ["function_definition", "class_specifier", "struct_specifier"],
    "c_sharp": ["method_declaration", "class_declaration", "interface_declaration"],
    "ruby": ["method", "class", "module"],
    "kotlin": ["function_declaration", "class_declaration"],
    "swift": ["function_declaration", "class_declaration", "struct_declaration"],
}

# Maps language name -> module import path for tree-sitter grammar packages
_GRAMMAR_MODULES: dict[str, str] = {
    "python": "tree_sitter_python",
    "javascript": "tree_sitter_javascript",
    "typescript": "tree_sitter_typescript",
    "go": "tree_sitter_go",
    "rust": "tree_sitter_rust",
    "java": "tree_sitter_java",
    "c": "tree_sitter_c",
    "cpp": "tree_sitter_cpp",
    "c_sharp": "tree_sitter_c_sharp",
    "ruby": "tree_sitter_ruby",
    "kotlin": "tree_sitter_kotlin",
    "swift": "tree_sitter_swift",
}


class TreeSitterChunker:
    """Language-aware code chunker using Tree-sitter grammars (loaded on demand)."""

    def __init__(self, max_chunk_lines: int = 200) -> None:
        self.max_chunk_lines = max_chunk_lines
        self._parsers: dict[str, Any] = {}
        self._unavailable: set[str] = set()

    def can_parse(self, suffix: str) -> bool:
        lang = LANGUAGE_MAP.get(suffix.lower())
        if not lang or lang in self._unavailable:
            return False
        return self._get_parser(lang) is not None

    def chunk_file(
        self, rel_path: str, text: str, suffix: str, *, source_type: str = "repo"
    ) -> list[IndexedChunk]:
        lang = LANGUAGE_MAP.get(suffix.lower())
        if not lang:
            return []
        parser = self._get_parser(lang)
        if parser is None:
            return []

        tree = parser.parse(text.encode("utf-8"))
        definition_types = set(DEFINITION_TYPES.get(lang, []))
        if not definition_types:
            return []

        lines = text.replace("\r\n", "\n").splitlines()
        chunks: list[IndexedChunk] = []

        for node in self._walk_definitions(tree.root_node, definition_types):
            start = node.start_point[0] + 1
            end = node.end_point[0] + 1
            end = min(end, len(lines))
            if start > end:
                continue
            if end - start > self.max_chunk_lines:
                end = start + self.max_chunk_lines

            kind = self._node_kind(node)
            symbol = self._node_name(node)
            chunk_text = "\n".join(lines[start - 1:end])
            chunks.append(IndexedChunk(
                source_type=source_type,
                path=rel_path,
                start_line=start,
                end_line=end,
                text=chunk_text,
                symbols=[symbol] if symbol else [],
                kind=kind,
            ))

        return chunks

    def extract_symbols(self, text: str, suffix: str) -> list[str]:
        lang = LANGUAGE_MAP.get(suffix.lower())
        if not lang:
            return list(dict.fromkeys(_SYMBOL_RE.findall(text)))
        parser = self._get_parser(lang)
        if parser is None:
            return list(dict.fromkeys(_SYMBOL_RE.findall(text)))

        tree = parser.parse(text.encode("utf-8"))
        definition_types = set(DEFINITION_TYPES.get(lang, []))
        symbols: list[str] = []
        for node in self._walk_definitions(tree.root_node, definition_types):
            name = self._node_name(node)
            if name:
                symbols.append(name)
        return list(dict.fromkeys(symbols))

    def _get_parser(self, lang: str) -> Any:
        if lang in self._parsers:
            return self._parsers[lang]
        if lang in self._unavailable:
            return None
        try:
            import importlib

            import tree_sitter
            mod_name = _GRAMMAR_MODULES.get(lang)
            if not mod_name:
                self._unavailable.add(lang)
                return None
            mod = importlib.import_module(mod_name)
            language = tree_sitter.Language(mod.language())
            parser = tree_sitter.Parser(language)
            self._parsers[lang] = parser
            return parser
        except (ImportError, Exception) as e:
            logger.debug("Tree-sitter grammar unavailable for {}: {}", lang, e)
            self._unavailable.add(lang)
            return None

    @staticmethod
    def _walk_definitions(node: Any, types: set[str]) -> list[Any]:
        results: list[Any] = []
        stack = [node]
        while stack:
            n = stack.pop()
            if n.type in types:
                results.append(n)
            else:
                stack.extend(reversed(n.children))
        return results

    @staticmethod
    def _node_kind(node: Any) -> str:
        t = node.type
        if "class" in t or "struct" in t or "impl" in t or "enum" in t or "interface" in t:
            return "class"
        return "function"

    @staticmethod
    def _node_name(node: Any) -> str:
        for child in node.children:
            if child.type in ("identifier", "name", "property_identifier", "type_identifier"):
                return child.text.decode("utf-8") if isinstance(child.text, bytes) else child.text
        return ""
