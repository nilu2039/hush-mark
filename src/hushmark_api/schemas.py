from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

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


class ErrorResponseV1(BaseModel):
    code: str
    message: str
