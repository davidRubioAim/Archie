"""v1.08: cliente de Gemini (google-genai) en un solo sitio.

Por qué: con algunas versiones del SDK / del endpoint, el cliente acaba en la API "v1", que NO admite
system_instruction ("Developer instruction is not enabled for api version v1. Use api version v1beta
instead."). El chat de Archie usa system_instruction, response_mime_type y thinking_config: todo eso es
de v1beta, así que la versión se fija aquí explícitamente y no depende del SDK instalado en cada equipo.
"""
import os

from google import genai
from google.genai import types

from log_utils import debug

API_VERSION = "v1beta"
MODELO_CHAT = os.getenv("ARCHIE_AI_MODEL", "gemini-2.5-flash")


def crear_cliente(api_key):
    """genai.Client contra la Gemini API (no Vertex) en v1beta."""
    return genai.Client(api_key=api_key, http_options=types.HttpOptions(api_version=API_VERSION))


def config_chat(system_instruction, json_mode=True):
    """GenerateContentConfig del chat. v1.05: gemini-2.5-flash "piensa" por defecto (varios segundos por
    mensaje) y para devolver un JSON de parámetros no hace falta: presupuesto 0 por defecto.
    ARCHIE_AI_THINKING=-1 (dinámico) o N tokens en Config/.env. (thinking_budget=0 es válido en
    gemini-2.5-flash y flash-lite; gemini-2.5-pro no lo admite: ahí se pone -1.)"""
    cfg = dict(system_instruction=system_instruction)
    if json_mode:
        cfg["response_mime_type"] = "application/json"
    try:
        presupuesto = int(os.getenv("ARCHIE_AI_THINKING", "0") or 0)
        if presupuesto == 0 and "pro" in MODELO_CHAT.lower():
            presupuesto = -1
        cfg["thinking_config"] = types.ThinkingConfig(thinking_budget=presupuesto)
    except Exception as e:
        debug(f"Gemini sin thinking_config: {e}")
    return types.GenerateContentConfig(**cfg)
