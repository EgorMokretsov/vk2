"""Текущая погода по городам и статистика по странам, без внешних пакетов."""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_FILE = BASE_DIR / "Cities.txt"


@dataclass(frozen=True)
class WeatherData:
    """Город из входного файла, текущая температура (°C), страна из API."""

    city: str
    temperature_c: int
    country: str


@dataclass(frozen=True)
class CountryStatistics:
    country: str
    city_count: int
    average_c: float
    minimum_c: int
    maximum_c: int


class WeatherError(Exception):
    """Сервис недоступен или прислал некорректные данные."""


def load_cities(path: Path) -> list[str]:
    """Прочитать UTF-8/UTF-8 BOM, убрать пустые строки и повторы."""
    cities: list[str] = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        city = line.strip()
        key = city.casefold()
        if city and key not in seen:
            seen.add(key)
            cities.append(city)
    if not cities:
        raise ValueError("В файле нет городов.")
    return cities


def parse_weather(city: str, payload: Any) -> WeatherData:
    """Использовать именно temp_C текущих условий, а не прогноз/FeelsLikeC."""
    try:
        temperature = payload["current_condition"][0]["temp_C"]
        country = payload["nearest_area"][0]["country"][0]["value"]
        if isinstance(temperature, bool) or not isinstance(temperature, (str, int)):
            raise ValueError("temp_C должен содержать целое число")
        temperature_c = int(temperature)
        if not isinstance(country, str) or not country.strip():
            raise ValueError("страна отсутствует")
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise WeatherError(f"Некорректный ответ API для {city}: {exc}") from exc
    return WeatherData(city, temperature_c, country.strip())


def fetch_weather(city: str, timeout: float = 20.0, retries: int = 2) -> WeatherData:
    """Повторить запрос при временном сбое, сохраняя проверку HTTPS."""
    if not math.isfinite(timeout) or timeout <= 0 or retries < 0:
        raise ValueError("timeout должен быть положительным, retries — неотрицательным")
    url = f"https://wttr.in/{quote(city, safe='')}?format=j1"
    request = Request(url, headers={
        "Accept": "application/json",
        "Accept-Language": "en",
        "User-Agent": "VK2-Weather/1.0",
    })
    for attempt in range(retries + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            retryable = exc.code in (408, 429) or 500 <= exc.code < 600
            if not retryable or attempt == retries:
                raise WeatherError(f"{city}: HTTP {exc.code}") from exc
        except (URLError, OSError) as exc:
            if attempt == retries:
                raise WeatherError(f"{city}: ошибка подключения ({exc})") from exc
        except UnicodeError as exc:
            raise WeatherError(f"{city}: ответ API не в UTF-8") from exc
        else:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise WeatherError(f"{city}: API вернул некорректный JSON") from exc
            return parse_weather(city, payload)
        time.sleep(attempt + 1)
    raise AssertionError("Недостижимая ветка")


def group_by_country(weather: Sequence[WeatherData]) -> list[CountryStatistics]:
    groups: dict[str, list[int]] = defaultdict(list)
    for item in weather:
        groups[item.country].append(item.temperature_c)
    return [
        CountryStatistics(country, len(values), sum(values) / len(values),
                          min(values), max(values))
        for country, values in sorted(groups.items())
    ]


def format_temperature(value: float) -> str:
    """Показать знак и до двух знаков после запятой без лишних нулей."""
    rounded = round(value, 2)
    if rounded == 0:
        rounded = 0.0
    return f"{rounded:+.2f}".rstrip("0").rstrip(".") + " °C"


def print_report(weather: Sequence[WeatherData]) -> None:
    print("Погода по городам:")
    for item in weather:
        print(f"{item.city}, {item.country} {format_temperature(item.temperature_c)}")
    print("\nСтатистика по странам:")
    for item in group_by_country(weather):
        unit = "city" if item.city_count == 1 else "cities"
        print(f"{item.country} - {item.city_count} {unit}, "
              f"avg: {format_temperature(item.average_c)}, "
              f"min: {format_temperature(item.minimum_c)}, "
              f"max: {format_temperature(item.maximum_c)}")


def positive_timeout(value: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("нужно конечное число больше нуля")
    return result


def nonnegative_integer(value: str) -> int:
    result = int(value)
    if result < 0:
        raise argparse.ArgumentTypeError("нужно целое число не меньше нуля")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", nargs="?", type=Path, default=DEFAULT_FILE,
                        help="файл городов; по умолчанию Cities.txt рядом со скриптом")
    parser.add_argument("--timeout", type=positive_timeout, default=20.0,
                        help="тайм-аут запроса в секундах (по умолчанию 20)")
    parser.add_argument("--retries", type=nonnegative_integer, default=2,
                        help="число повторов при временном сбое (по умолчанию 2)")
    args = parser.parse_args(argv)
    try:
        cities = load_cities(args.file)
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"Ошибка чтения списка городов: {exc}", file=sys.stderr)
        return 1

    weather: list[WeatherData] = []
    failed = 0
    for index, city in enumerate(cities, start=1):
        print(f"[{index}/{len(cities)}] Получение погоды: {city}", file=sys.stderr)
        try:
            weather.append(fetch_weather(city, args.timeout, args.retries))
        except WeatherError as exc:
            failed += 1
            print(f"Ошибка: {exc}", file=sys.stderr)
    if weather:
        print_report(weather)
    if failed:
        print(f"Не получены данные для {failed} из {len(cities)} городов. "
              "Статистика учитывает только успешные запросы.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    # UTF-8 для русских сообщений и символа ° при перенаправлении в Windows.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nВыполнение прервано пользователем.", file=sys.stderr)
        sys.exit(130)
