"""Solar Hijri (Jalali) conversion helpers using the Borkowski break-year method."""
from datetime import date, timedelta
import re

BREAKS = (-61, 9, 38, 199, 426, 686, 756, 818, 1111, 1181, 1210, 1635,
          2060, 2097, 2192, 2262, 2324, 2394, 2456, 3178)
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def jalali_year_start(jy: int) -> date:
    if jy < BREAKS[0] or jy >= BREAKS[-1]:
        raise ValueError("Jalali year is outside the supported range.")
    gy = jy + 621
    leap_j = -14
    jp = BREAKS[0]
    jump = 0
    for jm in BREAKS[1:]:
        jump = jm - jp
        if jy < jm:
            break
        leap_j += (jump // 33) * 8 + ((jump % 33) // 4)
        jp = jm
    n = jy - jp
    leap_j += (n // 33) * 8 + (((n % 33) + 3) // 4)
    if jump % 33 == 4 and jump - n == 4:
        leap_j += 1
    leap_g = gy // 4 - (((gy // 100) + 1) * 3) // 4 - 150
    march = 20 + leap_j - leap_g
    return date(gy, 3, march)


def jalali_to_gregorian(jy: int, jm: int, jd: int) -> date:
    if jm < 1 or jm > 12:
        raise ValueError("Jalali month must be between 1 and 12.")
    max_day = 31 if jm <= 6 else (30 if jm <= 11 else (30 if (jalali_year_start(jy + 1) - jalali_year_start(jy)).days == 366 else 29))
    if jd < 1 or jd > max_day:
        raise ValueError("Day is outside the selected Jalali month.")
    offset = (jm - 1) * 31 + (jd - 1) if jm <= 6 else 186 + (jm - 7) * 30 + (jd - 1)
    return jalali_year_start(jy) + timedelta(days=offset)


def gregorian_to_jalali(value: date) -> tuple[int, int, int]:
    jy = value.year - 621
    start = jalali_year_start(jy)
    if value < start:
        jy -= 1
        start = jalali_year_start(jy)
    offset = (value - start).days
    if offset < 186:
        jm, jd = offset // 31 + 1, offset % 31 + 1
    else:
        offset -= 186
        jm, jd = offset // 30 + 7, offset % 30 + 1
    return jy, jm, jd


def parse_local_date(value: str, calendar: str = "gregorian") -> date:
    raw = str(value or "").strip().translate(_DIGITS)
    match = re.fullmatch(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", raw)
    if not match:
        raise ValueError("Date must use YYYY/MM/DD or YYYY-MM-DD format.")
    year, month, day = map(int, match.groups())
    if calendar == "jalali":
        if year >= 1700 and "-" in raw:
            return date(year, month, day)
        return jalali_to_gregorian(year, month, day)
    return date(year, month, day)


def format_jalali(value: date, *, persian_digits: bool = True) -> str:
    jy, jm, jd = gregorian_to_jalali(value)
    rendered = f"{jy:04d}/{jm:02d}/{jd:02d}"
    return rendered.translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")) if persian_digits else rendered
