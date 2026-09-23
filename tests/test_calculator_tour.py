from __future__ import annotations

import unittest
from urllib.parse import quote

from agentpro.demos.calculator_tour import (
    _CALCULATOR_HTML,
    build_calculator_data_url,
    build_calculator_demo,
)


class CalculatorTourTests(unittest.TestCase):
    def test_data_url_shape(self) -> None:
        url = build_calculator_data_url()
        self.assertTrue(url.startswith("data:text/html;charset=utf-8,"))

    def test_data_url_is_encoded_html(self) -> None:
        url = build_calculator_data_url()
        expected = "data:text/html;charset=utf-8," + quote(_CALCULATOR_HTML, safe="")
        self.assertEqual(url, expected)

    def test_html_contains_digits_and_operators(self) -> None:
        for digit in ("A('0')", "A('1')", "A('2')", "A('3')", "A('4')",
                      "A('5')", "A('6')", "A('7')", "A('8')", "A('9')"):
            self.assertIn(digit, _CALCULATOR_HTML, msg=f"missing {digit}")

    def test_html_contains_operators(self) -> None:
        for op in ("A('+')", "A('-')", "A('*')", "A('/')"):
            self.assertIn(op, _CALCULATOR_HTML)

    def test_html_uses_function_not_eval(self) -> None:
        # Safety: arithmetic evaluated via Function, not global eval().
        self.assertIn("Function", _CALCULATOR_HTML)
        self.assertNotIn("eval(", _CALCULATOR_HTML)

    def test_html_has_clear_and_equals(self) -> None:
        self.assertIn("onclick=\"C()\"", _CALCULATOR_HTML)
        self.assertIn("onclick=\"E()\"", _CALCULATOR_HTML)

    def test_demo_runs_and_opens_calculator(self) -> None:
        runner, _fake, client = build_calculator_demo(max_actions=10)
        result = runner.run("build me a calculator app")
        self.assertTrue(result.success)
        self.assertEqual(len(client.open_urls), 1)
        self.assertTrue(client.open_urls[0].startswith("data:text/html"))


if __name__ == "__main__":
    unittest.main()
