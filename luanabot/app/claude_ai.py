import os
import logging
from groq import Groq
from datetime import datetime

log = logging.getLogger(__name__)

client = Groq(api_key=os.environ.get('GROQ_API_KEY', ''))

SYSTEM_PROMPT = """És o assistente financeiro pessoal da Luana, uma jovem portuguesa.
Respondes sempre em português europeu (não brasileiro), de forma amigável, direta e útil.
Conheces os dados financeiros dela:
- Salário: mensal, recebe dia 21
- Fixos: carro 350€, ordem assistentes sociais 20€, unhas 50€ (até setembro) depois 25€, conta conjunta com o Ruben 50€
- Carro: VW Taigo 1.0 gasolina
- Junho: subsídio de férias (mês especial)
- Novembro: subsídio de natal (mês especial)

Quando ela regista despesas, compras as com o mês anterior.
Quando ela recebe o salário, crias um plano financeiro completo.
Quando ela pergunta se pode comprar algo, analisas a situação financeira dela.
Mantém as respostas curtas e práticas — máximo 5 linhas.
Usa emojis com moderação."""


def processar_mensagem_ia(texto, usuario, contexto='geral'):
    try:
        mes_atual = datetime.now().month
        ano_atual = datetime.now().year
        nomes_mes = ['Janeiro','Fevereiro','Março','Abril','Maio','Junho','Julho','Agosto','Setembro','Outubro','Novembro','Dezembro']

        contexto_financeiro = f"""
Dados atuais da Luana:
- Mês: {nomes_mes[mes_atual-1]} {ano_atual}
- Salário: {usuario.salario_liquido or 'não registado'}€
- Fixos: carro {usuario.fixo_carro or 350}€, ordem {usuario.fixo_ordem or 20}€, unhas {usuario.fixo_unhas or 50}€, conjunta {usuario.fixo_conjunta or 50}€
"""

        resposta = client.chat.completions.create(
            model='llama-3.1-8b-instant',
            messages=[
                {'role': 'system', 'content': SYSTEM_PROMPT + contexto_financeiro},
                {'role': 'user', 'content': texto}
            ],
            max_tokens=300,
            temperature=0.7
        )
        return resposta.choices[0].message.content
    except Exception as e:
        log.error(f"Erro IA: {e}")
        return "🤔 Não percebi bem. Podes reformular? Exemplo: \"Gastei 25€ Continente\" ou \"Resumo\""


def ler_talao_imagem(base64_data, mimetype='image/jpeg'):
    try:
        import anthropic
        cliente_claude = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY', ''))

        resposta = cliente_claude.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=200,
            messages=[{
                'role': 'user',
                'content': [
                    {
                        'type': 'image',
                        'source': {
                            'type': 'base64',
                            'media_type': mimetype,
                            'data': base64_data
                        }
                    },
                    {
                        'type': 'text',
                        'text': 'Lê este talão/recibo e extrai: valor total, loja/estabelecimento. Responde APENAS com: "Gastei X€ em NOME_LOJA" em português. Se não conseguires ler, responde "não consigo ler".'
                    }
                ]
            }]
        )
        texto = resposta.content[0].text
        if 'não consigo' in texto.lower():
            return None
        return texto
    except Exception as e:
        log.error(f"Erro ler talão: {e}")
        return None


def extrair_texto_pdf_ia(base64_data):
    try:
        import anthropic
        cliente_claude = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY', ''))

        resposta = cliente_claude.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=300,
            messages=[{
                'role': 'user',
                'content': [
                    {
                        'type': 'document',
                        'source': {
                            'type': 'base64',
                            'media_type': 'application/pdf',
                            'data': base64_data
                        }
                    },
                    {
                        'type': 'text',
                        'text': 'Extrai o salário líquido deste recibo de vencimento. Responde APENAS com o número, exemplo: "1456.78". Se não encontrares, responde "0".'
                    }
                ]
            }]
        )
        return resposta.content[0].text.strip()
    except Exception as e:
        log.error(f"Erro extrair PDF IA: {e}")
        return '0'
