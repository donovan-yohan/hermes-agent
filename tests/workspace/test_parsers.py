import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

try:
    import chonkie  # noqa: F401 — force early import to avoid xdist C-extension race
except ImportError:
    pass

from workspace.constants import BINARY_SUFFIXES, CODE_SUFFIXES, MARKDOWN_SUFFIXES, PARSEABLE_SUFFIXES


class TestParseableSuffixes:
    def test_contains_expected_extensions(self):
        assert PARSEABLE_SUFFIXES == frozenset({".pdf", ".docx", ".pptx"})

    def test_is_subset_of_binary_suffixes(self):
        assert PARSEABLE_SUFFIXES <= BINARY_SUFFIXES

    def test_no_overlap_with_code_suffixes(self):
        assert PARSEABLE_SUFFIXES & CODE_SUFFIXES == frozenset()

    def test_no_overlap_with_markdown_suffixes(self):
        assert PARSEABLE_SUFFIXES & MARKDOWN_SUFFIXES == frozenset()


from workspace.config import KnowledgebaseConfig, ParsingConfig


class TestParsingConfig:
    def test_default_values(self):
        cfg = ParsingConfig()
        assert cfg.default == "markitdown"
        assert cfg.overrides == {}

    def test_custom_default(self):
        cfg = ParsingConfig(default="pandoc")
        assert cfg.default == "pandoc"

    def test_per_extension_overrides(self):
        cfg = ParsingConfig(overrides={".docx": "pandoc"})
        assert cfg.overrides[".docx"] == "pandoc"

    def test_frozen(self):
        cfg = ParsingConfig()
        with pytest.raises(Exception):
            cfg.default = "pandoc"

    def test_nested_in_knowledgebase_config(self):
        kb = KnowledgebaseConfig()
        assert isinstance(kb.parsing, ParsingConfig)
        assert kb.parsing.default == "markitdown"

    def test_knowledgebase_config_from_raw(self):
        kb = KnowledgebaseConfig.model_validate(
            {"parsing": {"default": "pandoc", "overrides": {".docx": "pandoc"}}}
        )
        assert kb.parsing.default == "pandoc"
        assert kb.parsing.overrides == {".docx": "pandoc"}

    def test_unknown_keys_ignored(self):
        cfg = ParsingConfig.model_validate({"default": "markitdown", "future_key": True})
        assert cfg.default == "markitdown"
        assert not hasattr(cfg, "future_key")


class TestFileParserABC:
    def test_cannot_instantiate_directly(self):
        from workspace.parsers import FileParser

        with pytest.raises(TypeError):
            FileParser()

    def test_parse_returns_none_on_exception(self):
        from workspace.parsers import FileParser

        class ExplodingParser(FileParser):
            name = "exploding"

            def supported_suffixes(self) -> frozenset[str]:
                return frozenset({".boom"})

            def _convert(self, path: Path) -> str:
                raise RuntimeError("kaboom")

        parser = ExplodingParser()
        result = parser.parse(Path("/fake/file.boom"))
        assert result is None

    def test_parse_returns_none_on_empty_output(self):
        from workspace.parsers import FileParser

        class EmptyParser(FileParser):
            name = "empty"

            def supported_suffixes(self) -> frozenset[str]:
                return frozenset({".empty"})

            def _convert(self, path: Path) -> str:
                return "   \n  \n  "

        parser = EmptyParser()
        result = parser.parse(Path("/fake/file.empty"))
        assert result is None

    def test_parse_returns_content_on_success(self):
        from workspace.parsers import FileParser

        class GoodParser(FileParser):
            name = "good"

            def supported_suffixes(self) -> frozenset[str]:
                return frozenset({".good"})

            def _convert(self, path: Path) -> str:
                return "# Converted\n\nContent."

        parser = GoodParser()
        result = parser.parse(Path("/fake/file.good"))
        assert result == "# Converted\n\nContent."


@pytest.fixture
def mock_markitdown():
    """Mock the markitdown module regardless of whether it's installed."""
    mock_result = MagicMock()
    mock_result.markdown = "# Converted\n\nParsed content from document."
    mock_instance = MagicMock()
    mock_instance.convert.return_value = mock_result
    mock_class = MagicMock(return_value=mock_instance)

    module = types.ModuleType("markitdown")
    module.MarkItDown = mock_class

    with patch.dict(sys.modules, {"markitdown": module}):
        yield mock_instance


class TestMarkitdownParser:
    def test_name(self):
        from workspace.parsers import MarkitdownParser

        assert MarkitdownParser.name == "markitdown"

    def test_supported_suffixes(self):
        from workspace.parsers import MarkitdownParser

        suffixes = MarkitdownParser().supported_suffixes()
        assert ".pdf" in suffixes
        assert ".docx" in suffixes
        assert ".pptx" in suffixes

    def test_parse_calls_markitdown_convert(self, tmp_path, mock_markitdown):
        from workspace.parsers import MarkitdownParser

        pdf = tmp_path / "report.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake content")

        parser = MarkitdownParser()
        result = parser.parse(pdf)

        assert result == "# Converted\n\nParsed content from document."
        mock_markitdown.convert.assert_called_once_with(str(pdf))

    def test_parse_returns_none_when_markitdown_not_installed(self, tmp_path):
        from workspace.parsers import MarkitdownParser

        pdf = tmp_path / "report.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")

        with patch.dict(sys.modules, {"markitdown": None}):
            parser = MarkitdownParser()
            result = parser.parse(pdf)

        assert result is None


class TestPandocParser:
    def test_name(self):
        from workspace.parsers import PandocParser

        assert PandocParser.name == "pandoc"

    def test_supported_suffixes(self):
        from workspace.parsers import PandocParser

        suffixes = PandocParser().supported_suffixes()
        assert ".pdf" in suffixes
        assert ".docx" in suffixes
        assert ".pptx" in suffixes

    def test_parse_calls_pandoc_subprocess(self, tmp_path):
        from workspace.parsers import PandocParser

        docx = tmp_path / "report.docx"
        docx.write_bytes(b"PK fake docx content")

        mock_completed = MagicMock()
        mock_completed.stdout = "# Converted\n\nFrom pandoc."
        mock_completed.check_returncode = MagicMock()

        with patch("workspace.parsers.subprocess.run", return_value=mock_completed) as mock_run:
            parser = PandocParser()
            result = parser.parse(docx)

        assert result == "# Converted\n\nFrom pandoc."
        mock_run.assert_called_once_with(
            ["pandoc", str(docx), "-t", "markdown"],
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_parse_returns_none_when_pandoc_not_found(self, tmp_path):
        from workspace.parsers import PandocParser

        docx = tmp_path / "report.docx"
        docx.write_bytes(b"PK fake")

        with patch(
            "workspace.parsers.subprocess.run",
            side_effect=FileNotFoundError("pandoc not found"),
        ):
            parser = PandocParser()
            result = parser.parse(docx)

        assert result is None

    def test_parse_returns_none_on_nonzero_exit(self, tmp_path):
        import subprocess as sp
        from workspace.parsers import PandocParser

        docx = tmp_path / "report.docx"
        docx.write_bytes(b"PK fake")

        mock_completed = MagicMock()
        mock_completed.check_returncode.side_effect = sp.CalledProcessError(1, "pandoc")

        with patch("workspace.parsers.subprocess.run", return_value=mock_completed):
            parser = PandocParser()
            result = parser.parse(docx)

        assert result is None


class TestCompositeParser:
    def _make_stub_parser(self, name: str, suffixes: frozenset[str], output: str):
        """Create a concrete FileParser stub for testing."""
        from workspace.parsers import FileParser

        class StubParser(FileParser):
            def supported_suffixes(self) -> frozenset[str]:
                return suffixes

            def _convert(self, path):
                return output

        StubParser.name = name
        return StubParser()

    def test_routes_to_correct_parser(self, tmp_path):
        from workspace.parsers import CompositeParser

        pdf_parser = self._make_stub_parser("pdf_backend", frozenset({".pdf"}), "# From PDF")
        docx_parser = self._make_stub_parser("docx_backend", frozenset({".docx"}), "# From DOCX")

        composite = CompositeParser({".pdf": pdf_parser, ".docx": docx_parser})

        pdf = tmp_path / "test.pdf"
        pdf.write_bytes(b"fake")
        assert composite.parse(pdf) == "# From PDF"

        docx = tmp_path / "test.docx"
        docx.write_bytes(b"fake")
        assert composite.parse(docx) == "# From DOCX"

    def test_returns_none_for_unknown_suffix(self, tmp_path):
        from workspace.parsers import CompositeParser

        composite = CompositeParser({})
        txt = tmp_path / "test.txt"
        txt.write_text("hello")
        assert composite.parse(txt) is None

    def test_can_parse(self):
        from workspace.parsers import CompositeParser

        stub = self._make_stub_parser("stub", frozenset({".pdf"}), "content")
        composite = CompositeParser({".pdf": stub})

        assert composite.can_parse(".pdf") is True
        assert composite.can_parse(".docx") is False
        assert composite.can_parse(".txt") is False


class TestBuildParser:
    def test_default_config_routes_all_parseable_suffixes(self, mock_markitdown):
        from workspace.config import ParsingConfig
        from workspace.parsers import build_parser

        composite = build_parser(ParsingConfig())
        assert composite.can_parse(".pdf")
        assert composite.can_parse(".docx")
        assert composite.can_parse(".pptx")
        assert not composite.can_parse(".txt")

    def test_override_routes_extension_to_different_backend(self, mock_markitdown):
        from workspace.config import ParsingConfig
        from workspace.parsers import build_parser

        cfg = ParsingConfig(overrides={".docx": "pandoc"})
        composite = build_parser(cfg)

        # .docx should be routed to pandoc, not markitdown
        assert composite._routing[".docx"].name == "pandoc"
        assert composite._routing[".pdf"].name == "markitdown"

    def test_unknown_backend_name_skips_extension(self, mock_markitdown):
        from workspace.config import ParsingConfig
        from workspace.parsers import build_parser

        cfg = ParsingConfig(overrides={".pdf": "nonexistent_backend"})
        composite = build_parser(cfg)

        assert not composite.can_parse(".pdf")
        assert composite.can_parse(".docx")

    def test_pandoc_as_default(self, mock_markitdown):
        from workspace.config import ParsingConfig
        from workspace.parsers import build_parser

        cfg = ParsingConfig(default="pandoc")
        composite = build_parser(cfg)

        assert composite._routing[".pdf"].name == "pandoc"
        assert composite._routing[".docx"].name == "pandoc"
        assert composite._routing[".pptx"].name == "pandoc"


from workspace.files import discover_workspace_files


class TestDiscoveryWithParseableFiles:
    def test_pdf_files_are_discovered(self, make_workspace_config):
        cfg = make_workspace_config()
        pdf = cfg.workspace_root / "docs" / "report.pdf"
        pdf.parent.mkdir(parents=True, exist_ok=True)
        pdf.write_bytes(b"%PDF-1.4 fake content")

        result = discover_workspace_files(cfg)
        discovered_names = [p.name for _, p in result.files]
        assert "report.pdf" in discovered_names

    def test_docx_files_are_discovered(self, make_workspace_config):
        cfg = make_workspace_config()
        docx = cfg.workspace_root / "docs" / "report.docx"
        docx.parent.mkdir(parents=True, exist_ok=True)
        docx.write_bytes(b"PK fake docx")

        result = discover_workspace_files(cfg)
        discovered_names = [p.name for _, p in result.files]
        assert "report.docx" in discovered_names

    def test_pptx_files_are_discovered(self, make_workspace_config):
        cfg = make_workspace_config()
        pptx = cfg.workspace_root / "docs" / "slides.pptx"
        pptx.parent.mkdir(parents=True, exist_ok=True)
        pptx.write_bytes(b"PK fake pptx")

        result = discover_workspace_files(cfg)
        discovered_names = [p.name for _, p in result.files]
        assert "slides.pptx" in discovered_names

    def test_true_binaries_still_excluded(self, make_workspace_config):
        cfg = make_workspace_config()
        exe = cfg.workspace_root / "docs" / "app.exe"
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_bytes(b"MZ fake exe")

        result = discover_workspace_files(cfg)
        discovered_names = [p.name for _, p in result.files]
        assert "app.exe" not in discovered_names


class TestDefaultIndexerParsing:
    def test_indexes_pdf_via_parser(self, make_workspace_config, mock_markitdown):
        cfg = make_workspace_config()
        pdf = cfg.workspace_root / "docs" / "report.pdf"
        pdf.parent.mkdir(parents=True, exist_ok=True)
        pdf.write_bytes(b"%PDF-1.4 fake content")

        from workspace.default import DefaultIndexer

        indexer = DefaultIndexer(cfg)
        summary = indexer.index()

        assert summary.files_indexed == 1
        assert summary.files_errored == 0
        assert summary.chunks_created >= 1

    def test_parsed_content_is_searchable(self, make_workspace_config, mock_markitdown):
        cfg = make_workspace_config()
        pdf = cfg.workspace_root / "docs" / "report.pdf"
        pdf.parent.mkdir(parents=True, exist_ok=True)
        pdf.write_bytes(b"%PDF-1.4 fake content")

        from workspace.default import DefaultIndexer

        indexer = DefaultIndexer(cfg)
        indexer.index()

        results = indexer.search("Parsed content")
        assert len(results) > 0
        assert any("report.pdf" in r.path for r in results)

    def test_parse_failure_counts_as_error(self, make_workspace_config, mock_markitdown):
        cfg = make_workspace_config()
        pdf = cfg.workspace_root / "docs" / "broken.pdf"
        pdf.parent.mkdir(parents=True, exist_ok=True)
        pdf.write_bytes(b"%PDF-1.4 fake")

        mock_markitdown.convert.side_effect = RuntimeError("corrupt PDF")

        from workspace.default import DefaultIndexer

        indexer = DefaultIndexer(cfg)
        summary = indexer.index()

        assert summary.files_errored == 1
        assert summary.files_indexed == 0
        assert any(e.stage == "parse" for e in summary.errors)

    def test_text_files_still_use_read_file_text(self, make_workspace_config, write_file):
        """Ensure non-parseable text files still go through the normal path."""
        cfg = make_workspace_config()
        write_file(cfg.workspace_root / "docs" / "readme.md", "# Hello\n\nWorld.\n")

        from workspace.default import DefaultIndexer

        indexer = DefaultIndexer(cfg)
        summary = indexer.index()

        assert summary.files_indexed == 1
        assert summary.files_errored == 0
