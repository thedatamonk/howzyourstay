import uuid
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from twilio.twiml.voice_response import Connect, VoiceResponse

from caller.twilio_client import CallHandler
from db.db_session import AsyncSessionLocal, get_db, init_db
from db.models import FeedbackSession, SessionStatus
from helpers import require_env
from schemas import FeedbackResponse, FeedbackStatusResponse


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup and shutdown events"""
    # Startup
    await init_db()
    logger.info("Database initialized")
    yield
    # Shutdown (if needed)
    logger.info("Shutting down...")

app = FastAPI(title="Hostel Feedback Agent", lifespan=lifespan)

call_handler = CallHandler()

@app.post("/get_feedback", response_model=FeedbackResponse)
async def initiate_feedback_call(
    booking_id: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db)
):
    """
    Initiates a feedback call for a given booking.
    Returns immediately with a task_id for tracking.
    """
    try:
        # Generate unique task ID
        task_id = f"task_{uuid.uuid4().hex[:12]}"
        
        # TODO: Mock: Fetch booking details (in production, query your booking DB)
        booking_details = await _get_booking_details(booking_id)
        
        if not booking_details:
            raise HTTPException(status_code=404, detail="Booking not found")
        
        # Create feedback session in DB
        session = FeedbackSession(
            id=task_id,
            booking_id=booking_id,
            phone_number=booking_details["phone_number"],
            status=SessionStatus.PENDING
        )
        
        db.add(session)
        await db.commit()
        await db.refresh(session)

        # Initiate call in background
        background_tasks.add_task(
            call_handler.handle_feedback_call,
            task_id=task_id,
            booking_details=booking_details
        )

        logger.info(f"Feedback call initiated for booking {booking_id}, task_id: {task_id}")

        return FeedbackResponse(
                task_id=task_id,
                status="initiated",
                message=f"Feedback call initiated for booking {booking_id}"
            )

    except Exception as e:
        logger.error(f"Error initiating feedback call: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
    
@app.get("/get_feedback/{task_id}", response_model=FeedbackStatusResponse)
async def get_feedback_status(
    task_id: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Check the status of a feedback call by task_id.
    Returns the current status and results if completed.
    """
    try:
        result = await db.execute(
            select(FeedbackSession).where(FeedbackSession.id == task_id)
        )
        session = result.scalar_one_or_none()
        
        if not session:
            raise HTTPException(status_code=404, detail="Task not found")
        
        return FeedbackStatusResponse(
            task_id=session.id,
            booking_id=session.booking_id,
            status=session.status.value,
            phone_number=session.phone_number,
            duration_seconds=session.duration_seconds,
            summary=session.summary,
            transcript=session.transcript,
            created_at=session.created_at,
            completed_at=session.completed_at
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching feedback status: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

async def _get_booking_details(booking_id: str) -> Optional[dict]:
    """
    Mock function to fetch booking details.
    In production, this would query your booking database.
    """
    # Mock data - replace with actual DB query
    mock_bookings = {
        "BK-2024-001": {
            "booking_id": "BK-2024-001",
            "user_id": "USER-123",
            "phone_number": "+91-7905324606",
            "guest_name": "Rohil Pal",
            "check_in": "2024-01-15",
            "check_out": "2024-01-20",
            "room_number": "204",
            "hostel_name": "City Center Hostel"
        },
        "BK-2024-002": {
            "booking_id": "BK-2024-002",
            "user_id": "USER-456",
            "phone_number": "+9876543210",
            "guest_name": "Jane Smith",
            "check_in": "2024-01-18",
            "check_out": "2024-01-22",
            "room_number": "305",
            "hostel_name": "City Center Hostel"
        }
    }
    
    return mock_bookings.get(booking_id)

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "service": "hostel-feedback-system"}

#################################
#### TWILIO webhooks ############
#################################

@app.post("/twilio/voice/{task_id}")
async def twilio_voice_webhook(task_id: str, db: AsyncSession = Depends(get_db)):
    """
    Twilio webhook called when the call is answered.
    Returns TwiML instructions to connect the call to OpenAI Realtime API.
    """
    logger.info(f"Twilio voice webhook called for task {task_id}")

    try:
        # Fetch booking details from session to personalize greeting
        result = await db.execute(
            select(FeedbackSession).where(FeedbackSession.id == task_id)
        )
        session = result.scalar_one_or_none()
        if not session:
            logger.error(f'Session not found for task {task_id}')
            response = VoiceResponse()
            response.say("We're sorry, there was an error. Please try again later.")
            response.hangup()
            return Response(content=str(response), media_type="application/xml")
        
        # Get booking details
        # booking_details = await _get_booking_details(session.booking_id)

        response = VoiceResponse()
        base_url = require_env("BASE_URL").replace('https://', '').replace('http://', '')
        
        response.say(
            "Please wait while you get connected to our customer service representative",
            voice="Google.en-US-Chirp3-HD-Aoede"
        )

        response.pause(length=1)

        connect = Connect()
        connect.stream(url=f"wss://{base_url}/twilio/stream/{task_id}")
        response.append(connect)
        logger.info(f"/twilio/voice/{task_id} successfully finished.")
        return HTMLResponse(content=str(response), media_type="application/xml")

    except RuntimeError as e:
        logger.error(f"Configuration error: {e}")
        response = VoiceResponse()
        response.say("We're facing a configuration issue. We'll call you again shortly.") 
        response.hangup()
           
    except Exception as e:
        logger.error(f"Unexpected error in voice webhook: {str(e)}")
        response = VoiceResponse()
        response.say("We're sorry, there was a technical issue. Please try again later.")
        response.hangup()
    
    return Response(content=str(response), media_type="application/xml")
        
@app.post("/twilio/status/{task_id}")
async def twilio_status_callback(task_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    """
    Twilio status callback
    Whenever status of the call changes, Twilio calls this endpoint to update the status
    Twilio will automatically POST to this endpoint at various points in the call lifecycle

    1. **`queued`** - Call is queued to be made
    2. **`ringing`** - Phone is ringing on the user's end
    3. **`in-progress`** - User answered the call
    4. **`completed`** - Call ended normally
    5. **`busy`** - User's line was busy
    6. **`failed`** - Call failed (network issues, invalid number, etc.)
    7. **`no-answer`** - User didn't pick up
    """
    form_data = await request.form()
    call_status = form_data.get("CallStatus")
    call_duration = form_data.get("CallDuration", "0")

    logger.info(f"Call Status update for task {task_id}: {call_status}, duration: {call_duration}s")

    try:
        result = await db.execute(
            select(FeedbackSession).where(FeedbackSession.id == task_id)
        )

        session = result.scalar_one_or_none()

        if not session:
            logger.warning(f"Session not found for status callback: {task_id}")
            return {"status": "ok"}
        
        if call_status == "completed":
            # Call completed successfuly - if not already marked as completed by stream
            if session.status == SessionStatus.IN_PROGRESS:
                session.status == SessionStatus.COMPLETED
                session.duration_seconds = int(call_duration)
                await db.commit()
        elif call_status in ["failed", "busy", "no-answer"]:
            # Call failed - marked as failed
            session.status = SessionStatus.FAILED
            await db.commit()
            logger.error(f"Call failed for task {task_id}: {call_status}")
        

        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Error in status callback for task {task_id}: {str(e)}")
        return {"status": "error", "message": str(e)}
        

@app.websocket("/twilio/stream/{task_id}")
async def handle_media_stream(websocket: WebSocket, task_id: str):
    """
    Websocket endpoint that bridges Twilio <-> OpenAI RealtimeAPI.
    This is where the actual audio conversation happens.
    """

    await websocket.accept()
    logger.info(f"Websocket connection established for task {task_id}.")


    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(FeedbackSession).where(FeedbackSession.id == task_id)
            )

            session = result.scalar_one_or_none()

            if not session:
                logger.error(f"Session not found: {task_id}.")
                await websocket.close(code=1008, reason="Session not found")
                return
            
            logger.info(f"Successfully fetched session with task id: {session.id} and booking id: {session.booking_id}")
            # Get booking details for conversation context
            booking_details = await _get_booking_details(session.booking_id)

            logger.info("Successfully fetched booking details.")

            # Call the stream handler from CallHander
            await call_handler.handle_media_stream(
                websocket=websocket,
                task_id=task_id,
                booking_details=booking_details
            )

            logger.info(f"/twilio/stream/{task_id} successfully finished.")

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for task {task_id}")
    except Exception as e:
        logger.error(f"Error in media stream for task {task_id}: {str(e)}")
        await websocket.close(code=1011, reason="Internal error")