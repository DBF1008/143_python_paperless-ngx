import pytest
import regex
from pytest_mock import MockerFixture

from documents.regex import MATCH_CONTENT_MAX_LENGTH
from documents.regex import safe_regex_finditer
from documents.regex import safe_regex_match
from documents.regex import safe_regex_search
from documents.regex import safe_regex_sub
from documents.regex import validate_regex_pattern
from documents.regex import validate_regex_safety


class TestValidateRegexPattern:
    def test_valid_pattern(self) -> None:
        validate_regex_pattern(r"\d+")

    def test_invalid_pattern_raises(self) -> None:
        with pytest.raises(ValueError):
            validate_regex_pattern(r"[invalid")


class TestValidateRegexSafety:
    """
    Conservative detection of nested unbounded quantifiers (ReDoS). Dangerous
    shapes must raise; legitimate patterns must be left untouched.
    """

    @pytest.mark.parametrize(
        "pattern",
        [
            pytest.param(r"(a+)+", id="nested-plus-plus"),
            pytest.param(r"(a*)*", id="nested-star-star"),
            pytest.param(r"(a+)*", id="nested-plus-star"),
            pytest.param(r"(.+)+", id="nested-dot-plus"),
            pytest.param(r"(\w+)*", id="nested-word-star"),
            pytest.param(r"([a-z]+)*", id="nested-class-star"),
            pytest.param(r"(\d{2,})+", id="nested-openrange-plus"),
            pytest.param(r"(a+)+$", id="nested-anchored"),
            pytest.param(r"(?:a+)+", id="nested-noncapturing"),
        ],
    )
    def test_unsafe_pattern_raises(self, pattern) -> None:
        with pytest.raises(ValueError):
            validate_regex_safety(pattern)

    @pytest.mark.parametrize(
        "pattern",
        [
            pytest.param(r"\d+", id="simple-digits"),
            pytest.param(r"\w+", id="simple-word"),
            pytest.param(r"[a-z]+", id="simple-class"),
            pytest.param(r"(\d+)", id="group-no-outer-quantifier"),
            pytest.param(r"(abc)+", id="group-no-inner-quantifier"),
            pytest.param(r"alpha\w+gamma", id="single-quantifier"),
            pytest.param(r"(\d{4})-(\d{2})", id="bounded-groups"),
            pytest.param(r"([+*])+", id="charclass-literals"),
            pytest.param(r"(a{2,5})+", id="bounded-inner-range"),
            pytest.param(r"(foo|bar)+", id="alternation"),
        ],
    )
    def test_safe_pattern_allowed(self, pattern) -> None:
        # Must not raise.
        validate_regex_safety(pattern)

    def test_content_max_length_is_positive_int(self) -> None:
        assert isinstance(MATCH_CONTENT_MAX_LENGTH, int)
        assert MATCH_CONTENT_MAX_LENGTH > 0


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

    @pytest.mark.parametrize(
        "func",
        [
            pytest.param(safe_regex_search, id="search"),
            pytest.param(safe_regex_match, id="match"),
        ],
    )
    def test_unsafe_pattern_returns_none(
        self,
        func,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level("WARNING", logger="paperless.regex"):
            assert func(r"(a+)+", "a" * 20) is None
        assert "unsafe" in caplog.text.lower()


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

    def test_unsafe_pattern_returns_none(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level("WARNING", logger="paperless.regex"):
            assert safe_regex_sub(r"(a+)+", "X", "a" * 20) is None
        assert "unsafe" in caplog.text.lower()


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
