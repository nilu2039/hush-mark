import ipaddress
import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache

from presidio_analyzer import AnalyzerEngine

from hushmark_api.schemas import DetectionSource, EntityType


@dataclass(frozen=True, slots=True)
class DetectionCandidate:
    entity_type: EntityType
    start: int
    end: int
    confidence: float
    source: DetectionSource


_EMAIL = re.compile(
    r"(?<![\w.+-])[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}"
)
_PHONE = re.compile(r"(?<!\d)(?:(?:\+91|0)[ -]?)?[6-9](?:[ -]?\d){9}(?!\d)")
_DIGIT_WORD = r"(?:zero|one|two|three|four|five|six|seven|eight|nine)"
_SPOKEN_PHONE = re.compile(
    rf"(?<!\w)(?:(?:plus[ -]+)?nine[ -]+one[ -]+)?"
    rf"(?:six|seven|eight|nine)(?:[ -]+{_DIGIT_WORD}){{9}}"
    rf"(?![ -]+{_DIGIT_WORD})(?!\w)",
    re.I,
)
_PAN = re.compile(r"(?<![A-Z0-9])[A-Z]{5}\d{4}[A-Z](?![A-Z0-9])", re.I)
_AADHAAR = re.compile(r"(?<!\d)[2-9]\d{3}(?:[ -]?\d{4}){2}(?!\d)")
_PAYMENT_CARD = re.compile(r"(?<!\d)\d(?:[ -]?\d){12,18}(?!\d)")
_IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?!\w|\.\d)")
_IPV6 = re.compile(
    r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}"
    r"(?![0-9A-Fa-f:])"
)

_VERHOEFF_MULTIPLICATION = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8),
    (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2),
    (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
_VERHOEFF_PERMUTATION = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0),
    (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5),
    (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)


def _digits(value: str) -> str:
    return "".join(character for character in value if character.isdigit())


def _valid_aadhaar(value: str) -> bool:
    checksum = 0
    for index, digit in enumerate(reversed(_digits(value))):
        checksum = _VERHOEFF_MULTIPLICATION[checksum][
            _VERHOEFF_PERMUTATION[index % 8][int(digit)]
        ]
    return checksum == 0


def _valid_payment_card(value: str) -> bool:
    digits = _digits(value)
    if len(set(digits)) == 1:
        return False
    total = 0
    parity = len(digits) % 2
    for index, digit in enumerate(map(int, digits)):
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _valid_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def _regex_candidates(
    text: str,
    pattern: re.Pattern[str],
    entity_type: EntityType,
    confidence: float,
    validator: Callable[[str], bool] | None = None,
) -> list[DetectionCandidate]:
    matches = pattern.finditer(text)
    if validator is not None:
        matches = (match for match in matches if validator(match.group()))
    return [
        DetectionCandidate(
            entity_type=entity_type,
            start=match.start(),
            end=match.end(),
            confidence=confidence,
            source=DetectionSource.REGEX,
        )
        for match in matches
    ]


def detect_structured_pii(text: str) -> list[DetectionCandidate]:
    candidates: list[DetectionCandidate] = []
    candidates.extend(_regex_candidates(text, _EMAIL, EntityType.EMAIL, 0.99))
    candidates.extend(_regex_candidates(text, _PHONE, EntityType.PHONE, 0.95))
    candidates.extend(
        _regex_candidates(text, _SPOKEN_PHONE, EntityType.PHONE, 0.95)
    )
    candidates.extend(_regex_candidates(text, _PAN, EntityType.PAN, 0.95))
    candidates.extend(
        _regex_candidates(text, _AADHAAR, EntityType.AADHAAR, 1.0, _valid_aadhaar)
    )
    candidates.extend(
        _regex_candidates(
            text,
            _PAYMENT_CARD,
            EntityType.PAYMENT_CARD,
            1.0,
            _valid_payment_card,
        )
    )
    for pattern in (_IPV4, _IPV6):
        candidates.extend(
            _regex_candidates(
                text,
                pattern,
                EntityType.IP_ADDRESS,
                1.0,
                _valid_ip,
            )
        )
    return candidates


@cache
def _context_analyzer() -> AnalyzerEngine:
    # ponytail: a cold-start race may load twice; use app lifespan if observed.
    return AnalyzerEngine()


def detect_contextual_pii(text: str, locale: str) -> list[DetectionCandidate]:
    language = {"en-IN": "en"}[locale]
    entity_map = {
        "PERSON": EntityType.PERSON,
        "LOCATION": EntityType.ADDRESS,
    }
    return [
        DetectionCandidate(
            entity_type=entity_map[result.entity_type],
            start=result.start,
            end=result.end,
            confidence=result.score,
            source=DetectionSource.PRESIDIO,
        )
        for result in _context_analyzer().analyze(
            text=text,
            language=language,
            entities=list(entity_map),
            score_threshold=0.5,
        )
    ]
