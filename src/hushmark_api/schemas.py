from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


class ReviewedAudioDetectionV1(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str = Field(pattern=r"^det_[1-9]\d*$")
    status: Literal[ReviewStatus.APPROVED, ReviewStatus.REJECTED]
    audio_start_ms: int = Field(alias="audioStartMs", strict=True, ge=0)
    audio_end_ms: int = Field(alias="audioEndMs", strict=True, gt=0)

    @model_validator(mode="after")
    def valid_interval(self) -> "ReviewedAudioDetectionV1":
        if self.audio_end_ms <= self.audio_start_ms:
            raise ValueError("audio interval must have positive duration")
        return self


class RedactAudioReviewV1(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    analysis_id: str = Field(alias="analysisId", pattern=r"^ana_[0-9a-f]{32}$")
    detections: list[ReviewedAudioDetectionV1]

    @model_validator(mode="after")
    def sequential_detection_ids(self) -> "RedactAudioReviewV1":
        if any(
            detection.id != f"det_{index}"
            for index, detection in enumerate(self.detections, start=1)
        ):
            raise ValueError("detection IDs must be complete and ordered")
        return self


class ErrorResponseV1(BaseModel):
    code: str
    message: str
