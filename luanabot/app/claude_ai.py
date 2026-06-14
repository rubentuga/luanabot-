import os
import logging
from datetime import datetime

log = logging.getLogger(__name__)

# Inicialização lazy — evita crash no arranque se groq tiver problemas
_groq_client = None

def get_groq():
    global _groq_client
    if _groq_client is None:
        from groq import Groq
        _groq_client = Groq(api_key=os.environ.get('GROQ_API_KEY', ''))
    return _groq_client

SYSTEM_PROMPT = """És o Zé das Finanças, assistente financeiro pessoal em português europeu.
Respondes sempre em PT-PT, de forma direta, informal e com personalidade.
Ruben (mano, direto) | Luana (querida, motivadora).
Máximo 4 linhas. Emojis com moderação."""


def processar_mensagem_ia(texto, usuario, contexto='geral'):
    try:
        mes_atual = datetime.now().month
        nomes_mes = ['Janeiro','Fevereiro','Março','Abril','Maio','Junho',
                     'Julho','Agosto','Setembro','Outubro','Novembro','Dezembro']
        contexto_fin = f"Utilizador: {getattr(usuario,'phone','?')} | Mês: {nomes_mes[mes_atual-1]}"
        resposta = get_groq().chat.completions.create(
            model='llama-3.3-70b-versatile',
            messages=[
                {'role': 'system', 'content': SYSTEM_PROMPT + '\n' + contexto_fin},
                {'role': 'user', 'content': texto}
            ],
            max_tokens=200, temperature=0.7)
        return resposta.choices[0].message.content
    except Exception as e:
        log.error(f"Erro IA: {e}")
        return "🤔 Não percebi. Tenta: 'gastei 20 no café' ou 'resumo'"


def ler_talao_imagem(base64_data, mimetype='image/jpeg'):
    try:
        import anthropic
        claude = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY', ''))
        r = claude.messages.create(
            model='claude-haiku-4-5-20251001', max_tokens=200,
            messages=[{'role':'user','content':[
                {'type':'image','source':{'type':'base64','media_type':mimetype,'data':base64_data}},
                {'type':'text','text':'Lê este talão. Extrai valor total e loja. Responde só: "Gastei X€ em LOJA". Se não conseguires: "não consigo ler".'}
            ]}])
        texto = r.content[0].text
        return None if 'não consigo' in texto.lower() else texto
    except Exception as e:
        log.error(f"Erro ler talão: {e}"); return None


def extrair_texto_pdf_ia(base64_data):
    try:
        import anthropic
        claude = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY', ''))
        r = claude.messages.create(
            model='claude-haiku-4-5-20251001', max_tokens=100,
            messages=[{'role':'user','content':[
                {'type':'document','source':{'type':'base64','media_type':'application/pdf','data':base64_data}},
                {'type':'text','text':'Extrai o salário líquido deste recibo. Responde SÓ com o número ex: "1456.78". Se não encontrares: "0".'}
            ]}])
        return r.content[0].text.strip()
    except Exception as e:
        log.error(f"Erro extrair PDF: {e}"); return '0'
