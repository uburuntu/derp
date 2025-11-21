"""Tests for common utility functions."""

import pytest
from unittest.mock import patch

from derp.common.utils import one_liner, percent_chance


class TestOneLiner:
    """Tests for one_liner function that converts multiline text to single line."""

    def test_single_line_unchanged(self):
        """Single line text should remain unchanged."""
        text = "This is a single line"
        result = one_liner(text)
        assert result == "This is a single line"

    def test_newlines_replaced_with_spaces(self):
        """Newlines should be replaced with spaces."""
        text = "Line 1\nLine 2\nLine 3"
        result = one_liner(text)
        assert result == "Line 1 Line 2 Line 3"

    def test_multiple_spaces_collapsed(self):
        """Multiple consecutive spaces should be collapsed to single space."""
        text = "Too    many     spaces"
        result = one_liner(text)
        assert result == "Too many spaces"

    def test_newlines_and_spaces_combined(self):
        """Should handle both newlines and multiple spaces."""
        text = "Line 1\n\nLine 2    with  spaces"
        result = one_liner(text)
        assert result == "Line 1 Line 2 with spaces"

    def test_cut_len_truncates(self):
        """Should truncate text to specified length."""
        text = "This is a very long line that should be cut"
        result = one_liner(text, cut_len=20)
        assert result == "This is a very long "
        assert len(result) == 20

    def test_cut_len_none_no_truncation(self):
        """cut_len=None should not truncate."""
        text = "This should not be truncated"
        result = one_liner(text, cut_len=None)
        assert result == "This should not be truncated"

    def test_cut_len_shorter_than_text(self):
        """Should handle cut_len shorter than text."""
        text = "Short"
        result = one_liner(text, cut_len=10)
        assert result == "Short"

    def test_empty_string(self):
        """Should handle empty strings."""
        result = one_liner("")
        assert result == ""

    def test_only_newlines(self):
        """Should handle strings with only newlines."""
        text = "\n\n\n"
        result = one_liner(text)
        assert result == " "

    def test_tabs_preserved(self):
        """Tabs should be preserved (only newlines and double spaces are processed)."""
        text = "Text\twith\ttabs"
        result = one_liner(text)
        assert result == "Text\twith\ttabs"

    def test_mixed_whitespace(self):
        """Should handle mixed whitespace correctly."""
        text = "Line 1\n  Line 2\n    Line 3"
        result = one_liner(text)
        assert result == "Line 1 Line 2 Line 3"


class TestPercentChance:
    """Tests for percent_chance function."""

    def test_zero_percent_always_false(self):
        """0% chance should always return False."""
        for _ in range(100):
            assert percent_chance(0.0) is False

    def test_hundred_percent_always_true(self):
        """100% chance should always return True."""
        for _ in range(100):
            assert percent_chance(100.0) is True

    def test_fifty_percent_statistical(self):
        """50% chance should return roughly half True, half False."""
        results = [percent_chance(50.0) for _ in range(1000)]
        true_count = sum(results)
        # Allow some variance, but should be close to 500
        assert 400 < true_count < 600

    @patch('derp.common.utils.random.random')
    def test_percent_conversion(self, mock_random):
        """Should correctly convert percent to decimal for comparison."""
        mock_random.return_value = 0.24  # 24%

        assert percent_chance(25.0) is True   # 25% > 24%
        assert percent_chance(24.0) is False  # 24% not > 24%
        assert percent_chance(23.0) is False  # 23% < 24%

    def test_negative_percent_raises_error(self):
        """Negative percentages should raise ValueError."""
        with pytest.raises(ValueError, match=r"between 0\. an 100\."):
            percent_chance(-1.0)

    def test_over_hundred_raises_error(self):
        """Percentages over 100 should raise ValueError."""
        with pytest.raises(ValueError, match=r"between 0\. an 100\."):
            percent_chance(101.0)

    def test_exactly_100_valid(self):
        """Exactly 100.0 should be valid."""
        assert percent_chance(100.0) is True

    def test_exactly_0_valid(self):
        """Exactly 0.0 should be valid."""
        assert percent_chance(0.0) is False

    def test_decimal_percentages(self):
        """Should handle decimal percentages."""
        # Just verify it doesn't crash
        percent_chance(50.5)
        percent_chance(0.1)
        percent_chance(99.9)

    @patch('derp.common.utils.random.random')
    def test_edge_case_random_equals_chance(self, mock_random):
        """When random equals the chance exactly, should return False."""
        mock_random.return_value = 0.5
        # percent_chance uses < not <=, so equal should be False
        assert percent_chance(50.0) is False

    @patch('derp.common.utils.random.random')
    def test_very_small_chance(self, mock_random):
        """Should handle very small percentages correctly."""
        mock_random.return_value = 0.001  # 0.1%
        assert percent_chance(0.2) is True
        assert percent_chance(0.05) is False
