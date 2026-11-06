from pydantic import BaseModel, ConfigDict, Field


class ItemBase(BaseModel):
    item_name: str = Field(min_length=1, max_length=200)
    category: str = Field(min_length=1, max_length=100)
    specification: str | None = Field(default=None, max_length=500)
    unit_of_measure: str = Field(min_length=1, max_length=20)


class ItemCreate(ItemBase):
    item_id: str = Field(min_length=1, max_length=20)


class ItemRead(ItemBase):
    model_config = ConfigDict(from_attributes=True)

    item_id: str
