from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Response
from pinecone_plugins.assistant.models.chat import Message
from fastapi.responses import Response
from prompts import SYSTEM_MESSAGE
from dotenv import load_dotenv
from twilio.rest import Client
from datetime import datetime, timedelta
from pinecone import Pinecone
import websockets
import traceback
import requests
import audioop
import asyncio
import base64
import json
import os
import re

load_dotenv(override=True)

# Get environment variables
ULTRAVOX_API_KEY = os.environ.get('ULTRAVOX_API_KEY')
PINECONE_API_KEY = os.environ.get('PINECONE_API_KEY')
N8N_WEBHOOK_URL = os.environ.get('N8N_WEBHOOK_URL')
PUBLIC_URL = os.environ.get('PUBLIC_URL')
PORT = int(os.environ.get('PORT', '8000'))
TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN')
TWILIO_PHONE_NUMBER = os.environ.get('TWILIO_PHONE_NUMBER')
print("N8N_WEBHOOK_URL:", N8N_WEBHOOK_URL)

# Ultravox defaults
ULTRAVOX_MODEL = "fixie-ai/ultravox-70B"
ULTRAVOX_VOICE = "Alex-Spanish"   
ULTRAVOX_SAMPLE_RATE = 8000        
ULTRAVOX_BUFFER_SIZE = 60        

# Calendario de estilistas
CALENDARS_LIST = {
    "MARIA": "CALENDAR_MARIA@peluqueria.com",
    "CARLOS": "CALENDAR_CARLOS@peluqueria.com",
    "ANA": "CALENDAR_ANA@peluqueria.com",
    "JOSE": "CALENDAR_JOSE@peluqueria.com",
    "LAURA": "CALENDAR_LAURA@peluqueria.com",
    "ELENA": "CALENDAR_ELENA@peluqueria.com"
}

# Duración de servicios en minutos
SERVICE_DURATIONS = {
    "CORTE_CABALLERO": 20,
    "CORTE_CON_SECADO": 30,
    "CORTE_SIN_SECADO": 20,
    "MECHAS": 40,
    "BALAIX": 30,
    "COLOR_ORGANICO": 30,
    "SECADO_S": 15,
    "SECADO_M": 20,
    "SECADO_L": 30,
    "QUERATINA_ORGANICA": 240,
    "TRATAMIENTO_HIDRATACION": 60
}

app = FastAPI()

# Keep the same session store
sessions = {}

# Just for debugging specific event types
LOG_EVENT_TYPES = [
    'response.content.done',
    'response.done',
    'session.created',
    'conversation.item.input_audio_transcription.completed'
]


@app.get("/")
async def root():
    return {"message": "Twilio + Ultravox Media Stream Server is running!"}

@app.post("/incoming-call")
async def incoming_call(request: Request):
    """
    Handle the inbound call from Twilio. 
    - Fetch firstMessage from N8N
    - Store session data
    - Respond with TwiML containing <Stream> to /media-stream
    """
    form_data = await request.form()
    twilio_params = dict(form_data)
    print('Incoming call')

    caller_number = twilio_params.get('From', 'Unknown')
    session_id = twilio_params.get('CallSid')
    print('Caller Number:', caller_number)
    print('Session ID (CallSid):', session_id)

    # Fetch first message from N8N
    first_message = "Hola, soy Sara de Peluquería Estilo. ¿En qué puedo ayudarte hoy?"
    print("Fetching N8N ...")
    try:
        # Preparar datos iniciales de la sesión
        session_data = {
            "route": "1",
            "number": caller_number,
            "call_details": {
                "call_id": session_id,
                "timestamp": datetime.now().isoformat(),
                "initial_state": {
                    "selected_stylist": None,
                    "selected_service": None,
                    "selected_date": None,
                    "selected_time": None
                }
            }
        }

        webhook_response = requests.post(
            N8N_WEBHOOK_URL,
            headers={"Content-Type": "application/json"},
            json=session_data
        )
        
        if webhook_response.ok:
            response_text = webhook_response.text
            try:
                response_data = json.loads(response_text)
                if response_data and response_data.get('firstMessage'):
                    first_message = response_data['firstMessage']
                    print('Parsed firstMessage from N8N:', first_message)
            except json.JSONDecodeError:
                first_message = response_text.strip()
        else:
            print(f"Error al enviar datos al webhook de N8N: {webhook_response.status_code}")
    except Exception as e:
        print(f"Error al enviar datos al webhook de N8N: {e}")

    # Save session with initial appointment data
    session = {
        "transcript": "",
        "callerNumber": caller_number,
        "callDetails": twilio_params,
        "firstMessage": first_message,
        "streamSid": None,
        "appointmentData": {
            "selected_stylist": None,
            "selected_service": None,
            "selected_date": None,
            "selected_time": None
        }
    }
    sessions[session_id] = session

    # Respond with TwiML to connect to /media-stream
    host = PUBLIC_URL
    stream_url = f"{host.replace('https', 'wss')}/media-stream"

    twiml_response = f"""<?xml version="1.0" encoding="UTF-8"?>
        <Response>
            <Connect>
                <Stream url="{stream_url}">
                    <Parameter name="firstMessage" value="{first_message}" />
                    <Parameter name="callerNumber" value="{caller_number}" />
                    <Parameter name="callSid" value="{session_id}" />
                </Stream>
            </Connect>
        </Response>"""

    return Response(content=twiml_response, media_type="text/xml")


@app.post("/outgoing-call")
async def outgoing_call(request: Request):
    try:
        # Get request data
        data = await request.json() 
        phone_number = data.get('phoneNumber')
        first_message = data.get('firstMessage')
        if not phone_number:
            return {"error": "Phone number is required"}, 400
        
        print(' Initiating outbound call to:', phone_number)
        print(' With the following first message:', first_message)
        
        # Initialize Twilio client
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

        # Store call data
        call_data = {
            "originalRequest": data,
            "startTime": datetime.now().isoformat()
        }

         # Respond with TwiML to connect to /media-stream
        host = PUBLIC_URL
        stream_url = f"{host.replace('https', 'wss')}/media-stream"
        
        print(' Creating Twilio call with TWIML...')
        call = client.calls.create(
            twiml=f'''<Response>
                        <Connect>
                            <Stream url="{stream_url}">
                                <Parameter name="firstMessage" value="{first_message}" />
                                <Parameter name="callerNumber" value="{phone_number}" />
                            </Stream> 
                        </Connect>
                    </Response>''',
            to=phone_number,
            from_=TWILIO_PHONE_NUMBER,
            status_callback=f"{PUBLIC_URL}/call-status",
            status_callback_event=['initiated', 'ringing', 'answered', 'completed']
        )

        print(' Twilio call created:', call.sid)
        # Store call data in sessions
        sessions[call.sid] = {
            "transcript": "",
            "callerNumber": phone_number,
            "callDetails": call_data,
            "firstMessage": first_message,
            "streamSid": None
        }

        return {
            "success": True,
            "callSid": call.sid
        }

    except Exception as error:
        print(' Error creating call:', str(error))
        traceback.print_exc()
        return {"error": str(error)}, 500
    

@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket):
    await websocket.accept()
    
    # Get parameters from WebSocket query
    params = websocket.query_params
    first_message = params.get("firstMessage", "")
    caller_number = params.get("callerNumber", "")
    call_sid = params.get("callSid", "")
    
    print(f"WebSocket connected: {call_sid}")
    
    # Initialize or get session
    session = sessions.get(call_sid, {
        "transcript": "",
        "callerNumber": caller_number,
        "callSid": call_sid,
        "firstMessage": first_message,
        "streamSid": None,
        "appointmentData": {
            "selected_stylist": None,
            "selected_service": None,
            "selected_date": None,
            "selected_time": None
        }
    })
    
    if call_sid:
        sessions[call_sid] = session
    
    # Create periodic update task
    update_interval = 10  # segundos
    periodic_update_task = None
    
    try:
        # Create Ultravox call
        uv_join_url = await create_ultravox_call(SYSTEM_MESSAGE, first_message)
        print(f"Ultravox join URL: {uv_join_url}")
        
        # Connect to Ultravox WebSocket
        async with websockets.connect(uv_join_url) as uv_ws:
            print("Connected to Ultravox WebSocket")
            
            # Start periodic update task
            periodic_update_task = asyncio.create_task(
                periodic_conversation_updates(call_sid, update_interval)
            )
            
            # Create tasks for handling Twilio and Ultravox
            twilio_task = asyncio.create_task(handle_twilio_messages(websocket, uv_ws, session))
            ultravox_task = asyncio.create_task(handle_ultravox_messages(uv_ws, websocket, session))
            
            # Wait for either task to complete
            done, pending = await asyncio.wait(
                [twilio_task, ultravox_task],
                return_when=asyncio.FIRST_COMPLETED
            )
            
            # Cancel the remaining task
            for task in pending:
                task.cancel()
                
            # Cancel periodic update task
            if periodic_update_task:
                periodic_update_task.cancel()
                
            # Send transcript to N8N
            await send_transcript_to_n8n(session)
            
    except websockets.exceptions.ConnectionClosedError:
        print("WebSocket connection closed")
    except Exception as e:
        print(f"Error in media_stream: {e}")
        traceback.print_exc()
    finally:
        # Cancel periodic update task if it exists
        if periodic_update_task:
            periodic_update_task.cancel()
            
        # Clean up
        if call_sid in sessions:
            # Enviar una última actualización antes de cerrar
            await periodic_conversation_update(call_sid)
            
            # Enviar transcripción completa
            await send_transcript_to_n8n(sessions[call_sid])


#
# Handle Twilio call status updates
#
@app.post("/call-status")
async def call_status(request: Request):
    try:
        # Get form data
        data = await request.form()
        print('\n===  Twilio Status Update ===')
        print('Status:', data.get('CallStatus'))
        print('Duration:', data.get('CallDuration'))
        print('Timestamp:', data.get('Timestamp'))
        print('Call SID:', data.get('CallSid'))
        # print('Full status payload:', dict(data))
        print('\n====== END ======')
        
    except Exception as e:
        print(f"Error getting request data: {e}")
        return {"error": str(e)}, 400

    return {"success": True}

#
# Create an Ultravox serverWebSocket call
#
async def create_ultravox_call(system_prompt: str, first_message: str) -> str:
    """
    Creates a new Ultravox call in serverWebSocket mode and returns the joinUrl.
    """
    url = "https://api.ultravox.ai/api/calls"
    headers = {
        "X-API-Key": ULTRAVOX_API_KEY,
        "Content-Type": "application/json"
    }

    payload = {
        "systemPrompt": system_prompt,
        "model": ULTRAVOX_MODEL,
        "voice": ULTRAVOX_VOICE,
        "temperature":0.1,
        "initialMessages": [
            {
                "role": "MESSAGE_ROLE_ASSISTANT",  
                "text": first_message
            }
        ],
        "medium": {
            "serverWebSocket": {
                "inputSampleRate": ULTRAVOX_SAMPLE_RATE,   
                "outputSampleRate": ULTRAVOX_SAMPLE_RATE,   
                "clientBufferSizeMs": ULTRAVOX_BUFFER_SIZE
            }
        },
        "selectedTools": [  
            {
                "temporaryTool": {
                    "modelToolName": "question_and_answer",
                    "description": "Get answers to customer questions especially about AI employees",
                    "dynamicParameters": [
                        {
                            "name": "question",
                            "location": "PARAMETER_LOCATION_BODY",
                            "schema": {
                                "type": "string",
                                "description": "Question to be answered"
                            },
                            "required": True
                        }
                    ],
                    "timeout": "20s",
                    "client": {},
                },
            },
            {
                "temporaryTool": {
                    "modelToolName": "schedule_meeting",
                    "description": "Schedule a meeting for a customer. Returns a message indicating whether the booking was successful or not.",
                    "dynamicParameters": [
                        {
                            "name": "name",
                            "location": "PARAMETER_LOCATION_BODY",
                            "schema": {
                                "type": "string",
                                "description": "Customer's name"
                            },
                            "required": True
                        },
                        {
                            "name": "email",
                            "location": "PARAMETER_LOCATION_BODY",
                            "schema": {
                                "type": "string",
                                "description": "Customer's email"
                            },
                            "required": True
                        },
                        {
                            "name": "purpose",
                            "location": "PARAMETER_LOCATION_BODY",
                            "schema": {
                                "type": "string",
                                "description": "Purpose of the Meeting"
                            },
                            "required": True
                        },
                        {
                            "name": "datetime",
                            "location": "PARAMETER_LOCATION_BODY",
                            "schema": {
                                "type": "string",
                                "description": "Meeting Datetime"
                            },
                            "required": True
                        },
                        {
                            "name": "location",
                            "location": "PARAMETER_LOCATION_BODY",
                            "schema": {
                                "type": "string",
                                "enum": ["London", "Manchester", "Brighton"],
                                "description": "Meeting location"
                            },
                            "required": True
                        }
                    ],
                    "timeout": "20s",
                    "client": {},
                },
            },
            { "temporaryTool": {
                "modelToolName": "hangUp",
                "description": "End the call",
                "client": {},
                }
            }
        ]
    }

    # print("Creating Ultravox call with payload:", json.dumps(payload, indent=2))  # Enhanced logging

    try:
        resp = requests.post(url, headers=headers, json=payload)
        if not resp.ok:
            print("Ultravox create call error:", resp.status_code, resp.text)
            return ""
        body = resp.json()
        join_url = body.get("joinUrl") or ""
        print("Ultravox joinUrl received:", join_url)  # Enhanced logging
        return join_url
    except Exception as e:
        print("Ultravox create call request failed:", e)
        return ""

#
# Handle "question_and_answer" via Pinecone
#
async def handle_question_and_answer(uv_ws, invocationId: str, question: str):
    try:
        pc = Pinecone(api_key=PINECONE_API_KEY)
        assistant = pc.assistant.Assistant(assistant_name="rag-tool")

        msg = Message(content=question)
        chunks = assistant.chat(messages=[msg], stream=True)

        # Collect entire answer
        answer_message = ""
        for chunk in chunks:
            if chunk and chunk.type == "content_chunk":
                answer_message += chunk.delta.content

        # Respond back to Ultravox
        tool_result = {
            "type": "client_tool_result",
            "invocationId": invocationId,
            "result": answer_message,
            "response_type": "tool-response"
        }
        await uv_ws.send(json.dumps(tool_result))
    except Exception as e:
        print(f"Error in Q&A tool: {e}")
        # Send error result back to Ultravox
        error_result = {
            "type": "client_tool_result",
            "invocationId": invocationId,
            "error_type": "implementation-error",
            "error_message": "An error occurred while processing your request."
        }
        await uv_ws.send(json.dumps(error_result))

#
# Handle "schedule_meeting" calls
#
async def handle_schedule_meeting(uv_ws, session, invocationId: str, parameters):
    """
    Uses N8N to finalize a meeting schedule.
    Parameters expected:
    - selected_stylist: Nombre del estilista
    - selected_service: Servicio solicitado
    - selected_date: Fecha (YYYY-MM-DD)
    - selected_time: Hora (HH:mm)
    """
    try:
        # Extract parameters
        stylist = parameters.get('selected_stylist', '')
        service = parameters.get('selected_service', '')
        date = parameters.get('selected_date', '')
        time = parameters.get('selected_time', '')
        
        # Validate parameters
        if not all([stylist, service, date, time]):
            raise ValueError("Faltan parámetros requeridos para la cita")
        
        # Update appointment status in session and N8N
        session_id = session.get("callSid", "")
        await update_appointment_status(session_id, "selected_stylist", stylist)
        await update_appointment_status(session_id, "selected_service", service)
        await update_appointment_status(session_id, "selected_date", date)
        await update_appointment_status(session_id, "selected_time", time)
        
        # Get service duration
        service_key = service.upper().replace(' ', '_')
        duration = SERVICE_DURATIONS.get(service_key, 30)
        
        # Get calendar ID based on stylist
        stylist_key = stylist.upper()
        calendar_id = CALENDARS_LIST.get(stylist_key, '')
        if not calendar_id:
            raise ValueError(f"Estilista no encontrado: {stylist}")
        
        # Format datetime for N8N
        appointment_datetime = f"{date} {time}"
        
        # Prepare scheduling data
        scheduling_data = {
            "appointment_details": {
                "stylist": stylist,
                "service": service,
                "date": date,
                "time": time,
                "duration": duration,
                "calendar_id": calendar_id
            },
            "customer_details": {
                "phone": session.get("callerNumber", ""),
                "call_id": session_id
            }
        }

        # Send to N8N for processing
        webhook_response = await send_to_webhook({
            "route": "schedule_appointment",
            "data": scheduling_data
        })

        try:
            response_data = json.loads(webhook_response)
            confirmation_status = response_data.get('status', 'error')
            message = response_data.get('message', 'No se pudo procesar la solicitud')

            # Return the result to Ultravox
            tool_result = {
                "type": "tool-response",
                "invocationId": invocationId,
                "result": {
                    "status": confirmation_status,
                    "message": message,
                    "appointment": {
                        "stylist": stylist,
                        "service": service,
                        "date": date,
                        "time": time,
                        "duration": duration
                    }
                }
            }
            
            await uv_ws.send(json.dumps(tool_result))
            print(f"Sent schedule_meeting result to Ultravox: {message}")

        except json.JSONDecodeError:
            raise ValueError("Error al procesar la respuesta de N8N")

    except Exception as e:
        error_message = f"Error al programar la cita: {str(e)}"
        print(error_message)
        error_result = {
            "type": "tool-response",
            "invocationId": invocationId,
            "error": error_message
        }
        await uv_ws.send(json.dumps(error_result))

#
# Analizar la transcripción para extraer información
#
async def extract_appointment_info_from_transcript(transcript):
    """
    Analiza la transcripción para extraer información sobre la cita
    """
    appointment_info = {
        "selected_stylist": None,
        "selected_service": None,
        "selected_date": None,
        "selected_time": None,
        "customer_name": None,
        "customer_email": None
    }
    
    # Lista de estilistas para buscar en la transcripción
    stylists = ["Maria", "Carlos", "Ana", "Jose", "Laura", "Elena", "Lena"]
    
    # Lista de servicios para buscar en la transcripción
    services = ["corte de pelo", "corte con secado", "corte sin secado", 
                "mechas", "balaix", "color", "secado", "queratina", "tratamiento"]
    
    # Patrones para fechas y horas
    days = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
    
    # Analizar línea por línea
    lines = transcript.lower().split('\n')
    for line in lines:
        # Buscar estilista
        if appointment_info["selected_stylist"] is None:
            for stylist in stylists:
                if stylist.lower() in line.lower():
                    appointment_info["selected_stylist"] = stylist
                    break
        
        # Buscar servicio
        if appointment_info["selected_service"] is None:
            for service in services:
                if service in line.lower():
                    appointment_info["selected_service"] = service
                    break
        
        # Buscar día
        if appointment_info["selected_date"] is None:
            for day in days:
                if day in line.lower():
                    # Convertir día de la semana a fecha
                    today = datetime.now()
                    weekday_today = today.weekday()  # 0 = lunes, 6 = domingo
                    day_index = days.index(day)  # 0 = lunes, 6 = domingo
                    
                    # Calcular días hasta el día deseado
                    days_until = (day_index - weekday_today) % 7
                    if days_until == 0:
                        days_until = 7  # Si es el mismo día, ir a la próxima semana
                    
                    # Calcular la fecha
                    appointment_date = today + datetime.timedelta(days=days_until)
                    appointment_info["selected_date"] = appointment_date.strftime("%Y-%m-%d")
                    break
        
        # Buscar hora
        if appointment_info["selected_time"] is None:
            # Patrones comunes de hora
            time_patterns = [
                r'(\d{1,2})(?:\s*)?(?::|h)(?:\s*)?(\d{2})',  # 17:00, 5:30, 17h00
                r'(\d{1,2})(?:\s+)?(?:de la|en la|por la)(?:\s+)?(mañana|tarde|noche)',  # 5 de la tarde
                r'a las (\d{1,2})'  # a las 5
            ]
            
            for pattern in time_patterns:
                matches = re.findall(pattern, line.lower())
                if matches:
                    # Convertir a formato HH:MM
                    if "mañana" in str(matches) or "tarde" in str(matches) or "noche" in str(matches):
                        hour = int(matches[0][0])
                        period = matches[0][1]
                        
                        # Ajustar hora según período
                        if period == "tarde" and hour < 12:
                            hour += 12
                        elif period == "noche" and hour < 12:
                            hour += 12
                        
                        appointment_info["selected_time"] = f"{hour:02d}:00"
                    else:
                        # Formato HH:MM
                        if len(matches[0]) >= 2:
                            hour = int(matches[0][0])
                            minute = matches[0][1] if len(matches[0]) > 1 else "00"
                            
                            # Ajustar para PM si es probable
                            if "tarde" in line.lower() or "noche" in line.lower() and hour < 12:
                                hour += 12
                                
                            appointment_info["selected_time"] = f"{hour:02d}:{minute}"
                        else:
                            hour = int(matches[0])
                            # Ajustar para PM si es probable
                            if "tarde" in line.lower() or "noche" in line.lower() and hour < 12:
                                hour += 12
                            appointment_info["selected_time"] = f"{hour:02d}:00"
                    break
        
        # Buscar nombre del cliente
        if "mi nombre es" in line.lower() and appointment_info["customer_name"] is None:
            name_match = re.search(r'mi nombre es\s+([a-zA-Z]+)', line.lower())
            if name_match:
                appointment_info["customer_name"] = name_match.group(1).capitalize()
        
        # Buscar correo electrónico
        if "@" in line and "." in line and appointment_info["customer_email"] is None:
            email_match = re.search(r'([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)', line.lower().replace(" arroba ", "@").replace(" punto ", "."))
            if email_match:
                appointment_info["customer_email"] = email_match.group(1)
    
    return appointment_info

#
# Send entire transcript to N8N (end of call)
#
async def send_transcript_to_n8n(session):
    print("Full Transcript:\n", session['transcript'])
    
    # Extraer las variables de la sesión
    appointment_data = session.get("appointmentData", {
        "selected_stylist": None,
        "selected_service": None,
        "selected_date": None,
        "selected_time": None
    })
    
    # Extraer información de la transcripción
    transcript_data = await extract_appointment_info_from_transcript(session["transcript"])
    
    # Si alguna variable es None, intentar actualizarla con datos de la transcripción
    for key, value in transcript_data.items():
        if key in appointment_data and appointment_data[key] is None and value is not None:
            appointment_data[key] = value
    
    # Enviar transcripción y datos de la cita
    await send_to_webhook({
        "route": "2",
        "number": session.get("callerNumber", "Unknown"),
        "data": session["transcript"],
        "appointment_data": appointment_data,
        "customer_info": {
            "name": transcript_data.get("customer_name", ""),
            "email": transcript_data.get("customer_email", ""),
            "phone": session.get("callerNumber", "")
        },
        "call_details": {
            "call_id": session.get("callSid", ""),
            "end_time": datetime.now().isoformat(),
            "call_duration": session.get("callDetails", {}).get("CallDuration", "0")
        }
    })

#
# Send data to N8N webhook
#
async def send_to_webhook(payload):
    if not N8N_WEBHOOK_URL:
        print("Error: N8N_WEBHOOK_URL is not set")
        return json.dumps({"error": "N8N_WEBHOOK_URL not configured"})
        
    try:
        print(f"Sending payload to N8N webhook: {N8N_WEBHOOK_URL}")
        print(f"Payload: {json.dumps(payload, indent=2)}")
        
        response = requests.post(
            N8N_WEBHOOK_URL,
            json=payload,
            headers={"Content-Type": "application/json"}
        )
        
        if response.status_code != 200:
            print(f"N8N webhook returned status code {response.status_code}")
            print(f"Response: {response.text}")
            return json.dumps({"error": f"N8N webhook returned status {response.status_code}"})
            
        return response.text
        
    except requests.exceptions.RequestException as e:
        error_msg = f"Error sending data to N8N webhook: {str(e)}"
        print(error_msg)
        return json.dumps({"error": error_msg})

#
# Actualiza el estado de la cita y notifica a N8N
#
async def update_appointment_status(session_id: str, field: str, value: str):
    """
    Actualiza el estado de la cita y notifica a N8N
    """
    if session_id in sessions:
        session = sessions[session_id]
        
        # Actualizar el campo específico
        if "appointmentData" not in session:
            session["appointmentData"] = {
                "selected_stylist": None,
                "selected_service": None,
                "selected_date": None,
                "selected_time": None
            }
        
        session["appointmentData"][field] = value
        
        # Notificar a N8N sobre el cambio
        try:
            update_data = {
                "route": "appointment_update",
                "number": session.get("callerNumber", "Unknown"),
                "call_id": session_id,
                "update_type": field,
                "update_value": value,
                "current_state": session["appointmentData"]
            }
            
            # Enviar actualización a N8N
            webhook_response = await send_to_webhook(update_data)
            print(f"Appointment status update sent to N8N: {field} = {value}")
            
            return True
        except Exception as e:
            print(f"Error updating appointment status in N8N: {e}")
            return False
    
    return False

#
# Actualiza periódicamente el estado de la conversación a N8N
#
async def periodic_conversation_update(session_id):
    """
    Envía actualizaciones periódicas del estado de la conversación a N8N
    """
    if session_id not in sessions:
        return
        
    session = sessions[session_id]
    
    # Extraer información de la transcripción
    transcript_data = await extract_appointment_info_from_transcript(session["transcript"])
    
    # Actualizar datos de la sesión con la información extraída
    if "appointmentData" not in session:
        session["appointmentData"] = {
            "selected_stylist": None,
            "selected_service": None,
            "selected_date": None,
            "selected_time": None
        }
    
    # Actualizar solo los valores que son None
    appointment_data = session["appointmentData"]
    updated = False
    
    for key, value in transcript_data.items():
        if key in appointment_data and value is not None:
            if appointment_data[key] != value:
                appointment_data[key] = value
                updated = True
    
    # Si se actualizó algún valor, enviar a N8N
    if updated:
        update_data = {
            "route": "conversation_update",
            "number": session.get("callerNumber", "Unknown"),
            "call_id": session_id,
            "timestamp": datetime.now().isoformat(),
            "conversation_state": {
                "appointment_data": appointment_data,
                "customer_info": {
                    "name": transcript_data.get("customer_name", ""),
                    "email": transcript_data.get("customer_email", ""),
                    "phone": session.get("callerNumber", "")
                },
                "transcript_length": len(session["transcript"])
            }
        }
        
        # Enviar actualización a N8N
        await send_to_webhook(update_data)
        print(f"Sent conversation update to N8N: {json.dumps(update_data, indent=2)}")

#
# Función para ejecutar actualizaciones periódicas
#
async def periodic_conversation_updates(session_id, interval):
    """
    Ejecuta actualizaciones periódicas del estado de la conversación
    """
    while True:
        await periodic_conversation_update(session_id)
        await asyncio.sleep(interval)

#
# Manejar mensajes de Twilio
#
async def handle_twilio_messages(websocket, uv_ws, session):
    """
    Maneja los mensajes entrantes desde Twilio
    """
    try:
        while True:
            message = await websocket.receive_text()
            data = json.loads(message)

            if data.get('event') == 'start':
                stream_sid = data['start']['streamSid']
                call_sid = data['start']['callSid']
                
                # Actualizar la sesión con el stream_sid
                session['streamSid'] = stream_sid
                print(f"Twilio stream started: {stream_sid} for call {call_sid}")
                
            elif data.get('event') == 'media':
                # Twilio envía audio del usuario
                payload_base64 = data['media']['payload']

                try:
                    # Decodificar base64 para obtener bytes µ-law
                    mu_law_bytes = base64.b64decode(payload_base64)
                    # Transcodificar µ-law a PCM (s16le)
                    pcm_bytes = audioop.ulaw2lin(mu_law_bytes, 2)
                    # Enviar bytes PCM a Ultravox
                    await uv_ws.send(pcm_bytes)
                except Exception as e:
                    print(f"Error procesando audio: {e}")
                    
            elif data.get('event') == 'stop':
                print(f"Twilio stream stopped: {data.get('streamSid')}")
                return  # Terminar el manejador
                
    except WebSocketDisconnect:
        print("Twilio WebSocket desconectado")
    except Exception as e:
        print(f"Error en handle_twilio_messages: {e}")
        traceback.print_exc()

#
# Manejar mensajes de Ultravox
#
async def handle_ultravox_messages(uv_ws, websocket, session):
    """
    Maneja los mensajes entrantes desde Ultravox
    """
    try:
        async for raw_message in uv_ws:
            if isinstance(raw_message, bytes):
                # Audio del agente en PCM s16le
                try:
                    # Transcodificar PCM a µ-law
                    mu_law_bytes = audioop.lin2ulaw(raw_message, 2)
                    payload_base64 = base64.b64encode(mu_law_bytes).decode('ascii')
                    
                    # Enviar a Twilio como carga útil de medios
                    await websocket.send_text(json.dumps({
                        "event": "media",
                        "streamSid": session.get("streamSid", ""),
                        "media": {
                            "payload": payload_base64
                        }
                    }))
                except Exception as e:
                    print(f"Error procesando audio del agente: {e}")
                    
            else:
                # Mensaje de texto de Ultravox
                try:
                    msg_data = json.loads(raw_message)
                    msg_type = msg_data.get("type") or msg_data.get("eventType")
                    
                    if msg_type == "transcript":
                        role = msg_data.get("role")
                        text = msg_data.get("text") or msg_data.get("delta")
                        final = msg_data.get("final", False)
                        
                        if role and text:
                            role_cap = role.capitalize()
                            session['transcript'] += f"{role_cap}: {text}\n"
                            print(f"{role_cap} dice: {text}")
                            
                    elif msg_type == "client_tool_invocation":
                        toolName = msg_data.get("toolName", "")
                        invocationId = msg_data.get("invocationId")
                        parameters = msg_data.get("parameters", {})
                        print(f"Invocando herramienta: {toolName} con ID: {invocationId}")
                        
                        if toolName == "question_and_answer":
                            question = parameters.get('question')
                            await handle_question_and_answer(uv_ws, invocationId, question)
                            
                        elif toolName == "schedule_meeting":
                            await handle_schedule_meeting(uv_ws, session, invocationId, parameters)
                            
                        elif toolName == "hangUp":
                            print("Recibida solicitud de colgar")
                            # Enviar respuesta de éxito al agente
                            tool_result = {
                                "type": "tool-response",
                                "invocationId": invocationId,
                                "result": "Llamada finalizada con éxito"
                            }
                            await uv_ws.send(json.dumps(tool_result))
                            
                            # Finalizar la llamada de Twilio
                            try:
                                client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
                                client.calls(session.get("callSid")).update(status='completed')
                                print(f"Llamada de Twilio finalizada: {session.get('callSid')}")
                            except Exception as e:
                                print(f"Error al finalizar la llamada: {e}")
                            
                            return  # Salir del manejador
                            
                except Exception as e:
                    print(f"Error procesando mensaje de Ultravox: {e}")
                    
    except websockets.exceptions.ConnectionClosedError:
        print("Conexión WebSocket de Ultravox cerrada")
    except Exception as e:
        print(f"Error en handle_ultravox_messages: {e}")
        traceback.print_exc()

#
# Run app via Uvicorn
#
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
