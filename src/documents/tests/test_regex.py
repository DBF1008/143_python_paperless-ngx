import pytest
import regex
from pytest_mock import MockerFixture

from documents.regex import is_unsafe_regex_pattern
from documents.regex import limit_content_length
from documents.regex import MATCH_CONTENT_MAX_LENGTH
from documents.regex import safe_regex_finditer
from documents.regex import safe_regex_match
from documents.regex import safe_regex_search
from documents.regex import safe_regex_sub
from documents.regex import validate_regex_pattern


class TestValidateRegexPattern:
    def test_valid_pattern(self) -> None:
        validate_regex_pattern(r"\d+")

    def test_invalid_pattern_raises(self) -> None:
        with pytest.raises(ValueError):
            validate_regex_pattern(r"[invalid")


class TestSafeRegexSearchAndMatch:
    """Tests for safe_regex_search and safe_regex_match (same contract)."""

    @pytest.mark.parametrize(
        ("func", "pattern", "text", "expected_group"),
        [
            pytest.param(
                safe_regex_search,
                r"\d+",
                "abc123def",
                "123",
                id="search-match-found",
            ),
            pytest.param(
                safe_regex_match,
                r"\d+",
                "123abc",
                "123",
                id="match-match-found",
            ),
        ],
    )
    def test_match_found(self, func, pattern, text, expected_group) -> None:
        result = func(pattern, text)
        assert result is not None
        assert result.group() == expected_group

    @pytest.mark.parametrize(
        ("func", "pattern", "text"),
        [
            pytest.param(safe_regex_search, r"\d+", "abcdef", id="search-no-match"),
            pytest.param(safe_regex_match, r"\d+", "abc123", id="match-no-match"),
        ],
    )
    def test_no_match(self, func, pattern, text) -> None:
        assert func(pattern, text) is None

    @pytest.mark.parametrize(
        "func",
        [
            pytest.param(safe_regex_search, id="search"),
            pytest.param(safe_regex_match, id="match"),
        ],
    )
    def test_invalid_pattern_returns_none(self, func) -> None:
        assert func(r"[invalid", "test") is None

    @pytest.mark.parametrize(
        "func",
        [
            pytest.param(safe_regex_search, id="search"),
            pytest.param(safe_regex_match, id="match"),
        ],
    )
    def test_flags_respected(self, func) -> None:
        assert func(r"abc", "ABC", flags=regex.IGNORECASE) is not None

    @pytest.mark.parametrize(
        ("func", "method_name"),
        [
            pytest.param(safe_regex_search, "search", id="search"),
            pytest.param(safe_regex_match, "match", id="match"),
        ],
    )
    def test_timeout_returns_none(
        self,
        func,
        method_name,
        mocker: MockerFixture,
    ) -> None:
        mock_compile = mocker.patch("documents.regex.regex.compile")
        getattr(mock_compile.return_value, method_name).side_effect = TimeoutError
        assert func(r"\d+", "test") is None


class TestSafeRegexSub:
    @pytest.mark.parametrize(
        ("pattern", "repl", "text", "expected"),
        [
            pytest.param(r"\d+", "NUM", "abc123def456", "abcNUMdefNUM", id="basic-sub"),
            pytest.param(r"\d+", "NUM", "abcdef", "abcdef", id="no-match"),
            pytest.param(r"abc", "X", "ABC", "X", id="flags"),
        ],
    )
    def test_substitution(self, pattern, repl, text, expected) -> None:
        flags = regex.IGNORECASE if pattern == r"abc" else 0
        result = safe_regex_sub(pattern, repl, text, flags=flags)
        assert result == expected

    def test_invalid_pattern_returns_none(self) -> None:
        assert safe_regex_sub(r"[invalid", "x", "test") is None

    def test_timeout_returns_none(self, mocker: MockerFixture) -> None:
        mock_compile = mocker.patch("documents.regex.regex.compile")
        mock_compile.return_value.sub.side_effect = TimeoutError
        assert safe_regex_sub(r"\d+", "X", "test") is None


class TestSafeRegexFinditer:
    def test_yields_matches(self) -> None:
        pattern = regex.compile(r"\d+")
        matches = list(safe_regex_finditer(pattern, "a1b22c333"))
        assert [m.group() for m in matches] == ["1", "22", "333"]

    def test_no_matches(self) -> None:
        pattern = regex.compile(r"\d+")
        assert list(safe_regex_finditer(pattern, "abcdef")) == []

    def test_timeout_stops_iteration(self, mocker: MockerFixture) -> None:
        mock_pattern = mocker.MagicMock()
        mock_pattern.finditer.side_effect = TimeoutError
        mock_pattern.pattern = r"\d+"
        assert list(safe_regex_finditer(mock_pattern, "test")) == []


class TestIsUnsafeRegexPattern:
    """Static unsafe-pattern detection should flag known ReDoS constructs
    while leaving common, legitimate patterns untouched."""

    @pytest.mark.parametrize(
        "pattern",
        [
            pytest.param(r"\d+", id="simple-quantifier"),
            pytest.param(r"\w{3,10}", id="bounded-quantifier"),
            pytest.param(r"\d{4}-\d{2}-\d{2}", id="date-format"),
            pytest.param(r"(?:foo|bar)+", id="non-capturing-alternation"),
            pytest.param(r"[\w\s]+", id="character-class-quantifier"),
            pytest.param(r"(abc)+", id="group-no-inner-quantifier"),
            pytest.param(r"(foo)?", id="optional-group"),
            pytest.param(r"INV-\d{4}-\d{3}", id="invoice-number"),
            pytest.param(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", id="email"),
            pytest.param(r"^\d{1,3}(?:\.\d{1,3}){3}$", id="ip-address"),
        ],
    )
    def test_safe_patterns_not_flagged(self, pattern: str) -> None:
        assert not is_unsafe_regex_pattern(pattern), (
            f"Pattern {pattern!r} should NOT be flagged as unsafe"
        )

    @pytest.mark.parametrize(
        "pattern",
        [
            pytest.param(r"(a+)+", id="nested-plus"),
            pytest.param(r"(\w+)*", id="nested-star"),
            pytest.param(r"(a+)+$", id="classic-redos"),
            pytest.param(r"(\d+)+", id="digit-nested"),
            pytest.param(r"(a*)+", id="zero-min-group"),
            pytest.param(r"(\s?)*", id="optional-in-star-group"),
            pytest.param(r"a**", id="adjacent-star"),
            pytest.param(r".*+", id="adjacent-dot-star"),
        ],
    )
    def test_unsafe_patterns_flagged(self, pattern: str) -> None:
        assert is_unsafe_regex_pattern(pattern), (
            f"Pattern {pattern!r} should be flagged as unsafe"
        )


class TestValidateRegexPatternUnsafe:
    def test_unsafe_pattern_raises(self) -> None:
        with pytest.raises(ValueError, match="unsafe"):
            validate_regex_pattern(r"(a+)+$")

    def test_safe_pattern_passes(self) -> None:
        validate_regex_pattern(r"\d{4}-\d{2}-\d{2}")

    def test_invalid_syntax_still_raises(self) -> None:
        with pytest.raises(ValueError):
            validate_regex_pattern(r"[invalid")


class TestLimitContentLength:
    def test_short_content_unchanged(self) -> None:
        assert limit_content_length("hello", max_length=100) == "hello"

    def test_exact_limit_unchanged(self) -> None:
        content = "x" * 100
        assert limit_content_length(content, max_length=100) == content

    def test_long_content_truncated(self) -> None:
        content = "x" * 200
        result = limit_content_length(content, max_length=100)
        assert len(result) == 100
        assert result == "x" * 100

    def test_default_limit_is_generous(self) -> None:
        # The default limit should be at least 1M characters
        assert MATCH_CONTENT_MAX_LENGTH >= 1_000_000
