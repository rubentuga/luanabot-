import os
import requests
import logging

log = logging.getLogger(__name__)

EVOLUTION_URL = os.environ.get('EVOLUTION_URL', '')
EVOLUTION_API_KEY = os.environ.get('EVOLUTION_API_KEY', '')
EVOLUTION_INSTANCE = os.environ.get('EVOLUTION_INSTANCE', 'luana')


def enviar_mensagem(phone, texto):
    try:
        if not EVOLUTION_URL or not EVOLUTION_API_KEY:
            log.warning(f"WPP não configurado. Mensagem para {phone}: {texto[:50]}")
            return False

        url = f"{EVOLUTION_URL}/message/sendText/{EVOLUTION_INSTANCE}"
        headers = {'apikey': EVOLUTION_API_KEY, 'Content-Type': 'application/json'}
        payload = {
            "number": f"{phone}@s.whatsapp.net",
            "text": texto
        }
        r = requests.post(url, json=payload, headers=headers, timeout=15)
        if r.status_code not in [200, 201]:
            log.error(f"Erro enviar mensagem: {r.status_code} {r.text}")
            return False
        return True
    except Exception as e:
        log.error(f"Erro enviar mensagem: {e}")
        return False


def enviar_mensagem_com_botoes(phone, texto, botoes):
    try:
        url = f"{EVOLUTION_URL}/message/sendButtons/{EVOLUTION_INSTANCE}"
        headers = {'apikey': EVOLUTION_API_KEY, 'Content-Type': 'application/json'}
        payload = {
            "number": f"{phone}@s.whatsapp.net",
            "title": texto,
            "buttons": [{"buttonId": str(i), "buttonText": {"displayText": b}} for i, b in enumerate(botoes)]
        }
        r = requests.post(url, json=payload, headers=headers, timeout=15)
        return r.status_code in [200, 201]
    except Exception as e:
        log.error(f"Erro enviar botões: {e}")
        return enviar_mensagem(phone, texto)


def enviar_localizacao(phone, latitude, longitude, nome, endereco):
    try:
        url = f"{EVOLUTION_URL}/message/sendLocation/{EVOLUTION_INSTANCE}"
        headers = {'apikey': EVOLUTION_API_KEY, 'Content-Type': 'application/json'}
        payload = {
            "number": f"{phone}@s.whatsapp.net",
            "latitude": latitude,
            "longitude": longitude,
            "name": nome,
            "address": endereco
        }
        r = requests.post(url, json=payload, headers=headers, timeout=15)
        return r.status_code in [200, 201]
    except Exception as e:
        log.error(f"Erro enviar localização: {e}")
        return False
