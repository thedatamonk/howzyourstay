import asyncio
import json
from typing import Dict, List
import base64

import websockets
from loguru import logger
from openai import AsyncOpenAI
from sqlalchemy import select
from twilio.rest import Client
from fastapi import WebSocket, WebSocketDisconnect
from db.db_session import AsyncSessionLocal
from db.models import FeedbackSession, SessionStatus
from helpers import require_env

from datetime import datetime, timezone

VOICE = "alloy"

"""
conversation.item.input_audio_transcription.completed: this event is the output of audio transcription for user audio written to the user audio buffer. 
Transcription begins when the input audio buffer is committed by the client or server (when VAD is enabled).

response.output_audio_transcript.done: this event is generated when the audio transcription for the agent generated response is completed.

response.function_call_arguments.delta: this event is generated when a function call needs to be triggered.

"""
LOG_EVENT_TYPES = [
    'error', 'response.content.done', 'rate_limits.updated',
    'response.done', 'input_audio_buffer.committed',
    'input_audio_buffer.speech_stopped', 'input_audio_buffer.speech_started',
    'session.created', 'session.updated', 'conversation.item.input_audio_transcription.completed', 
    'response.output_audio_transcript.done', 'response.function_call_arguments.done', 'response.output_audio.done'
]
SHOW_TIMING_MATH = False

class CallHandler:
    """
    Singleton Class that handles Twilio Voice calls and OpenAI Realtime API integration
    """
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "_initialized"):
            return
        
        self.twilio_client = Client(
            require_env("TWILIO_ACCOUNT_SID"),
            require_env("TWILIO_AUTH_TOKEN")
        )
        self.openai_client = AsyncOpenAI(api_key=require_env("OPENAI_API_KEY"))
        self.twilio_phone = require_env("TWILIO_PHONE_NUMBER")

        # Load guidelines
        self.guidelines = self._load_guidelines()
        
        # Max conversation duration (5 minutes)
        self.max_duration_in_secs = 300

        self._initialized = True

    def _load_guidelines(self) -> str:
        """Load feedback guidelines from file"""
        try:
            with open("guidelines/guidelines_samarya.txt", "r") as f:
                return f.read()
        except FileNotFoundError:
            logger.warning("Guidelines file not found, using default guidelines")
            return """
            Collect feedback on the following areas:
            1. Cleanliness of rooms and common areas
            2. Staff friendliness and helpfulness
            3. Amenities (WiFi, kitchen, lockers, etc.)
            4. Location and accessibility
            5. Noise levels and comfort
            6. Value for money
            7. Any specific incidents or concerns
            8. Overall satisfaction and likelihood to recommend
            """
        
    
    async def handle_feedback_call(self, task_id: str, booking_details: Dict):
        """
        Initiates a Twilio Call. The actual conversation is handled by the WebSocket Stream endpoint

        This method only:
        1. Updates DB status to IN_PROGRESS
        2. Initiates the Twilio Call
        3. Stores the call SID
        """

        async with AsyncSessionLocal() as db:
            try:
                base_url = require_env("BASE_URL")

                # Update status to in_progress
                result = await db.execute(
                    select(FeedbackSession).where(FeedbackSession.id == task_id)
                )
                session = result.scalar_one()
                session.status = SessionStatus.IN_PROGRESS
                await db.commit()
                
                logger.info(f"Starting feedback call for task {task_id}")
                
                # Initiate Twilio call
                # When user answers, Twilio will hit /twilio/voice/{task_id}
                # That webhook returns TwiML with Stream pointing to /twilio/stream/{task_id}
                call = self.twilio_client.calls.create(
                    to=booking_details["phone_number"],
                    from_=self.twilio_phone,
                    url=f"{base_url}/twilio/voice/{task_id}",
                    status_callback=f"{base_url}/twilio/status/{task_id}",
                    timeout=30      # Ring for 30 seconds before giving up
                )
                
                session.call_sid = call.sid
                await db.commit()
                
                logger.info(f"Twilio call initiated with SID: {call.sid}")

                # Note: We don't wait for the call to complete here
                # The WebSocket handler will manage the actual conversation
                # And update the DB when done

            except Exception as e:
                logger.error(f"Error initiating Twilio call for task {task_id}: {str(e)}")
                
                # Update status to failed
                result = await db.execute(
                    select(FeedbackSession).where(FeedbackSession.id == task_id)
                )
                session = result.scalar_one_or_none()
                if session:
                    session.status = SessionStatus.FAILED
                    await db.commit()

    async def initialize_session(self, openai_ws, system_prompt):
        """Control initial session with OpenAI"""
        session_update = {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "model": "gpt-realtime",
                "output_modalities": ["audio"],
                "max_output_tokens": 512,
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcmu"},
                        "turn_detection": {"type": "server_vad"},
                        "transcription": {
                            "language": "en",
                            "model": "whisper-1",
                        }
                    },
                    "output": {
                        "format": {"type": "audio/pcmu"},
                        "voice": "marin"
                    }
                },
                "instructions": system_prompt,
                "tools": [
                    {
                        "type": "function",
                        "name": "end_conversation",
                        "description": "Call this when you have finished collecting all feedback from user and are ready to end the call",
                        "parameters": {
                            "type": "object",
                            "properties": {},
                            "required": []
                        }
                    }
                ],
                "tool_choice": "auto",
            }
        }

        print('Sending session update:', json.dumps(session_update))
        await openai_ws.send(json.dumps(session_update))

    async def handle_media_stream(self, websocket: WebSocket, task_id: str, booking_details: Dict):
        """
        Main WebSocket handler that bridges Twilio <-> OpenAI Realtime API.
        """        

        async with websockets.connect(
            uri=f"wss://api.openai.com/v1/realtime?model={require_env('OPENAI_REALTIME_MODEL_NAME')}",
            extra_headers=[
                ("Authorization", f"Bearer {require_env('OPENAI_API_KEY')}"),
            ]
        ) as openai_ws:
            
            # Get system prompt
            system_prompt = self._build_system_prompt(booking_details)

            await self.initialize_session(openai_ws, system_prompt)

            # Track conversation transcript and completion state
            transcript = []
            conversation_ended = False

            # Connection specific state
            stream_sid = None
            latest_media_timestamp = 0
            last_assistant_item = None
            mark_queue = []
            response_start_timestamp_twilio = None
            end_call = False

            # Bidirectional streaming tasks
            async def twilio_to_openai():
                """Receive audio data from Twilio and send it to the OpenAI Realtime API."""
                nonlocal stream_sid, latest_media_timestamp, response_start_timestamp_twilio, last_assistant_item, end_call
                logger.debug("[START] twilio_to_openai")

                try:
                    async for message in websocket.iter_text():
                        # Check if call should be hung by the server
                        if end_call:
                            logger.info("Twilio <-> FastAPI websocket needs to be close!")
                            break
                        data = json.loads(message)
                        event_type = data.get("event")

                        if event_type not in ["start", "media"]:
                            logger.warning(f"Event type: {event_type}")
                        # logger.info(f"Event type: {event_type}")

                        if event_type == "start":
                            stream_sid = data["start"]["streamSid"]
                            logger.info(f"Twilio stream (SID = {stream_sid}) started")
                            response_start_timestamp_twilio = None
                            latest_media_timestamp = 0
                            last_assistant_item = None

                        elif event_type == "media" and openai_ws.state.name == "OPEN":
                            latest_media_timestamp = int(data["media"]["timestamp"])
                            audio_payload = data["media"]["payload"]

                            # Send to OpenAI
                            openai_event = {
                                "type": "input_audio_buffer.append",
                                "audio": audio_payload
                            }
                            await openai_ws.send(json.dumps(openai_event))

                            # logger.info(f"Successfully sent event of type {event_type} to OpenAI")
                            
                        elif event_type == "mark":
                            # Handle mark acknowledgment
                            if mark_queue:
                                mark_queue.pop(0)
                        
                
                except WebSocketDisconnect:
                    print("Client disconnected.")
                    if openai_ws.state.name == 'OPEN':
                        await openai_ws.close()
                finally:
                    logger.info("Bye from `twilio_to_openai`")


            async def openai_to_twilio():
                """Receive events from the OpenAI Realtime API, send audio back to Twilio."""
                nonlocal stream_sid, last_assistant_item, response_start_timestamp_twilio, transcript, conversation_ended, end_call

                logger.warning("[START] openai_to_twilio...")
                try:
                    async for message in openai_ws:
                        response = json.loads(message)
                        event_type = response.get("type")
                        if event_type in LOG_EVENT_TYPES:
                            logger.warning(f"Event type: {event_type}", response)


                        # Get user audio transcription
                        if event_type == "conversation.item.input_audio_transcription.completed":
                            user_transcript = response.get("transcript", "")
                            if user_transcript:
                                transcript.append({
                                    "role": "user",
                                    "content": user_transcript
                                })
                                logger.info(f"User said: {user_transcript}")

                        # Get agent audio transcript
                        if event_type == "response.output_audio_transcript.done":
                            agent_transcript = response.get("transcript", "")
                            if agent_transcript:
                                transcript.append({
                                    "role": "agent",
                                    "content": agent_transcript
                                })

                        # Trigger end_conversation
                        # LLM determined that it has collected feedback from the user
                        # And so now should end the conversation!
                        if event_type == "response.function_call_arguments.done":
                            func_name = response.get("name")
                            call_id = response.get("call_id")

                            if func_name == "end_conversation":
                                logger.info("LLM requesting to end conversation...")
                                conversation_ended = True


                                # Send function result back to OpenAI as an ACK
                                func_output = {
                                    "type": "conversation.item.create",
                                    "item": {
                                        "type": "function_call_output",
                                        "call_id": call_id,
                                        "output": json.dumps({"status": "conversation_ended"})
                                    }
                                }

                                await openai_ws.send(json.dumps(func_output))

                                # Trigger final response generation
                                # await openai_ws.send(json.dumps({"type": "response.create"}))

                        if conversation_ended and event_type == "response.done":
                            logger.info("Hanging up call!")
                            end_call = True
                            await asyncio.sleep(5)  # Let the closing audio finish playing!
                            
                            if openai_ws.state.name == "OPEN":
                                logger.info("FasAPI <-> OpenAI websocket needs to be close!")
                                await openai_ws.close()
                            else:
                                logger.warning("FasAPI <-> OpenAI websocket already closed!")

                            break

                        # Handle audio output
                        if event_type == "response.output_audio.delta" and "delta" in response:
                            audio_payload = base64.b64encode(base64.b64decode(response['delta'])).decode('utf-8')

                            audio_delta = {
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": {
                                    "payload": audio_payload
                                }
                            }
                            await websocket.send_json(audio_delta)

                            if response.get("item_id") and response["item_id"] != last_assistant_item:
                                response_start_timestamp_twilio = latest_media_timestamp
                                last_assistant_item = response["item_id"]
                                if SHOW_TIMING_MATH:
                                    print(f"Setting start timestamp for new response: {response_start_timestamp_twilio}ms")
                            
                            # await _send_mark(websocket, stream_sid)

                        # Trigger an interruption. Your use case might work better using `input_audio_buffer.speech_stopped`, or combining the two.
                        # if event_type == 'input_audio_buffer.speech_started':
                        #     logger.info("Agent interrupted by user!!")
                        #     if last_assistant_item:
                        #         logger.info(f"Interrupting response with id: {last_assistant_item}")
                        #         # await handle_speech_started_event()

                except Exception as e:
                    logger.error(f"Error in openai_to_twilio: {str(e)}")
                finally:
                    logger.info("Bye from `openai_to_twilio`")

            async def handle_speech_started_event():
                """Handle interruption when the caller's speech starts."""
                nonlocal response_start_timestamp_twilio, last_assistant_item
                print("Handling speech started event.")
                if mark_queue and response_start_timestamp_twilio is not None:
                    elapsed_time = latest_media_timestamp - response_start_timestamp_twilio
                    if SHOW_TIMING_MATH:
                        print(f"Calculating elapsed time for truncation: {latest_media_timestamp} - {response_start_timestamp_twilio} = {elapsed_time}ms")

                    if last_assistant_item:
                        if SHOW_TIMING_MATH:
                            print(f"Truncating item with ID: {last_assistant_item}, Truncated at: {elapsed_time}ms")

                        truncate_event = {
                            "type": "conversation.item.truncate",
                            "item_id": last_assistant_item,
                            "content_index": 0,
                            "audio_end_ms": elapsed_time
                        }
                        await openai_ws.send(json.dumps(truncate_event))

                    await websocket.send_json({
                        "event": "clear",
                        "streamSid": stream_sid
                    })

                    mark_queue.clear()
                    last_assistant_item = None
                    response_start_timestamp_twilio = None
            
            async def _send_mark(websocket: WebSocket, stream_sid: str):
                """Send mark event to Twilio for tracking"""
                if stream_sid:
                    mark_event = {
                        "event": "mark",
                        "streamSid": stream_sid,
                        "mark": {"name": "responsePart"}
                    }
                    await websocket.send_text(json.dumps(mark_event))
                    mark_queue.append("responsePart")

            # Run both streaming tasks concurrently
            await asyncio.gather(
                twilio_to_openai(),
                openai_to_twilio()
            )

            try:
                await websocket.close()
                logger.info("Twilio <-> FastAPI websocket closed!")
            except Exception as e:
                logger.error(f"Error closing Twilio Websocket: {e}")
            
            logger.info(f"Feedback session {task_id} completed successfully")

            if transcript:
                logger.info("Generating summary for transcript...")

                summary = await self._generate_summary(transcript=transcript)

                # Update data with transcript and summary
                async with AsyncSessionLocal() as db:
                    try:
                        result = await db.execute(
                            select(FeedbackSession).where(FeedbackSession.id == task_id)
                        )
                        session = result.scalar_one_or_none()
                        
                        if session:
                            # Store transcript and summary
                            session.transcript = transcript  # JSON column
                            session.summary = summary  # JSON column
                            session.status = SessionStatus.COMPLETED
                            session.completed_at = datetime.now(timezone.utc)
                            
                            await db.commit()
                            logger.info(f"Summary saved for session {task_id} with sentiment: {summary.get('sentiment')}")
                        else:
                            logger.error(f"Session {task_id} not found in database")
                            
                    except Exception as e:
                        logger.error(f"Error saving summary to database: {str(e)}")
                        await db.rollback()
            else:
                logger.warning(f"No transcript available for session {task_id}")
                # Update status even without transcript
                async with AsyncSessionLocal() as db:
                    result = await db.execute(
                        select(FeedbackSession).where(FeedbackSession.id == task_id)
                    )
                    session = result.scalar_one_or_none()
                    if session:
                        session.status = SessionStatus.COMPLETED
                        await db.commit()

    
#     def _build_system_prompt(self, booking_details: Dict) -> str:
#         """Build the system prompt for the OpenAI agent"""
#         return f"""
# You are a friendly feedback collection agent for {booking_details['hostel_name']}. 
# You're speaking with {booking_details['guest_name']} who recently stayed at the hostel 
# from {booking_details['check_in']} to {booking_details['check_out']} in room {booking_details['room_number']}.

# Your goal is to have a natural, conversational phone call to collect feedback about their stay.

# IMPORTANT - START THE CONVERSATION:
# Begin by saying: "Hi {booking_details['guest_name']}! Thank you for taking my call. I'm calling from {booking_details['hostel_name']} 
# to ask you a few questions about your recent stay with us. This will only take about 3 to 5 minutes. 
# How are you doing today?"

# GUIDELINES TO COVER:
# {self.guidelines}

# CONVERSATION RULES:
# 1. Be warm, friendly, and professional - you're representing the hostel
# 2. Keep the conversation natural and flowing, not like a rigid survey
# 3. Ask open-ended questions and actively listen
# 4. Follow up on any concerns or issues they mention with empathy
# 5. Acknowledge their feedback positively (e.g., "That's great to hear!" or "I'm sorry about that")
# 6. Try to cover all guideline areas but let the conversation flow naturally
# 7. If they seem rushed, be understanding and keep it brief
# 8. When you've gathered sufficient feedback (or after ~5 minutes), thank them warmly
# 9. Call the end_conversation function when you're ready to wrap up

# Remember: This is a real phone call. Be natural, empathetic, and respectful of their time.
# """
    
    def _build_system_prompt(self, booking_details: Dict) -> str:
        """Build the system prompt for the OpenAI agent"""
        return f"""
You are a friendly feedback collection agent for {booking_details['hostel_name']}. 
You're speaking with {booking_details['guest_name']} who recently stayed at the hostel 
from {booking_details['check_in']} to {booking_details['check_out']} in room {booking_details['room_number']}.

Your goal is to ask the user a single question about the stay at the hotel. That's it!

Remember: This is a real phone call. Be natural, empathetic, and respectful of their time.
"""
    


    async def _generate_summary(self, transcript: List[Dict]) -> Dict:
        """
        Generate structured summary from conversation transcript using LLM.
        """
        try:
            if not transcript:
                logger.warning("Empty transcript, returning default summary")
                return {
                    "overview": "No conversation data available",
                    "painpoints": [],
                    "highlights": [],
                    "recommendations": [],
                    "sentiment": "unknown"
                }
            
            # Convert transcript of type List[Dict] to a string
            # so that we can pass it to the LLM 
            conversation_text = "\n".join([
                f"{turn['role'].upper()}: {turn['content']}"
                for turn in transcript
            ])

            prompt = f"""
Analyze this customer feedback conversation and provide a structured summary in JSON format.

CONVERSATION:
{conversation_text}

Provide a JSON response with this exact structure:
{{
    "overview": "Brief 2-3 sentence overview of the key feedback",
    "painpoints": ["list of issues, complaints, or areas needing improvement"],
    "highlights": ["list of positive aspects and things they enjoyed"],
    "recommendations": ["list of specific, actionable improvements the hostel should consider"],
    "sentiment": "positive/neutral/negative"
}}

Be specific and actionable. Focus on insights that can help improve the hostel.
"""
            response = await self.openai_client.chat.completions.create(
                model=require_env("OPENAI_CHAT_MODEL_NAME"),
                messages=[
                    {"role": "system", "content": "You are an expert at analyzing customer feedback and extracting actionable insights for hospitality businesses."},
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_object"},
                temperature=0.3
            )

            summary = json.loads(response.choices[0].message.content)
            logger.info(f"Generated summary with sentiment: {summary.get('sentiment')}")
            return summary
            
        except Exception as e:
            logger.error(f"Error generating summary: {str(e)}")
            return {
                "overview": "Error generating summary",
                "painpoints": [],
                "highlights": [],
                "recommendations": [],
                "sentiment": "unknown"
            }