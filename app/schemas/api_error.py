from pydantic import BaseModel


class ErrorField(BaseModel):
    loc: list[str | int]
    type: str


class ErrorDetail(BaseModel):
    code: str
    message: str | None = None
    fields: list[ErrorField] | None = None


class APIError(BaseModel):
    detail: str | ErrorDetail
