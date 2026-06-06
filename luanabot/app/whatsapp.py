import os
import requests
import logging

log = logging.getLogger(__name__)

WAHA_URL = os.environ.get('WAHA_URL', 'https://evolution-api-production-634b.up.railway.app')
WAHA_API_KEY = os.environ.get('WAHA_API_KEY', 'waha123')
WAHA_SESSION = os.environ.get('WAHA_SESSION', 'default')


def enviar_mensagem(phone, texto):
    try:
        if '@' in str(phone):
            chat_id = phone
        else:
            chat_id = f"{phone}@lid"

        r = requests.post(
            f'{WAHA_URL}/api/sendText',
            headers={'X-Api-Key': WAHA_API_KEY, 'Content-Type': 'application/json'},
            json={'session': WAHA_SESSION, 'chatId': chat_id, 'text': texto},
            timeout=15
        )
        log.info(f'Enviado para {chat_id}: {r.status_code} {r.text[:100]}')
        if r.status_code not in [200, 201]:
            log.error(f'Erro enviar: {r.status_code} {r.text}')
            return False
        return True
    except Exception as e:
        log.error(f'Erro enviar mensagem: {e}')
        return False


def enviar_mensagem_com_botoes(phone, texto, botoes):
    return enviar_mensagem(phone, texto)
