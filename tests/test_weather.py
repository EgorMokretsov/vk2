"""Проверки вычислений и поведения CLI; реальные HTTP-запросы не нужны."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import weather


def payload(temperature="18", country="Japan"):
    return {
        "current_condition": [{"temp_C": temperature, "FeelsLikeC": "99"}],
        "nearest_area": [{"country": [{"value": country}]}],
        "weather": [{"avgtempC": "77"}],
    }


def response(body):
    result = MagicMock()
    result.__enter__.return_value.read.return_value = body
    return result


class LoadCitiesTests(unittest.TestCase):
    def test_deduplicates_in_one_file_and_preserves_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cities.txt"
            path.write_text("\ufeff Moscow \n\nTokyo\nMOSCOW\nTokyo\nVienna\n",
                            encoding="utf-8")
            self.assertEqual(weather.load_cities(path),
                             ["Moscow", "Tokyo", "Vienna"])

    def test_supplied_file_has_eight_unique_cities(self):
        self.assertEqual(weather.load_cities(weather.DEFAULT_FILE), [
            "Moscow", "Khabarovsk", "Saint-Petersburg", "Vienna",
            "Izhevsk", "Perm", "NhaTrang", "Villach",
        ])

    def test_empty_file_is_an_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "empty.txt"
            path.write_text(" \n\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                weather.load_cities(path)

    def test_missing_file_is_an_error(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FileNotFoundError):
                weather.load_cities(Path(directory) / "missing.txt")


class ParseTests(unittest.TestCase):
    def test_uses_current_celsius_not_forecast_or_feels_like(self):
        self.assertEqual(weather.parse_weather("Tokyo", payload()),
                         weather.WeatherData("Tokyo", 18, "Japan"))

    def test_negative_temperature_and_country_whitespace(self):
        self.assertEqual(weather.parse_weather("Moscow", payload("-12", " Russia ")),
                         weather.WeatherData("Moscow", -12, "Russia"))

    def test_integer_zero_is_valid(self):
        self.assertEqual(weather.parse_weather("Tokyo", payload(0)).temperature_c, 0)

    def test_missing_or_malformed_fields_are_errors(self):
        invalid = [None, [], {}, {"current_condition": []},
                   payload("abc"), payload("18.5"), payload(True), payload(1.5),
                   payload(None), payload(country=""), payload(country="  "),
                   payload(country=None), payload(country=123)]
        for item in invalid:
            with self.subTest(payload=item), self.assertRaises(weather.WeatherError):
                weather.parse_weather("Tokyo", item)


class FetchTests(unittest.TestCase):
    @patch("weather.urlopen")
    def test_request_url_encoding_and_timeout(self, opener):
        opener.return_value = response(json.dumps(payload()).encode("utf-8"))
        self.assertEqual(weather.fetch_weather("Новый город", timeout=7, retries=0),
                         weather.WeatherData("Новый город", 18, "Japan"))
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url,
                         "https://wttr.in/%D0%9D%D0%BE%D0%B2%D1%8B%D0%B9%20"
                         "%D0%B3%D0%BE%D1%80%D0%BE%D0%B4?format=j1")
        self.assertEqual(opener.call_args.kwargs["timeout"], 7)

    @patch("weather.time.sleep")
    @patch("weather.urlopen")
    def test_recovers_from_network_error(self, opener, sleep):
        opener.side_effect = [URLError("temporary"),
                              response(json.dumps(payload()).encode())]
        self.assertEqual(weather.fetch_weather("Tokyo", retries=1).temperature_c, 18)
        self.assertEqual(opener.call_count, 2)
        sleep.assert_called_once_with(1)

    @patch("weather.time.sleep")
    @patch("weather.urlopen")
    def test_retries_transient_http_statuses(self, opener, sleep):
        for code in (408, 429, 500, 503):
            with self.subTest(code=code):
                opener.reset_mock()
                opener.side_effect = [HTTPError("url", code, "temporary", {}, None),
                                      response(json.dumps(payload()).encode())]
                self.assertEqual(weather.fetch_weather("Tokyo", retries=1).country,
                                 "Japan")
                self.assertEqual(opener.call_count, 2)

    @patch("weather.time.sleep")
    @patch("weather.urlopen")
    def test_does_not_retry_http_404(self, opener, sleep):
        opener.side_effect = HTTPError("url", 404, "missing", {}, None)
        with self.assertRaisesRegex(weather.WeatherError, "HTTP 404"):
            weather.fetch_weather("Tokyo")
        self.assertEqual(opener.call_count, 1)
        sleep.assert_not_called()

    @patch("weather.time.sleep")
    @patch("weather.urlopen")
    def test_retry_count_is_bounded(self, opener, sleep):
        opener.side_effect = TimeoutError("timeout")
        with self.assertRaises(weather.WeatherError):
            weather.fetch_weather("Tokyo", retries=2)
        self.assertEqual(opener.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    @patch("weather.urlopen")
    def test_non_json_response_is_an_error(self, opener):
        opener.return_value = response(b"<html>Unavailable</html>")
        with self.assertRaisesRegex(weather.WeatherError, "JSON"):
            weather.fetch_weather("Tokyo")
        self.assertEqual(opener.call_count, 1)

    @patch("weather.urlopen")
    def test_non_utf8_response_is_an_error(self, opener):
        opener.return_value = response(b"\xff")
        with self.assertRaisesRegex(weather.WeatherError, "UTF-8"):
            weather.fetch_weather("Tokyo")

    def test_invalid_connection_options(self):
        for timeout, retries in ((0, 0), (-1, 0), (float("nan"), 0),
                                 (float("inf"), 0), (1, -1)):
            with self.subTest(timeout=timeout, retries=retries):
                with self.assertRaises(ValueError):
                    weather.fetch_weather("Tokyo", timeout, retries)


class ReportTests(unittest.TestCase):
    def test_country_statistics_are_computed(self):
        result = weather.group_by_country([
            weather.WeatherData("Tokyo", 18, "Japan"),
            weather.WeatherData("Moscow", -5, "Russia"),
            weather.WeatherData("Osaka", 21, "Japan"),
            weather.WeatherData("Kyoto", 19, "Japan"),
        ])
        self.assertEqual([item.country for item in result], ["Japan", "Russia"])
        self.assertEqual(result[0].city_count, 3)
        self.assertAlmostEqual(result[0].average_c, 58 / 3)
        self.assertEqual((result[0].minimum_c, result[0].maximum_c), (18, 21))
        self.assertEqual(result[1], weather.CountryStatistics("Russia", 1, -5, -5, -5))

    def test_empty_grouping(self):
        self.assertEqual(weather.group_by_country([]), [])

    def test_temperature_format(self):
        for value, expected in [(18, "+18 °C"), (-5, "-5 °C"), (0, "+0 °C"),
                                (19.33333, "+19.33 °C"), (12.5, "+12.5 °C"),
                                (-0.001, "+0 °C")]:
            with self.subTest(value=value):
                self.assertEqual(weather.format_temperature(value), expected)

    def test_output_has_city_and_country_statistics(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            weather.print_report([weather.WeatherData("Tokyo", 18, "Japan")])
        self.assertIn("Tokyo, Japan +18 °C", output.getvalue())
        self.assertIn("Japan - 1 city, avg: +18 °C, min: +18 °C, max: +18 °C",
                      output.getvalue())


class CliTests(unittest.TestCase):
    def run_main(self, arguments):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = weather.main(arguments)
        return code, output.getvalue(), errors.getvalue()

    @patch("weather.fetch_weather")
    def test_default_file_requests_each_unique_city_once(self, fetch):
        fetch.side_effect = lambda city, *_: weather.WeatherData(city, 10, "Testland")
        code, output, _ = self.run_main([])
        self.assertEqual(code, 0)
        self.assertEqual(fetch.call_count, 8)
        self.assertEqual([call.args[0] for call in fetch.call_args_list],
                         weather.load_cities(weather.DEFAULT_FILE))
        self.assertIn("Testland - 8 cities, avg: +10 °C", output)

    @patch("weather.load_cities", return_value=["Tokyo", "Moscow"])
    @patch("weather.fetch_weather")
    def test_partial_failure_does_not_count_missing_city(self, fetch, load):
        fetch.side_effect = [weather.WeatherError("timeout"),
                             weather.WeatherData("Moscow", -3, "Russia")]
        code, output, errors = self.run_main(["custom.txt", "--retries", "0"])
        self.assertEqual(code, 1)
        self.assertIn("Russia - 1 city, avg: -3 °C", output)
        self.assertNotIn("Tokyo,", output)
        self.assertIn("1 из 2", errors)
        load.assert_called_once_with(Path("custom.txt"))

    @patch("weather.fetch_weather")
    def test_two_input_files_are_rejected_before_http(self, fetch):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as result:
                weather.main(["first.txt", "second.txt"])
        self.assertEqual(result.exception.code, 2)
        fetch.assert_not_called()

    @patch("weather.load_cities", return_value=["Tokyo"])
    @patch("weather.fetch_weather", side_effect=weather.WeatherError("offline"))
    def test_all_requests_fail(self, fetch, load):
        code, output, errors = self.run_main([])
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("1 из 1", errors)

    @patch("weather.fetch_weather")
    def test_missing_input_fails_before_http(self, fetch):
        with tempfile.TemporaryDirectory() as directory:
            code, _, errors = self.run_main([str(Path(directory) / "missing.txt")])
        self.assertEqual(code, 1)
        self.assertIn("Ошибка чтения", errors)
        fetch.assert_not_called()

    def test_invalid_cli_options(self):
        for arguments in (["--timeout", "0"], ["--timeout", "nan"],
                          ["--timeout", "inf"], ["--retries", "-1"]):
            with self.subTest(arguments=arguments):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as result:
                        weather.main(arguments)
                self.assertEqual(result.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
