from sqlalchemy import Column, String, Integer, DateTime, Enum as SQLEnum, JSON
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.sql import func
import enum

Base = declarative_base()


class SessionStatus(str, enum.Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class FeedbackSession(Base):
    __tablename__ = "feedback_sessions"
    
    id = Column(String, primary_key=True, index=True)  # task_id
    booking_id = Column(String, nullable=False, index=True)
    phone_number = Column(String, nullable=False)
    call_sid = Column(String, nullable=True)  # Twilio call SID
    
    status = Column(SQLEnum(SessionStatus), default=SessionStatus.PENDING, nullable=False)
    
    # Store full conversation transcript as JSON array
    # Format: [{"role": "agent", "content": "..."}, {"role": "user", "content": "..."}]
    transcript = Column(JSON, nullable=True)
    
    # Store structured summary as JSON
    # Format: {"overview": "...", "painpoints": [], "highlights": [], "recommendations": []}
    summary = Column(JSON, nullable=True)
    
    duration_seconds = Column(Integer, nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    
    def __repr__(self):
        return f"<FeedbackSession(id={self.id}, booking_id={self.booking_id}, status={self.status})>"