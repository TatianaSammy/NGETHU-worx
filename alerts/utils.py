# alerts/utils.py
import os
import logging
import asyncio
import aiohttp
import subprocess
from functools import wraps
from django.forms.models import model_to_dict
from django.core.mail import send_mail
from django.conf import settings


logger = logging.getLogger(__name__)

def send_alert_email(subject: str, body: str, recipients: list[str], from_email: str = None, fail_silently=False):
    """
    Send an email alert to recipients.
    """
    from_email = from_email or settings.DEFAULT_FROM_EMAIL
    if not recipients:
        logger.warning("No recipients provided for send_alert_email")
        return

    try:
        send_mail(subject, body, from_email, recipients, fail_silently=fail_silently)
        logger.info(f"Sent alert email to {recipients}")
    except Exception as e:
        logger.exception(f"Failed to send alert email to {recipients}: {e}")


# =====================================================
# 🔹 Retry decorator with exponential backoff
# =====================================================
def async_retry(retries=3, backoff_in_seconds=1, exceptions=(Exception,)):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            delay = backoff_in_seconds
            for attempt in range(1, retries + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    if attempt == retries:
                        logger.error(f"{func.__name__} failed after {retries} attempts: {e}")
                        raise
                    logger.warning(f"{func.__name__} failed on attempt {attempt}. Retrying in {delay}s: {e}")
                    await asyncio.sleep(delay)
                    delay *= 2  # exponential backoff
        return wrapper
    return decorator

# =====================================================
# 🔹 SMS Sender (async + retry)
# =====================================================
class SMSSender:
    SMS_GATEWAY_URL = os.environ.get("SMS_GATEWAY_URL")
    SMS_API_KEY = os.environ.get("SMS_API_KEY")

    @staticmethod
    @async_retry(retries=3, backoff_in_seconds=2)
    async def send_sms(number: str, message: str):
        if not SMSSender.SMS_GATEWAY_URL:
            logger.warning(f"SMS gateway URL not configured. SMS to {number} skipped.")
            return
        payload = {"to": number, "message": message, "api_key": SMSSender.SMS_API_KEY}
        async with aiohttp.ClientSession() as session:
            async with session.post(SMSSender.SMS_GATEWAY_URL, json=payload) as resp:
                if resp.status == 200:
                    logger.info(f"Sent SMS to {number}: {message}")
                else:
                    text = await resp.text()
                    raise RuntimeError(f"Failed SMS to {number}: {resp.status}, {text}")

    @staticmethod
    async def send_bulk_sms(message, phone_numbers):
        await asyncio.gather(*(SMSSender.send_sms(num, message) for num in phone_numbers))


# =====================================================
# 🔹 WhatsApp Sender (async + retry)
# =====================================================
class WhatsAppSender:
    SIGNAL_CLI_PATH = os.environ.get("SIGNAL_CLI_PATH", "/usr/bin/signal-cli")
    SENDER_NUMBER = os.environ.get("SIGNAL_CLI_NUMBER")

    @classmethod
    @async_retry(retries=3, backoff_in_seconds=2)
    async def send_message(cls, number: str, message: str):
        if not cls.SENDER_NUMBER:
            logger.warning(f"Signal CLI number not configured. Message to {number} skipped.")
            return
        proc = await asyncio.create_subprocess_exec(
            cls.SIGNAL_CLI_PATH, "-u", cls.SENDER_NUMBER, "send", number, "-m", message,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"Failed WhatsApp message to {number}: {stderr.decode().strip()}")
        logger.info(f"WhatsApp message sent to {number}")

    @classmethod
    async def send_bulk_messages(cls, message, phone_numbers):
        await asyncio.gather(*(cls.send_message(num, message) for num in phone_numbers))


# =====================================================
# 🔹 Slack / Mattermost Notifier (async + retry)
# =====================================================
class SlackNotifier:
    WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "http://localhost:8065/hooks/your-mattermost-webhook-id")

    @classmethod
    @async_retry(retries=3, backoff_in_seconds=2)
    async def send_message(cls, channel: str, message: str):
        payload = {
            "channel": channel,
            "username": "AlertBot",
            "text": message,
            "icon_emoji": ":warning:"
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(cls.WEBHOOK_URL, json=payload) as resp:
                if resp.status == 200:
                    logger.info(f"Sent Slack/Mattermost message to {channel}")
                else:
                    text = await resp.text()
                    raise RuntimeError(f"Failed Slack/Mattermost message to {channel}: {resp.status}, {text}")

    @classmethod
    async def send_bulk_messages(cls, message, channels):
        await asyncio.gather(*(cls.send_message(channel, message) for channel in channels))


# =====================================================
# 🔹 AI Alert Priority Predictor
# =====================================================
class AIAlertPriorityPredictor:
    CRITICAL_KEYWORDS = ['failure', 'outage', 'critical', 'error', 'down']
    HIGH_KEYWORDS = ['delay', 'warning', 'slow']

    @staticmethod
    def predict(serialized_instance: dict) -> str:
        instance_data = ' '.join(str(value).lower() for value in serialized_instance.values())
        if any(k in instance_data for k in AIAlertPriorityPredictor.CRITICAL_KEYWORDS):
            return 'critical'
        elif any(k in instance_data for k in AIAlertPriorityPredictor.HIGH_KEYWORDS):
            return 'high'
        else:
            return 'medium'


# =====================================================
# 🔹 Safe Condition Evaluator
# =====================================================
class SafeConditionEvaluator:
    @staticmethod
    def validate_structure(json_field, field_name):
        if not isinstance(json_field, dict):
            raise ValueError(f"{field_name} must be a dictionary")

    @staticmethod
    def evaluate(conditions, instance_data):
        try:
            for condition in conditions.get('conditions', []):
                field = condition.get('field')
                operator = condition.get('operator')
                value = condition.get('value')

                if field not in instance_data:
                    return False
                instance_value = instance_data[field]

                if operator == '==' and str(instance_value) != str(value):
                    return False
                elif operator == '!=' and str(instance_value) == str(value):
                    return False
                elif operator == '>' and float(instance_value) <= float(value):
                    return False
                elif operator == '<' and float(instance_value) >= float(value):
                    return False
                elif operator == '>=' and float(instance_value) < float(value):
                    return False
                elif operator == '<=' and float(instance_value) > float(value):
                    return False
            return True
        except Exception as e:
            logger.error(f"Error evaluating conditions: {e}")
            return False


# =====================================================
# 🔹 Text-to-Speech Engine (async / Celery-friendly)
# =====================================================
class TextToSpeechEngine:
    @staticmethod
    async def generate_voice_notification(message, filename='notification.mp3', voice_over=None):
        try:
            from asgiref.sync import sync_to_async
            from alerts.models import VoiceOver, synthesize_alert_audio

            if voice_over is None:
                voice_over = await sync_to_async(VoiceOver.get_next_voice)()
            if voice_over is None:
                logger.error("No active VoiceOver profile is available for alert speech.")
                return None
            audio_bytes, _ = await asyncio.to_thread(
                synthesize_alert_audio,
                message,
                voice_over,
                voice_over.accent_profile,
                voice_over.speech_rate,
            )
            if not audio_bytes:
                return None
            output_path = filename
            if audio_bytes.startswith(b"RIFF") and audio_bytes[8:12] == b"WAVE":
                output_path = os.path.splitext(filename)[0] + ".wav"
            await asyncio.to_thread(_write_voice_file, output_path, audio_bytes)
            logger.info("Voice notification saved with profile %s", voice_over.name)
            return output_path
        except Exception as e:
            logger.error("Failed to generate profile-based voice notification: %s", e)
            return None

    @staticmethod
    async def generate_voice_notification_gtts(message, filename='notification.mp3', voice_over=None):
        return await TextToSpeechEngine.generate_voice_notification(message, filename, voice_over)

    @staticmethod
    async def speak_alert(message, voice_over=None):
        return await TextToSpeechEngine.generate_voice_notification(message, voice_over=voice_over)


def _write_voice_file(filename, audio_bytes):
    with open(filename, "wb") as audio_file:
        audio_file.write(audio_bytes)


# =====================================================
# 🔹 Instance Serializer
# =====================================================
class InstanceSerializer:
    @staticmethod
    def serialize(instance):
        try:
            return model_to_dict(instance)
        except Exception:
            return {'id': getattr(instance, 'id', None), 'str': str(instance)}


# =====================================================
# 🔹 Geo Context Analyzer (stub)
# =====================================================
class GeoContextAnalyzer:
    @staticmethod
    def detect(serialized_instance):
        return {"location": "unknown", "plant": "default"}


# =====================================================
# 🔹 AI Escalation Forecaster (stub)
# =====================================================
class AIEscalationForecaster:
    @staticmethod
    def predict_escalation_window(serialized_instance, priority):
        return {"forecast_hours": 1, "risk_score": 0.5}


# =====================================================
# 🔹 Legacy wrapper for backward compatibility
# =====================================================
def generate_voice_notification_pyttsx3(message, filename='notification.mp3', voice_over=None):
    return asyncio.run(TextToSpeechEngine.generate_voice_notification(message, filename, voice_over))
