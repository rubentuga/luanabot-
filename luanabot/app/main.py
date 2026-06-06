import os
import json
import logging
import re
import base64
import tempfile
from datetime import datetime, timedelta
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Lisbon")
except Exception:
    TZ = None
from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from models import db, Usuario, Despesa, Receita, DespesaFutura, ObjetivoFinanceiro, FundoEmergencia
from whatsapp import enviar_mensagem, enviar_mensagem_com_botoes
from claude_ai import processar_mensagem_ia
from pdf_reader import extrair_salario_pdf

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///luana.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(app)

OWNER_PHONE = os.environ.get('OWNER_PHONE', '')
WAHA_URL = os.environ.get('WAHA_URL', 'https://evolution-api-production-634b.up.railway.app')
WAHA_API_KEY = os.environ.get('WAHA_API_KEY', 'waha123')
WAHA_SESSION = os.environ.get('WAHA_SESSION', 'default')
GROQ_API_KEY = os.environ.get('GROQ_API_KEY', '')
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')

# Orçamento fixo para gastar
GASTAR_NORMAL = 200
GASTAR_SUBSIDIO = 400  # junho e novembro
FUNDO_PCT = 0.05
BASE_COMBUSTIVEL = 50  # referência mensal

scheduler = BackgroundScheduler()


def agora():
    return datetime.now(TZ) if TZ else datetime.now()


# ─── ABREVIAÇÕES E CATEGORIAS ────────────────────────────────
LOJAS = {
    # Fast food
    'bk': ('fastfood', '🍔', 'Burger King'),
    'burger king': ('fastfood', '🍔', 'Burger King'),
    'mac': ('fastfood', '🍔', "McDonald's"),
    'mc': ('fastfood', '🍔', "McDonald's"),
    'mcd': ('fastfood', '🍔', "McDonald's"),
    'mcdonald': ('fastfood', '🍔', "McDonald's"),
    "mcdonald's": ('fastfood', '🍔', "McDonald's"),
    'mcdonalds': ('fastfood', '🍔', "McDonald's"),
    'kfc': ('fastfood', '🍗', 'KFC'),
    'sbx': ('fastfood', '☕', 'Starbucks'),
    'starbucks': ('fastfood', '☕', 'Starbucks'),
    'sub': ('fastfood', '🥪', 'Subway'),
    'subway': ('fastfood', '🥪', 'Subway'),
    'telepizza': ('fastfood', '🍕', 'Telepizza'),
    'dominos': ('fastfood', '🍕', "Domino's"),
    'pizza hut': ('fastfood', '🍕', 'Pizza Hut'),
    # Restaurante
    'zen': ('restaurante', '🍣', 'Zen Sushi'),
    'zen-sushi': ('restaurante', '🍣', 'Zen Sushi'),
    'zen sushi': ('restaurante', '🍣', 'Zen Sushi'),
    'sushi': ('restaurante', '🍣', 'Sushi'),
    'alcochete': ('restaurante', '🍣', 'Sushi Alcochete'),
    'restaurante': ('restaurante', '🍽️', 'Restaurante'),
    'jantar': ('restaurante', '🍽️', 'Jantar'),
    'almoco': ('restaurante', '🍽️', 'Almoço'),
    'almoço': ('restaurante', '🍽️', 'Almoço'),
    'cafe': ('restaurante', '☕', 'Café'),
    'café': ('restaurante', '☕', 'Café'),
    'kebab': ('restaurante', '🥙', 'Kebab'),
    'tasca': ('restaurante', '🍽️', 'Tasca'),
    # Roupa / sneakers
    'foot': ('roupa', '👟', 'Foot Locker'),
    'fl': ('roupa', '👟', 'Foot Locker'),
    'foot locker': ('roupa', '👟', 'Foot Locker'),
    'jd': ('roupa', '👟', 'JD Sports'),
    'jd sports': ('roupa', '👟', 'JD Sports'),
    'snipes': ('roupa', '👟', 'Snipes'),
    'zara': ('roupa', '👗', 'Zara'),
    'z': ('roupa', '👗', 'Zara'),
    'hm': ('roupa', '👗', 'H&M'),
    'h&m': ('roupa', '👗', 'H&M'),
    'primark': ('roupa', '👗', 'Primark'),
    'shein': ('roupa', '👗', 'Shein'),
    'bershka': ('roupa', '👗', 'Bershka'),
    'pull&bear': ('roupa', '👗', 'Pull&Bear'),
    'stradivarius': ('roupa', '👗', 'Stradivarius'),
    'nike': ('roupa', '👟', 'Nike'),
    'nk': ('roupa', '👟', 'Nike'),
    'nke': ('roupa', '👟', 'Nike'),
    'adidas': ('roupa', '👟', 'Adidas'),
    'ads': ('roupa', '👟', 'Adidas'),
    'adi': ('roupa', '👟', 'Adidas'),
    # Tecnologia
    'apl': ('tecnologia', '🍎', 'Apple'),
    'apple': ('tecnologia', '🍎', 'Apple'),
    'sam': ('tecnologia', '📱', 'Samsung'),
    'smg': ('tecnologia', '📱', 'Samsung'),
    'samsung': ('tecnologia', '📱', 'Samsung'),
    'ps': ('tecnologia', '🎮', 'PlayStation'),
    'psn': ('tecnologia', '🎮', 'PlayStation'),
    'playstation': ('tecnologia', '🎮', 'PlayStation'),
    'xb': ('tecnologia', '🎮', 'Xbox'),
    'xbx': ('tecnologia', '🎮', 'Xbox'),
    'xbox': ('tecnologia', '🎮', 'Xbox'),
    'wrt': ('tecnologia', '💻', 'Worten'),
    'worten': ('tecnologia', '💻', 'Worten'),
    'rp': ('tecnologia', '💻', 'Radio Popular'),
    'radio popular': ('tecnologia', '💻', 'Radio Popular'),
    # Supermercado
    'conti': ('supermercado', '🛒', 'Continente'),
    'cnt': ('supermercado', '🛒', 'Continente'),
    'continente': ('supermercado', '🛒', 'Continente'),
    'pd': ('supermercado', '🛒', 'Pingo Doce'),
    'pingo doce': ('supermercado', '🛒', 'Pingo Doce'),
    'pingo': ('supermercado', '🛒', 'Pingo Doce'),
    'lidl': ('supermercado', '🛒', 'Lidl'),
    'aldi': ('supermercado', '🛒', 'Aldi'),
    'mercadona': ('supermercado', '🛒', 'Mercadona'),
    'minipreco': ('supermercado', '🛒', 'Minipreço'),
    'intermarche': ('supermercado', '🛒', 'Intermarché'),
    'ik': ('supermercado', '🛋️', 'IKEA'),
    'ikea': ('supermercado', '🛋️', 'IKEA'),
    'compras': ('supermercado', '🛒', 'Compras'),
    'comida': ('supermercado', '🛒', 'Compras'),
    # Combustível
    'bp': ('combustivel', '⛽', 'BP'),
    'galp': ('combustivel', '⛽', 'Galp'),
    'repsol': ('combustivel', '⛽', 'Repsol'),
    'shell': ('combustivel', '⛽', 'Shell'),
    'prio': ('combustivel', '⛽', 'Prio'),
    'cepsa': ('combustivel', '⛽', 'Cepsa'),
    'gasolina': ('combustivel', '⛽', 'Gasolina'),
    'gasoleo': ('combustivel', '⛽', 'Gasóleo'),
    'combustivel': ('combustivel', '⛽', 'Combustível'),
    # Gota
    'gota': ('gota', '🧃', 'Gota'),
    'agua': ('gota', '🧃', 'Água'),
    'água': ('gota', '🧃', 'Água'),
    # Saúde
    'farmacia': ('saude', '💊', 'Farmácia'),
    'farmácia': ('saude', '💊', 'Farmácia'),
    'wells': ('saude', '💊', 'Wells'),
    'dentista': ('saude', '🦷', 'Dentista'),
    'medico': ('saude', '🏥', 'Médico'),
    'consulta': ('saude', '🏥', 'Consulta'),
    # Unhas / pessoal
    'unhas': ('pessoal', '💅', 'Unhas'),
    'cabelo': ('pessoal', '💇', 'Cabelo'),
    'cabeleireiro': ('pessoal', '💇', 'Cabeleireiro'),
    'estetica': ('pessoal', '💅', 'Estética'),
    # Carro
    'oficina': ('carro', '🔧', 'Oficina'),
    'mecanico': ('carro', '🔧', 'Mecânico'),
    'seguro': ('carro', '🚗', 'Seguro'),
    'portagem': ('carro', '🛣️', 'Portagem'),
    'via verde': ('carro', '🛣️', 'Via Verde'),
    'estacionamento': ('carro', '🅿️', 'Estacionamento'),
    # Lazer
    'cinema': ('lazer', '🎬', 'Cinema'),
    'concerto': ('lazer', '🎵', 'Concerto'),
    'bowling': ('lazer', '🎳', 'Bowling'),
    # Subscrições
    'netflix': ('subscricoes', '📺', 'Netflix'),
    'spotify': ('subscricoes', '🎵', 'Spotify'),
    'disney': ('subscricoes', '📺', 'Disney+'),
    'hbo': ('subscricoes', '📺', 'HBO'),
}

EMOJI_CAT = {
    'fastfood': '🍔', 'restaurante': '🍽️', 'roupa': '👗', 'tecnologia': '📱',
    'supermercado': '🛒', 'combustivel': '⛽', 'gota': '🧃', 'saude': '💊',
    'pessoal': '💅', 'carro': '🚗', 'lazer': '🎭', 'subscricoes': '📺', 'outros': '💳',
}


def categorizar(texto):
    t = texto.lower()
    # Procura palavra a palavra (match exato de tokens para abreviações curtas)
    tokens = re.findall(r"[a-zà-ú&']+", t)
    # Primeiro tenta match de expressões compostas
    for chave, val in LOJAS.items():
        if ' ' in chave and chave in t:
            return val
    # Depois match de tokens
    for tok in tokens:
        if tok in LOJAS:
            return LOJAS[tok]
    # Match parcial (palavra contida)
    for chave, val in LOJAS.items():
        if ' ' not in chave and len(chave) > 3 and chave in t:
            return val
    return ('outros', '💳', 'Gasto')


# ─── WEBHOOK ─────────────────────────────────────────────────
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.json
        if not data:
            return jsonify({'status': 'ok'})

        event = data.get('event', '')
        if event not in ['message', 'messages.upsert', '']:
            return jsonify({'status': 'ok'})

        payload = data.get('payload', data)

        from_me = payload.get('fromMe', False)
        msg_id = payload.get('id', '')
        if from_me or (isinstance(msg_id, str) and msg_id.startswith('true_')):
            return jsonify({'status': 'ok'})

        from_field = payload.get('from', '') or payload.get('chatId', '')
        phone_raw = from_field  # mantém @lid para responder
        phone = from_field.replace('@c.us', '').replace('@s.whatsapp.net', '').replace('@g.us', '').replace('@lid', '').split('@')[0]

        if not phone:
            return jsonify({'status': 'ok'})

        owner_phones = [p.strip() for p in OWNER_PHONE.split(',')] if OWNER_PHONE else []
        if owner_phones and phone not in owner_phones:
            return jsonify({'status': 'ok'})

        # Áudio
        media = payload.get('media')
        has_media = payload.get('hasMedia', False)
        body = payload.get('body', '')

        if isinstance(body, dict):
            texto = body.get('text', '') or body.get('conversation', '')
        else:
            texto = str(body) if body else ''

        if not texto and has_media and media:
            mime = media.get('mimetype', '')
            url = media.get('url', '')
            if 'audio' in mime or 'ogg' in mime:
                texto = transcrever_audio(url)
                if texto:
                    enviar_mensagem(phone_raw, f'🎤 Percebi: "{texto}"')
            elif 'image' in mime:
                resultado = ler_foto_talao(url)
                if resultado:
                    texto = resultado
                    enviar_mensagem(phone_raw, f'📸 Li o talão: {texto}')

        if not texto:
            return jsonify({'status': 'ok'})

        log.info(f"Mensagem de {phone}: {texto}")
        with app.app_context():
            processar_texto(phone_raw, phone, texto)

    except Exception as e:
        log.error(f'Erro webhook: {e}', exc_info=True)

    return jsonify({'status': 'ok'})


@app.route('/', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'bot': 'Ze das Financas'})


# ─── ÁUDIO (Groq Whisper) ────────────────────────────────────
def transcrever_audio(url):
    try:
        import requests
        from groq import Groq
        # URL interno do WAHA -> usa URL público
        if 'localhost' in url:
            url = url.replace('http://localhost:8080', WAHA_URL)
        r = requests.get(url, headers={'X-Api-Key': WAHA_API_KEY}, timeout=30)
        if r.status_code != 200:
            return ''
        with tempfile.NamedTemporaryFile(suffix='.ogg', delete=False) as f:
            f.write(r.content)
            fname = f.name
        client = Groq(api_key=GROQ_API_KEY)
        with open(fname, 'rb') as af:
            t = client.audio.transcriptions.create(model='whisper-large-v3', file=af, language='pt')
        return t.text.strip()
    except Exception as e:
        log.error(f'Erro audio: {e}')
        return ''


# ─── FOTO (Claude Vision) ────────────────────────────────────
def ler_foto_talao(url):
    try:
        import requests
        import anthropic
        if 'localhost' in url:
            url = url.replace('http://localhost:8080', WAHA_URL)
        r = requests.get(url, headers={'X-Api-Key': WAHA_API_KEY}, timeout=30)
        if r.status_code != 200:
            return ''
        img_b64 = base64.b64encode(r.content).decode()
        cliente = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = cliente.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=100,
            messages=[{'role': 'user', 'content': [
                {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/jpeg', 'data': img_b64}},
                {'type': 'text', 'text': 'Le este talao. Responde APENAS: "X euros NOME_LOJA". Se nao conseguires, responde "erro".'}
            ]}]
        )
        txt = resp.content[0].text.strip()
        return '' if 'erro' in txt.lower() else txt
    except Exception as e:
        log.error(f'Erro foto: {e}')
        return ''


# ─── PROCESSAR TEXTO ─────────────────────────────────────────
def processar_texto(phone_raw, phone, texto):
    usuario = Usuario.query.filter_by(phone=phone).first()
    if not usuario:
        usuario = Usuario(phone=phone, nome='Luana')
        db.session.add(usuario)
        db.session.commit()

    t = texto.lower().strip()

    if any(p in t for p in ['quem criou', 'quem te fez', 'quem te criou', 'teu criador', 'quem é o teu', 'quem foi que te']):
        enviar_mensagem(phone_raw, "Fui criado pelo tuga27 🚀\nO mesmo genio por tras do Zeflix (plataforma de streaming de filmes e series) e agora tambem do teu gestor financeiro pessoal. Sortuda! 😎")
        return

    if any(p in t for p in ['ajuda', 'help', '/start', 'comandos', 'o que fazes', 'o que sabes']):
        enviar_ajuda(phone_raw)
        return

    if t in ['ola', 'olá', 'oi', 'boas', 'hey'] or 'bom dia' in t or 'boa tarde' in t or 'boa noite' in t:
        enviar_boas_vindas(phone_raw)
        return

    if 'estou teso' in t or 'tou teso' in t or 'sem dinheiro' in t or 'liso' in t:
        modo_teso(phone_raw, usuario)
        return

    if 'gasolina mais barata' in t or 'posto mais barato' in t or 'gasolina barata' in t:
        gasolina_barata(phone_raw, t)
        return

    if any(p in t for p in ['quanto tenho', 'quanto me resta', 'quanto sobra', 'saldo']):
        enviar_quanto_tenho(phone_raw, usuario)
        return

    if 'conjunta' in t and any(p in t for p in ['quanto', 'tenho', 'sobra', 'resta']):
        enviar_conjunta(phone_raw, usuario)
        return

    if any(p in t for p in ['resumo', 'como estou', 'quanto gastei', 'situacao', 'situação']):
        enviar_resumo(phone_raw, usuario)
        return

    if any(p in t for p in ['plano', 'transferencia', 'transferência', 'distribuicao', 'distribuição']):
        enviar_plano_mes(phone_raw, usuario)
        return

    if 'score' in t or 'nota' in t or 'pontuacao' in t or 'pontuação' in t:
        enviar_score(phone_raw, usuario)
        return

    if any(p in t for p in ['poupar para', 'quero poupar', 'objetivo', 'objectivo']):
        resposta = processar_mensagem_ia(texto, usuario, 'objetivo')
        enviar_mensagem(phone_raw, resposta)
        return

    if any(p in t for p in ['mes que vem', 'mês que vem', 'proximo mes', 'próximo mês', 'para o mes', 'futuro']) and tem_numero(texto):
        processar_despesa_futura(phone_raw, usuario, texto)
        return

    if any(p in t for p in ['dentista', 'seguro', 'inspecao', 'inspeção']) and any(p in t for p in ['mes que vem', 'mês que vem', 'proximo', 'próximo', 'futuro']) and tem_numero(texto):
        processar_despesa_futura(phone_raw, usuario, texto)
        return

    if any(p in t for p in ['posso comprar', 'posso gastar', 'vale a pena', 'consigo comprar', 'devo comprar']):
        simular_compra(phone_raw, usuario, texto)
        return

    if any(p in t for p in ['recebi', 'ordenado', 'salario', 'salário', 'recibo', 'vencimento']) and tem_numero(texto):
        processar_receita(phone_raw, usuario, texto)
        return

    # GASTO — deteta número + contexto de gasto (com ou sem €)
    if tem_numero(texto) and eh_gasto(texto):
        processar_despesa(phone_raw, usuario, texto)
        return

    # IA para o resto
    resposta = perguntar_ia(texto, usuario)
    enviar_mensagem(phone_raw, resposta)


def tem_numero(texto):
    return bool(re.search(r'[0-9]+[.,]?[0-9]*', texto))


def extrair_valor(texto):
    matches = re.findall(r'[0-9]+[.,]?[0-9]*', texto)
    return float(matches[0].replace(',', '.')) if matches else 0


def eh_gasto(texto):
    t = texto.lower()
    # Palavras que indicam gasto
    verbos = ['gastei', 'paguei', 'comprei', 'gasto', 'almocei', 'jantei', 'foi', 'custou', 'meti', 'abasteci', 'lanchei', 'fiz']
    if any(v in t for v in verbos):
        return True
    if '€' in t or 'euro' in t:
        return True
    # Se tem uma loja conhecida + número
    cat, _, _ = categorizar(texto)
    if cat != 'outros':
        return True
    return False


# ─── PROCESSAR DESPESA ────────────────────────────────────────
def processar_despesa(phone_raw, usuario, texto):
    valor = extrair_valor(texto)
    if valor == 0:
        enviar_mensagem(phone_raw, "Nao percebi o valor 🤔 Diz tipo: gastei 25 no continente")
        return

    categoria, emoji, nome_loja = categorizar(texto)

    # Verifica se é gasto na conjunta
    na_conjunta = 'conjunta' in texto.lower()

    despesa = Despesa(
        usuario_id=usuario.id, valor=valor, categoria=categoria,
        descricao=('[conjunta] ' if na_conjunta else '') + texto[:90], data=agora().replace(tzinfo=None)
    )
    db.session.add(despesa)
    db.session.commit()

    mes_atual = agora().month
    ano_atual = agora().year
    mes_ant = mes_atual - 1 if mes_atual > 1 else 12
    ano_ant = ano_atual if mes_atual > 1 else ano_atual - 1

    total_cat = db.session.query(db.func.sum(Despesa.valor)).filter(
        Despesa.usuario_id == usuario.id, Despesa.categoria == categoria,
        db.extract('month', Despesa.data) == mes_atual, db.extract('year', Despesa.data) == ano_atual
    ).scalar() or 0

    total_cat_ant = db.session.query(db.func.sum(Despesa.valor)).filter(
        Despesa.usuario_id == usuario.id, Despesa.categoria == categoria,
        db.extract('month', Despesa.data) == mes_ant, db.extract('year', Despesa.data) == ano_ant
    ).scalar() or 0

    # Comentario com personalidade
    extra = ''
    # Conta quantas vezes esta categoria esta semana
    inicio_semana = agora().replace(tzinfo=None) - timedelta(days=agora().weekday())
    vezes_semana = db.session.query(db.func.count(Despesa.id)).filter(
        Despesa.usuario_id == usuario.id, Despesa.categoria == categoria,
        Despesa.data >= inicio_semana
    ).scalar() or 0

    if categoria == 'fastfood' and vezes_semana >= 3:
        extra = f'\n😏 Ja e a {vezes_semana}a vez de fast food esta semana, hein!'
    elif categoria == 'gota' and total_cat > 30:
        extra = f'\n🧃 Ja vais em {total_cat:.0f} euros de gota este mes... abranda nas bebidas!'
    elif categoria == 'restaurante' and agora().weekday() in [4, 5] and agora().hour >= 19:
        extra = '\n🍻 Sexta/sabado a noite, la vem o gasto do costume!'
    elif total_cat_ant > 0 and total_cat > total_cat_ant * 1.3:
        extra = f'\n⚠️ Ja gastaste mais em {categoria} que o mes passado todo ({total_cat_ant:.0f} euros)!'
    elif total_cat_ant > 0 and total_cat < total_cat_ant * 0.7:
        extra = f'\n✅ Estas a gastar menos em {categoria} que o mes passado. Bora!'

    disp = calcular_disponivel(usuario)
    conjunta_txt = ' (na conjunta 💑)' if na_conjunta else ''

    msg = f"{emoji} Bora, registado! {nome_loja} - {valor:.2f} euros{conjunta_txt}\n{categoria.capitalize()} este mes: {total_cat:.2f} euros{extra}\n💚 Ainda tens {disp:.2f} euros para gastar"
    enviar_mensagem(phone_raw, msg)


# ─── CÁLCULOS ────────────────────────────────────────────────
def calcular_disponivel(usuario):
    mes_atual = agora().month
    ano_atual = agora().year
    receita = db.session.query(db.func.sum(Receita.valor)).filter(
        Receita.usuario_id == usuario.id,
        db.extract('month', Receita.data) == mes_atual, db.extract('year', Receita.data) == ano_atual
    ).scalar() or usuario.salario_liquido or 0
    gastos_pessoais = db.session.query(db.func.sum(Despesa.valor)).filter(
        Despesa.usuario_id == usuario.id,
        db.extract('month', Despesa.data) == mes_atual, db.extract('year', Despesa.data) == ano_atual,
        ~Despesa.descricao.like('[conjunta]%')
    ).scalar() or 0
    gastar = GASTAR_SUBSIDIO if mes_atual in [6, 11] else GASTAR_NORMAL
    return gastar - gastos_pessoais


def calcular_plano(salario):
    mes_atual = agora().month
    fixo_carro = 350
    fixo_ordem = 20
    fixo_conjunta = 50
    fixo_unhas = 50 if mes_atual <= 9 else 25
    total_fixos = fixo_carro + fixo_ordem + fixo_conjunta + fixo_unhas
    fundo = round(salario * FUNDO_PCT, 2)
    gastar = GASTAR_SUBSIDIO if mes_atual in [6, 11] else GASTAR_NORMAL
    poupanca = round(salario - total_fixos - fundo - gastar, 2)
    return {
        'salario': salario, 'fixos': total_fixos, 'carro': fixo_carro,
        'ordem': fixo_ordem, 'unhas': fixo_unhas, 'conjunta': fixo_conjunta,
        'fundo': fundo, 'gastar': gastar, 'poupanca': poupanca,
        'subsidio': mes_atual in [6, 11]
    }


# ─── RECEITA / PLANO ─────────────────────────────────────────
def processar_receita(phone_raw, usuario, texto):
    valor = extrair_valor(texto)
    if valor == 0:
        enviar_mensagem(phone_raw, "Quanto recebeste? 💰")
        return
    usuario.salario_liquido = valor
    db.session.add(Receita(usuario_id=usuario.id, valor=valor, descricao='Salario', data=agora().replace(tzinfo=None)))
    db.session.commit()
    enviar_plano_salario(phone_raw, usuario, valor)


def enviar_plano_salario(phone_raw, usuario, salario):
    p = calcular_plano(salario)
    futuras = DespesaFutura.query.filter(DespesaFutura.usuario_id == usuario.id, DespesaFutura.pago == False).all()
    total_fut = sum(d.valor_reserva_mensal for d in futuras)
    poupanca_final = p['poupanca'] - total_fut

    msg = f"💰 Boa, recebeste {salario:.2f} euros!\n\n📋 Aqui esta o plano:\n"
    msg += f"🏠 Fixos: {p['fixos']:.0f} euros\n   Carro {p['carro']:.0f} | Ordem {p['ordem']:.0f} | Unhas {p['unhas']:.0f} | Conjunta {p['conjunta']:.0f}\n"
    msg += f"🛡️ Fundo emergencia: {p['fundo']:.2f} euros (mete no Revolut p/ nao mexeres)\n"
    msg += f"💳 Para gastar: {p['gastar']:.0f} euros\n"
    if total_fut > 0:
        msg += f"📅 Reserva despesas futuras: {total_fut:.2f} euros\n"
        for d in futuras:
            msg += f"   {d.descricao}: {d.valor_reserva_mensal:.0f} euros\n"
    msg += f"💎 Poupanca: {poupanca_final:.2f} euros 🔥"

    if p['subsidio']:
        msg += "\n\n🌴 Mes de subsidio! Meti mais margem p/ gastares (roupa, ferias...). Aproveita mas com juizo 😉"

    enviar_mensagem(phone_raw, msg)


# ─── QUANTO TENHO ────────────────────────────────────────────
def enviar_quanto_tenho(phone_raw, usuario):
    disp = calcular_disponivel(usuario)
    p = calcular_plano(usuario.salario_liquido or 0)
    if disp < 0:
        msg = f"😬 Ja passaste o orcamento em {abs(disp):.2f} euros este mes!\nA partir daqui e do fundo ou da poupanca... cuidado!"
    elif disp < 30:
        msg = f"💸 Tens so {disp:.2f} euros para gastar ate ao fim do mes. Aperta o cinto!"
    else:
        msg = f"💚 Tens {disp:.2f} euros para gastar este mes 😎\n🛡️ Fundo: {p['fundo']:.0f} euros | 💎 Poupanca prevista: {p['poupanca']:.0f} euros"
    enviar_mensagem(phone_raw, msg)


def enviar_conjunta(phone_raw, usuario):
    mes_atual = agora().month
    ano_atual = agora().year
    gasto_conjunta = db.session.query(db.func.sum(Despesa.valor)).filter(
        Despesa.usuario_id == usuario.id,
        db.extract('month', Despesa.data) == mes_atual, db.extract('year', Despesa.data) == ano_atual,
        Despesa.descricao.like('[conjunta]%')
    ).scalar() or 0
    # Conjunta = 50 teu + 50 dela = 100 total
    total_conjunta = 100
    resta = total_conjunta - gasto_conjunta
    msg = f"💑 Conjunta (jantares, lanches, cinema...):\n💰 Total: {total_conjunta:.0f} euros (50 teu + 50 Ruben)\n🛒 Ja gastaram: {gasto_conjunta:.2f} euros\n💚 Resta: {resta:.2f} euros"
    enviar_mensagem(phone_raw, msg)


# ─── RESUMO ──────────────────────────────────────────────────
def enviar_resumo(phone_raw, usuario):
    mes_atual = agora().month
    ano_atual = agora().year
    receita = db.session.query(db.func.sum(Receita.valor)).filter(
        Receita.usuario_id == usuario.id,
        db.extract('month', Receita.data) == mes_atual, db.extract('year', Receita.data) == ano_atual
    ).scalar() or usuario.salario_liquido or 0

    gastos_pessoais = db.session.query(db.func.sum(Despesa.valor)).filter(
        Despesa.usuario_id == usuario.id,
        db.extract('month', Despesa.data) == mes_atual, db.extract('year', Despesa.data) == ano_atual,
        ~Despesa.descricao.like('[conjunta]%')
    ).scalar() or 0

    gastos_conjunta = db.session.query(db.func.sum(Despesa.valor)).filter(
        Despesa.usuario_id == usuario.id,
        db.extract('month', Despesa.data) == mes_atual, db.extract('year', Despesa.data) == ano_atual,
        Despesa.descricao.like('[conjunta]%')
    ).scalar() or 0

    por_cat = db.session.query(Despesa.categoria, db.func.sum(Despesa.valor)).filter(
        Despesa.usuario_id == usuario.id,
        db.extract('month', Despesa.data) == mes_atual, db.extract('year', Despesa.data) == ano_atual
    ).group_by(Despesa.categoria).all()

    nomes = ['Janeiro','Fevereiro','Marco','Abril','Maio','Junho','Julho','Agosto','Setembro','Outubro','Novembro','Dezembro']
    disp = calcular_disponivel(usuario)

    msg = f"📊 Resumo de {nomes[mes_atual-1]}\n\n💰 Recebeste: {receita:.0f} euros\n🛒 Gastos teus: {gastos_pessoais:.2f} euros\n💑 Na conjunta: {gastos_conjunta:.2f} euros\n💚 Para gastar ainda: {disp:.2f} euros\n\n📈 Por categoria:"
    for cat, total in sorted(por_cat, key=lambda x: -x[1]):
        msg += f"\n{EMOJI_CAT.get(cat,'💳')} {cat.capitalize()}: {total:.2f} euros"

    # Previsao fim de mes
    dia = agora().day
    if dia > 3 and gastos_pessoais > 0:
        ritmo = gastos_pessoais / dia * 30
        msg += f"\n\n🔮 A este ritmo acabas o mes com ~{ritmo:.0f} euros de gastos"

    enviar_mensagem(phone_raw, msg)


def enviar_plano_mes(phone_raw, usuario):
    if not usuario.salario_liquido:
        enviar_mensagem(phone_raw, "Ainda nao sei o teu salario 🤔 Diz: recebi 1300 euros")
        return
    enviar_plano_salario(phone_raw, usuario, usuario.salario_liquido)


# ─── SCORE ───────────────────────────────────────────────────
def enviar_score(phone_raw, usuario):
    disp = calcular_disponivel(usuario)
    score = 10
    if disp < 0: score -= 5
    elif disp < 30: score -= 2
    emoji = '🏆' if score >= 8 else '👍' if score >= 6 else '😬'
    txt = 'Mestre da poupanca!' if score >= 8 else 'Vais bem!' if score >= 6 else 'Cuidado com os gastos!'
    enviar_mensagem(phone_raw, f"{emoji} Score financeiro: {score}/10\n{txt}")


# ─── MODO TESO ───────────────────────────────────────────────
def modo_teso(phone_raw, usuario):
    disp = calcular_disponivel(usuario)
    dias_ate_salario = (21 - agora().day) % 30
    msg = f"😅 Modo 'estou teso' ativado!\n\n💚 Tens {disp:.2f} euros\n📅 Faltam ~{dias_ate_salario} dias para o salario\n\nDicas:\n🍳 Cozinha em casa, evita take-away\n🚶 Anda a pe quando der\n🛒 So o essencial nas compras\n💪 Tu consegues, aguenta!"
    enviar_mensagem(phone_raw, msg)


# ─── GASOLINA ────────────────────────────────────────────────
def gasolina_barata(phone_raw, texto):
    t = texto.lower()
    if 'barreiro' in t:
        zona = 'Barreiro'
    elif 'moita' in t:
        zona = 'Moita'
    else:
        zona = 'Barreiro/Moita'
    enviar_mensagem(phone_raw, f"⛽ Para veres os postos mais baratos em {zona} em tempo real:\nhttps://precoscombustiveis.dgeg.gov.pt\n\nDica: o Prio e o Intermarche costumam ser dos mais baratos na zona! 💡")


# ─── DESPESA FUTURA ──────────────────────────────────────────
def processar_despesa_futura(phone_raw, usuario, texto):
    valor = extrair_valor(texto)
    if valor == 0:
        enviar_mensagem(phone_raw, "Quanto vai custar? Diz tipo: mes que vem dentista 40 euros")
        return
    t = texto.lower()
    if 'dentista' in t: desc = 'Dentista'
    elif 'seguro' in t: desc = 'Seguro'
    elif 'inspe' in t: desc = 'Inspecao'
    elif 'renda' in t: desc = 'Renda'
    else:
        palavras = [w for w in texto.split() if not re.match(r'[0-9€,.]', w) and len(w) > 3 and w.lower() not in ['mes', 'mês', 'que', 'vem', 'tenho', 'proximo', 'próximo']]
        desc = ' '.join(palavras[:2]).capitalize() if palavras else 'Despesa futura'
    meses = 2 if ('2 meses' in t or 'dois meses' in t) else (3 if ('3 meses' in t or 'tres meses' in t) else 1)
    reserva = round(valor / meses, 2)
    db.session.add(DespesaFutura(
        usuario_id=usuario.id, descricao=desc, valor_total=valor,
        valor_reserva_mensal=reserva, meses=meses,
        data_prevista=agora().replace(tzinfo=None) + timedelta(days=30 * meses)
    ))
    db.session.commit()
    enviar_mensagem(phone_raw, f"📅 Anotado! {desc}: {valor:.0f} euros daqui a {meses} mes{'es' if meses>1 else ''}\nVou guardar {reserva:.0f} euros/mes p/ isso. Quando receberes ja conto com isto! 👍")


# ─── SIMULAR COMPRA ──────────────────────────────────────────
def simular_compra(phone_raw, usuario, texto):
    valor = extrair_valor(texto)
    disp = calcular_disponivel(usuario)
    if valor == 0:
        enviar_mensagem(phone_raw, f"💚 Tens {disp:.2f} euros para gastar este mes.")
        return
    if disp <= 0:
        resp = f"🔴 Nem penses! Ja nao tens orcamento este mes ({disp:.2f} euros). Espera pelo proximo salario 😅"
    else:
        pct = valor / disp * 100
        if pct <= 30:
            resp = f"✅ Podes sim! {valor:.0f} euros e tranquilo, ficas com {disp-valor:.0f} euros. Vai nessa! 🛍️"
        elif pct <= 60:
            resp = f"🟡 Da, mas pensa bem. Ficavas com {disp-valor:.0f} euros para o resto do mes. Precisas mesmo?"
        elif pct <= 100:
            resp = f"🟠 Epa, ficavas quase a zero ({disp-valor:.0f} euros). So se for mesmo preciso!"
        else:
            resp = f"🔴 Nao da! Faltam-te {valor-disp:.0f} euros. Deixa para o mes que vem 😬"
    enviar_mensagem(phone_raw, resp)


# ─── BOAS VINDAS / AJUDA ─────────────────────────────────────
def enviar_boas_vindas(phone_raw):
    enviar_mensagem(phone_raw, "Ola! 👋 Sou o Ze das Financas, o teu parceiro de carteira 💰\n\nManda-me os teus gastos que eu trato de tudo. Tipo:\n• gastei 25 no conti\n• 15 no bk\n• recebi 1300 euros\n\nDiz 'ajuda' p/ veres tudo o que sei fazer 😎")


def enviar_ajuda(phone_raw):
    msg = """😎 O que eu sei fazer:

💸 Registar gastos:
• gastei 25 no conti
• 15 bk / 50 galp / 8 mac
• Foto do talao
• Mensagem de voz

📊 Consultar:
• resumo → tudo do mes
• quanto tenho → quanto podes gastar
• quanto tenho na conjunta
• plano → distribuicao do salario
• score → a tua nota

💰 Salario:
• recebi 1300 euros

🎯 Planear:
• posso comprar tenis 90 euros?
• mes que vem dentista 40 euros
• quero poupar para ferias

🆘 Extras:
• estou teso → modo poupanca
• gasolina mais barata no barreiro

Bora poupar! 🚀"""
    enviar_mensagem(phone_raw, msg)


# ─── IA ──────────────────────────────────────────────────────
def perguntar_ia(texto, usuario):
    try:
        from groq import Groq
        client = Groq(api_key=GROQ_API_KEY)
        disp = calcular_disponivel(usuario)
        ctx = f"Saldo disponivel da Luana este mes: {disp:.0f} euros. Salario: {usuario.salario_liquido or '?'} euros."
        sys = f"""Es o Ze das Financas, assistente financeiro pessoal portugues, criado pelo tuga27.
Falas portugues europeu informal, com piada, à maneira tuga. Es querido e motivador.
Sabes que em Portugal: BK=Burger King, Mac=McDonald's, conti=Continente, PD=Pingo Doce, galp/bp=postos gasolina, JD=JD Sports, FL=Foot Locker.
{ctx}
Responde curto (max 3 linhas), com 1-2 emojis. Nunca digas que BK e Banco de Portugal!"""
        resp = client.chat.completions.create(
            model='llama-3.3-70b-versatile',
            messages=[{'role': 'system', 'content': sys}, {'role': 'user', 'content': texto}],
            max_tokens=200
        )
        return resp.choices[0].message.content
    except Exception as e:
        log.error(f'Erro IA: {e}')
        return "Nao percebi 🤔 Diz 'ajuda' p/ veres o que sei fazer!"


# ─── LEMBRETES ───────────────────────────────────────────────
def lembrete_recibo():
    with app.app_context():
        hoje = agora()
        if hoje.day == 20 and hoje.hour == 12:
            for u in Usuario.query.all():
                if u.phone:
                    enviar_mensagem(f"{u.phone}@lid", "Ola! 📄 Ja recebeste o recibo este mes? Quando tiveres envia-me ou diz quanto recebeste 💰")

def lembrete_salario():
    with app.app_context():
        hoje = agora()
        dia = hoje.replace(day=21)
        if dia.weekday() == 5: dia -= timedelta(days=1)
        elif dia.weekday() == 6: dia -= timedelta(days=2)
        if hoje.day == dia.day and hoje.hour == 9:
            for u in Usuario.query.all():
                if u.phone:
                    enviar_mensagem(f"{u.phone}@lid", "💰 Hoje e dia de salario! Quando receberes diz-me que faco o plano do mes 🚀")

def resumo_semanal():
    with app.app_context():
        if agora().weekday() == 0 and agora().hour == 9:
            for u in Usuario.query.all():
                if u.phone:
                    enviar_resumo(f"{u.phone}@lid", u)


with app.app_context():
    try:
        db.create_all()
    except Exception as e:
        log.warning(f"db.create_all: {e}")

scheduler.add_job(lembrete_recibo, 'cron', hour=12, minute=0)
scheduler.add_job(lembrete_salario, 'cron', hour=9, minute=0)
scheduler.add_job(resumo_semanal, 'cron', hour=9, minute=30, day_of_week='mon')
scheduler.start()
log.info("Ze das Financas iniciado")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
