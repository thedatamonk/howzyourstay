from pydantic import BaseModel
from typing import Optional
from datetime import datetime

class FeedbackResponse(BaseModel):
    task_id: str
    status: str
    message: str

class FeedbackStatusResponse(BaseModel):
    task_id: str
    booking_id: str
    status: str
    phone_number: Optional[str]
    duration_seconds: Optional[int]
    summary: Optional[dict]
    transcript: Optional[list]
    created_at: datetime
    completed_at: Optional[datetime]