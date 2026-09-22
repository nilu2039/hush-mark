from hushmark_api.recognizers import DetectionCandidate
from hushmark_api.schemas import DetectionSource, EntityType

_SOURCE_PRIORITY = {
    DetectionSource.REGEX: 2,
    DetectionSource.PRESIDIO: 1,
    DetectionSource.MANUAL: 3,
}
_TYPE_PRIORITY = {
    EntityType.AADHAAR: 3,
    EntityType.PAN: 3,
    EntityType.PAYMENT_CARD: 3,
    EntityType.EMAIL: 2,
    EntityType.PHONE: 2,
    EntityType.IP_ADDRESS: 2,
    EntityType.DATE_OF_BIRTH: 2,
    EntityType.BANK_ACCOUNT: 2,
    EntityType.PERSON: 1,
    EntityType.ADDRESS: 1,
}


def resolve_overlaps(
    candidates: list[DetectionCandidate],
) -> list[DetectionCandidate]:
    ranked = sorted(
        candidates,
        key=lambda candidate: (
            -_SOURCE_PRIORITY[candidate.source],
            -_TYPE_PRIORITY[candidate.entity_type],
            -(candidate.end - candidate.start),
            candidate.start,
            candidate.entity_type,
        ),
    )
    accepted: list[DetectionCandidate] = []
    for candidate in ranked:
        if all(
            candidate.end <= existing.start or candidate.start >= existing.end
            for existing in accepted
        ):
            accepted.append(candidate)
    return sorted(accepted, key=lambda candidate: (candidate.start, candidate.end))
