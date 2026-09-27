from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

MAX_TEXT_LENGTH = 50_000


class EntityType(StrEnum):
    PERSON = "PERSON"
    EMAIL = "EMAIL"
    PHONE = "PHONE"
    ADDRESS = "ADDRESS"
    DATE_OF_BIRTH = "DATE_OF_BIRTH"
    IP_ADDRESS = "IP_ADDRESS"
    AADHAAR = "AADHAAR"
    PAN = "PAN"
    BANK_ACCOUNT = "BANK_ACCOUNT"
    PAYMENT_CARD = "PAYMENT_CARD"


class DetectionSource(StrEnum):
    REGEX = "regex"
    PRESIDIO = "presidio"
    OPENAI = "openai"
    MANUAL = "manual"


class ReviewStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


def _valid_review_ids(ids: list[str]) -> bool:
    automatic = {identifier for identifier in ids if identifier.startswith("det_")}
    return len(ids) == len(set(ids)) and automatic == {
        f"det_{index}" for index in range(1, len(automatic) + 1)
    }


class AnalyzeRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=MAX_TEXT_LENGTH, strict=True)
    locale: Literal["en-IN"] = "en-IN"

    @field_validator("text")
    @classmethod
    def text_must_have_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must contain non-whitespace characters")
        return value


class DetectionV1(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    entity_type: EntityType = Field(alias="type")
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    confidence: float = Field(ge=0, le=1)
    source: DetectionSource
    status: ReviewStatus = ReviewStatus.PENDING


class AnalyzeResponseV1(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    analysis_id: str = Field(alias="analysisId")
    text_length: int = Field(alias="textLength", ge=1)
    detections: list[DetectionV1]


class AudioDetectionV1(DetectionV1):
    audio_start_ms: int = Field(alias="audioStartMs", ge=0)
    audio_end_ms: int = Field(alias="audioEndMs", gt=0)


class AnalyzeAudioResponseV1(AnalyzeResponseV1):
    transcript: str = Field(min_length=1, max_length=MAX_TEXT_LENGTH)
    detections: list[AudioDetectionV1]


class AnalyzeDocumentResponseV1(AnalyzeResponseV1):
    text: str = Field(min_length=1, max_length=MAX_TEXT_LENGTH)


class ReviewedDocumentDetectionV1(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str = Field(pattern=r"^(?:det|man)_[1-9]\d*$")
    entity_type: EntityType = Field(alias="type")
    start: int = Field(strict=True, ge=0)
    end: int = Field(strict=True, gt=0)
    status: Literal[ReviewStatus.APPROVED, ReviewStatus.REJECTED]

    @model_validator(mode="after")
    def valid_interval(self) -> "ReviewedDocumentDetectionV1":
        if self.end <= self.start:
            raise ValueError("text interval must have positive length")
        return self


class RedactDocumentReviewV1(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    analysis_id: str = Field(alias="analysisId", pattern=r"^ana_[0-9a-f]{32}$")
    detections: list[ReviewedDocumentDetectionV1]

    @model_validator(mode="after")
    def valid_detections(self) -> "RedactDocumentReviewV1":
        if not _valid_review_ids([detection.id for detection in self.detections]):
            raise ValueError("detection IDs must be unique and automatic IDs complete")
        previous_end = 0
        approved = sorted(
            (
                detection
                for detection in self.detections
                if detection.status == ReviewStatus.APPROVED
            ),
            key=lambda detection: detection.start,
        )
        for detection in approved:
            if detection.start < previous_end:
                raise ValueError("approved detection spans must not overlap")
            previous_end = detection.end
        return self


class RedactTextRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=MAX_TEXT_LENGTH, strict=True)
    review: RedactDocumentReviewV1

    @field_validator("text")
    @classmethod
    def text_must_have_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must contain non-whitespace characters")
        return value


class ReviewedAudioDetectionV1(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str = Field(pattern=r"^(?:det|man)_[1-9]\d*$")
    entity_type: EntityType | None = Field(default=None, alias="type")
    status: Literal[ReviewStatus.APPROVED, ReviewStatus.REJECTED]
    audio_start_ms: int = Field(alias="audioStartMs", strict=True, ge=0)
    audio_end_ms: int = Field(alias="audioEndMs", strict=True, gt=0)

    @model_validator(mode="after")
    def valid_interval(self) -> "ReviewedAudioDetectionV1":
        if self.audio_end_ms <= self.audio_start_ms:
            raise ValueError("audio interval must have positive duration")
        if self.id.startswith("man_") and self.entity_type is None:
            raise PydanticCustomError(
                "manual_type_required", "manual detection type is required"
            )
        return self


class RedactAudioReviewV1(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    analysis_id: str = Field(alias="analysisId", pattern=r"^ana_[0-9a-f]{32}$")
    detections: list[ReviewedAudioDetectionV1]

    @model_validator(mode="after")
    def valid_detection_ids(self) -> "RedactAudioReviewV1":
        if not _valid_review_ids([detection.id for detection in self.detections]):
            raise ValueError("detection IDs must be unique and automatic IDs complete")
        return self


class ErrorResponseV1(BaseModel):
    code: str
    message: str
