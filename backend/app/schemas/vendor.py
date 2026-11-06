from pydantic import BaseModel, ConfigDict, Field


class VendorBase(BaseModel):
    vendor_name: str = Field(min_length=1, max_length=200)
    gst_number: str | None = Field(default=None, max_length=20)
    category: str = Field(min_length=1, max_length=100)
    oem_approved: bool = False
    performance_score: float | None = None
    quality_rating: float | None = None
    on_time_delivery_rate: float | None = None
    payment_terms: str | None = Field(default=None, max_length=50)
    is_active: bool = True


class VendorCreate(VendorBase):
    vendor_id: str = Field(min_length=1, max_length=20)


class VendorRead(VendorBase):
    model_config = ConfigDict(from_attributes=True)

    vendor_id: str
