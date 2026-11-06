from pydantic import BaseModel, ConfigDict, Field

from app.core.scales import (
    ON_TIME_RATE_MAX,
    ON_TIME_RATE_MIN,
    PERFORMANCE_SCORE_MAX,
    PERFORMANCE_SCORE_MIN,
    QUALITY_RATING_MAX,
    QUALITY_RATING_MIN,
)


class VendorBase(BaseModel):
    vendor_name: str = Field(min_length=1, max_length=200)
    gst_number: str | None = Field(default=None, max_length=20)
    category: str = Field(min_length=1, max_length=100)
    oem_approved: bool = False
    performance_score: float | None = Field(
        default=None, ge=PERFORMANCE_SCORE_MIN, le=PERFORMANCE_SCORE_MAX
    )
    quality_rating: float | None = Field(
        default=None, ge=QUALITY_RATING_MIN, le=QUALITY_RATING_MAX
    )
    on_time_delivery_rate: float | None = Field(
        default=None, ge=ON_TIME_RATE_MIN, le=ON_TIME_RATE_MAX
    )
    payment_terms: str | None = Field(default=None, max_length=50)
    is_active: bool = True


class VendorCreate(VendorBase):
    vendor_id: str = Field(min_length=1, max_length=20)


class VendorRead(VendorBase):
    model_config = ConfigDict(from_attributes=True)

    vendor_id: str
