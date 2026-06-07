import os, json, logging, re, base64, tempfile
from datetime import datetime, timedelta
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Lisbon")
except Exception:
    TZ = None
from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import text
from models import db, Usuario, Despesa, Receita, DespesaFutura, ObjetivoFinanceiro, FundoEmergencia
from whatsapp import enviar_mensagem
from claude_ai import processar_mensagem_ia
from pdf_reader import extrair_salario_pdf

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///luana.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(app)

OWNER_PHONE  = os.environ.get('OWNER_PHONE', '')
WAHA_URL     = os.environ.get('WAHA_URL', 'https://evolution-api-production-634b.up.railway.app')
WAHA_API_KEY = os.environ.get('WAHA_API_KEY', 'waha123')
WAHA_SESSION = os.environ.get('WAHA_SESSION', 'default')
GROQ_API_KEY = os.environ.get('GROQ_API_KEY', '')

GASTAR_NORMAL    = 200
GASTAR_SUBSIDIO  = 400
FUNDO_PCT        = 0.05
BASE_COMBUSTIVEL = 50

scheduler = BackgroundScheduler()

def agora():
    return datetime.now(TZ) if TZ else datetime.now()

# ─── DATAS ───────────────────────────────────────────────────
def dia_pagamento_mes(ano, mes):
    d = datetime(ano, mes, 21)
    if d.weekday() == 5: d -= timedelta(days=1)
    elif d.weekday() == 6: d -= timedelta(days=2)
    return d

def dia_recibo_mes(ano, mes):
    pag = dia_pagamento_mes(ano, mes)
    d = pag - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d

def dias_para_salario():
    hoje = agora()
    pag = dia_pagamento_mes(hoje.year, hoje.month)
    if pag.date() <= hoje.date():
        mes_prox = hoje.month + 1 if hoje.month < 12 else 1
        ano_prox = hoje.year if hoje.month < 12 else hoje.year + 1
        pag = dia_pagamento_mes(ano_prox, mes_prox)
    return (pag.date() - hoje.date()).days

# ─── ABREVIAÇÕES ─────────────────────────────────────────────
LOJAS = {
    'bk':'fastfood','burger king':'fastfood','mac':'fastfood','mc':'fastfood',
    'mcd':'fastfood','mcdonald':'fastfood',"mcdonald's":'fastfood','mcdonalds':'fastfood',
    'kfc':'fastfood','sbx':'fastfood','starbucks':'fastfood','sub':'fastfood','subway':'fastfood',
    'telepizza':'fastfood','dominos':'fastfood',
    'zen':'restaurante','zen-sushi':'restaurante','zen sushi':'restaurante','sushi':'restaurante',
    'alcochete':'restaurante','restaurante':'restaurante','cafe':'restaurante','café':'restaurante',
    'kebab':'restaurante','tasca':'restaurante','pizza':'restaurante',
    'foot':'roupa','fl':'roupa','foot locker':'roupa','jd':'roupa','jd sports':'roupa',
    'snipes':'roupa','zara':'roupa','z':'roupa','hm':'roupa','h&m':'roupa',
    'primark':'roupa','shein':'roupa','bershka':'roupa','nike':'roupa','nk':'roupa',
    'nke':'roupa','adidas':'roupa','ads':'roupa','adi':'roupa',
    'apl':'tecnologia','apple':'tecnologia','sam':'tecnologia','smg':'tecnologia',
    'samsung':'tecnologia','ps':'tecnologia','psn':'tecnologia','xb':'tecnologia',
    'xbx':'tecnologia','wrt':'tecnologia','worten':'tecnologia','rp':'tecnologia',
    'conti':'supermercado','cnt':'supermercado','continente':'supermercado',
    'pd':'supermercado','pingo doce':'supermercado','pingo':'supermercado',
    'lidl':'supermercado','aldi':'supermercado','mercadona':'supermercado',
    'minipreco':'supermercado','intermarche':'supermercado','ik':'supermercado','ikea':'supermercado',
    'bp':'combustivel','galp':'combustivel','repsol':'combustivel','shell':'combustivel',
    'prio':'combustivel','cepsa':'combustivel','gasolina':'combustivel',
    'gota':'gota','agua':'gota','água':'gota',
    'farmacia':'saude','farmácia':'saude','wells':'saude','dentista':'saude','medico':'saude',
    'unhas':'pessoal','cabelo':'pessoal','cabeleireiro':'pessoal','estetica':'pessoal',
    'oficina':'carro','mecanico':'carro','seguro':'carro','portagem':'carro','via verde':'carro',
    'cinema':'lazer','concerto':'lazer','bowling':'lazer',
    'netflix':'subscricoes','spotify':'subscricoes','disney':'subscricoes',
}
LOJAS_NOME = {
    'bk':'Burger King','mac':"McDonald's",'mc':"McDonald's",'mcd':"McDonald's",
    'mcdonald':"McDonald's",'mcdonalds':"McDonald's",'kfc':'KFC','sbx':'Starbucks',
    'starbucks':'Starbucks','sub':'Subway','subway':'Subway','zen':'Zen Sushi',
    'foot':'Foot Locker','fl':'Foot Locker','jd':'JD Sports','snipes':'Snipes',
    'zara':'Zara','z':'Zara','hm':'H&M','nike':'Nike','nk':'Nike','adidas':'Adidas',
    'ads':'Adidas','adi':'Adidas','apl':'Apple','sam':'Samsung','smg':'Samsung',
    'ps':'PlayStation','psn':'PlayStation','xb':'Xbox','xbx':'Xbox',
    'wrt':'Worten','worten':'Worten','rp':'Radio Popular',
    'conti':'Continente','cnt':'Continente','continente':'Continente',
    'pd':'Pingo Doce','pingo doce':'Pingo Doce','pingo':'Pingo Doce',
    'lidl':'Lidl','aldi':'Aldi','ikea':'IKEA','ik':'IKEA',
    'bp':'BP','galp':'Galp','repsol':'Repsol','shell':'Shell','prio':'Prio','cepsa':'Cepsa',
    'gota':'Gota','agua':'Água',
}
EMOJI_CAT = {
    'fastfood':'🍔','restaurante':'🍽️','roupa':'👗','tecnologia':'📱',
    'supermercado':'🛒','combustivel':'⛽','gota':'🧃','saude':'💊',
    'pessoal':'💅','carro':'🚗','lazer':'🎭','subscricoes':'📺','outros':'💳',
}
CATEGORIAS_VALIDAS = list(EMOJI_CAT.keys())
ALIAS_CAT = {
    'comida':'supermercado','mercado':'supermercado','super':'supermercado',
    'fast food':'fastfood','hamburguer':'fastfood','burger':'fastfood',
    'restaurantes':'restaurante','sushi':'restaurante','piza':'restaurante',
    'pizza':'restaurante','kebab':'restaurante','jantar':'restaurante',
    'roupas':'roupa','sapatilhas':'roupa','tenis':'roupa','sapatos':'roupa',
    'sneakers':'roupa','calcado':'roupa','moda':'roupa',
    'tech':'tecnologia','eletronica':'tecnologia','gaming':'tecnologia',
    'gasolina':'combustivel','gasoleo':'combustivel','posto':'combustivel',
    'agua':'gota','bebida':'gota','bebidas':'gota',
    'farmacia':'saude','medico':'saude','saúde':'saude','dentista':'saude',
    'unhas':'pessoal','cabelo':'pessoal','beleza':'pessoal',
    'automovel':'carro','oficina':'carro','portagem':'carro',
    'cinema':'lazer','diversao':'lazer','concerto':'lazer',
    'netflix':'subscricoes','subscricao':'subscricoes','spotify':'subscricoes',
}

def normalizar_categoria(cat):
    cat = cat.lower().strip()
    if cat in CATEGORIAS_VALIDAS: return cat
    return ALIAS_CAT.get(cat, cat)

def categorizar(texto):
    t = texto.lower()
    aprendidas = carregar_aprendidas()
    for chave, cat in aprendidas.items():
        if chave in t:
            return cat, EMOJI_CAT.get(cat,'💳'), chave.capitalize()
    tokens = re.findall(r"[a-zà-ú&']+", t)
    for chave, cat in LOJAS.items():
        if ' ' in chave and chave in t:
            return cat, EMOJI_CAT.get(cat,'💳'), LOJAS_NOME.get(chave, chave.capitalize())
    for tok in tokens:
        if tok in LOJAS:
            cat = LOJAS[tok]
            return cat, EMOJI_CAT.get(cat,'💳'), LOJAS_NOME.get(tok, tok.capitalize())
    for chave, cat in LOJAS.items():
        if ' ' not in chave and len(chave) > 3 and chave in t:
            return cat, EMOJI_CAT.get(cat,'💳'), LOJAS_NOME.get(chave, chave.capitalize())
    return 'outros', '💳', 'Gasto'

# ─── APRENDIZAGEM + ESTADO ───────────────────────────────────
def criar_tabelas():
    sqls = [
        """CREATE TABLE IF NOT EXISTS aprendizagem (
            chave VARCHAR(100) PRIMARY KEY, categoria VARCHAR(50) NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS estado_utilizador (
            phone VARCHAR(50) PRIMARY KEY, estado VARCHAR(100),
            dados TEXT, atualizado TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS badges (
            id SERIAL PRIMARY KEY, usuario_id INTEGER, badge VARCHAR(100),
            obtido_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS pessoas_gastos (
            id SERIAL PRIMARY KEY, usuario_id INTEGER, despesa_id INTEGER,
            pessoa VARCHAR(100))""",
    ]
    for sql in sqls:
        try:
            db.session.execute(text(sql)); db.session.commit()
        except Exception as e:
            log.warning(f"tabela: {e}"); db.session.rollback()

def carregar_aprendidas():
    try:
        rows = db.session.execute(text("SELECT chave, categoria FROM aprendizagem")).fetchall()
        return {r[0]: r[1] for r in rows}
    except Exception:
        db.session.rollback(); return {}

def guardar_aprendida(chave, categoria):
    try:
        db.session.execute(
            text("INSERT INTO aprendizagem (chave,categoria) VALUES (:c,:cat) ON CONFLICT (chave) DO UPDATE SET categoria=:cat"),
            {'c': chave.lower().strip(), 'cat': categoria})
        db.session.commit(); return True
    except Exception as e:
        log.error(f"aprendida: {e}"); db.session.rollback(); return False

def get_estado(phone):
    try:
        r = db.session.execute(text("SELECT estado, dados FROM estado_utilizador WHERE phone=:p"), {'p':phone}).fetchone()
        return (r[0], json.loads(r[1]) if r[1] else {}) if r else (None, {})
    except Exception:
        db.session.rollback(); return (None, {})

def set_estado(phone, estado, dados=None):
    try:
        db.session.execute(
            text("INSERT INTO estado_utilizador (phone,estado,dados,atualizado) VALUES (:p,:e,:d,NOW()) ON CONFLICT (phone) DO UPDATE SET estado=:e,dados=:d,atualizado=NOW()"),
            {'p':phone,'e':estado,'d':json.dumps(dados or {})})
        db.session.commit()
    except Exception as e:
        log.error(f"set_estado: {e}"); db.session.rollback()

def limpar_estado(phone):
    set_estado(phone, None, {})

# ─── BADGES ──────────────────────────────────────────────────
BADGES = {
    'primeiro_gasto':    ('🎉', 'Primeiro Gasto!', 'Registaste o primeiro gasto'),
    'semana_limpa':      ('✨', 'Semana Limpa', '7 dias dentro do orcamento'),
    'mes_dentro':        ('🏆', 'Mes Perfeito', 'Dentro do orcamento o mes todo'),
    'poupadora':         ('💎', 'Poupadora', 'Poupaste mais de 200€ num mes'),
    'mestre_poupanca':   ('👑', 'Mestre da Poupanca', '3 meses seguidos dentro do orcamento'),
    'sem_fastfood':      ('🥗', 'Clean Eater', '2 semanas sem fast food'),
    'meta_gasolina':     ('⛽', 'Combustivel Controlado', 'Ficaste abaixo dos 50€ em gasolina'),
}

def verificar_badges(usuario, phone_raw):
    try:
        badges_usuario = {r[0] for r in db.session.execute(
            text("SELECT badge FROM badges WHERE usuario_id=:id"), {'id':usuario.id}).fetchall()}

        novos = []
        mes = agora().month; ano = agora().year

        # Primeiro gasto
        total_gastos = db.session.query(db.func.count(Despesa.id)).filter_by(usuario_id=usuario.id).scalar() or 0
        if total_gastos == 1 and 'primeiro_gasto' not in badges_usuario:
            novos.append('primeiro_gasto')

        # Mes dentro do orcamento
        gastos_mes = db.session.query(db.func.sum(Despesa.valor)).filter(
            Despesa.usuario_id==usuario.id,
            db.extract('month',Despesa.data)==mes, db.extract('year',Despesa.data)==ano).scalar() or 0
        gastar = GASTAR_SUBSIDIO if mes in [6,11] else GASTAR_NORMAL
        if gastos_mes <= gastar and agora().day >= 20 and 'mes_dentro' not in badges_usuario:
            novos.append('mes_dentro')

        # Poupadora — poupou mais de 200€
        if usuario.salario_liquido:
            p = calcular_plano(usuario.salario_liquido)
            if p['poupanca'] >= 200 and 'poupadora' not in badges_usuario:
                novos.append('poupadora')

        # Combustivel controlado
        gas_mes = db.session.query(db.func.sum(Despesa.valor)).filter(
            Despesa.usuario_id==usuario.id, Despesa.categoria=='combustivel',
            db.extract('month',Despesa.data)==mes, db.extract('year',Despesa.data)==ano).scalar() or 0
        if gas_mes > 0 and gas_mes <= BASE_COMBUSTIVEL and agora().day >= 20 and 'meta_gasolina' not in badges_usuario:
            novos.append('meta_gasolina')

        for badge in novos:
            db.session.execute(text("INSERT INTO badges (usuario_id,badge) VALUES (:id,:b)"),
                               {'id':usuario.id,'b':badge})
            db.session.commit()
            emoji, nome, desc = BADGES[badge]
            enviar_mensagem(phone_raw, f"🎖️ CONQUISTA DESBLOQUEADA!\n{emoji} {nome}\n{desc}\n\nEstas a mandar! 🙌")
    except Exception as e:
        log.error(f"badges: {e}"); db.session.rollback()

# ─── WEBHOOK ─────────────────────────────────────────────────
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.json
        if not data: return jsonify({'status':'ok'})
        event = data.get('event','')
        if event not in ['message','messages.upsert','']: return jsonify({'status':'ok'})
        payload = data.get('payload', data)
        from_me = payload.get('fromMe', False)
        msg_id  = payload.get('id','')
        if from_me or (isinstance(msg_id,str) and msg_id.startswith('true_')):
            return jsonify({'status':'ok'})
        from_field = payload.get('from','') or payload.get('chatId','')
        phone_raw = from_field
        phone = from_field.replace('@c.us','').replace('@s.whatsapp.net','').replace('@g.us','').replace('@lid','').split('@')[0]
        if not phone: return jsonify({'status':'ok'})
        owner_phones = [p.strip() for p in OWNER_PHONE.split(',')] if OWNER_PHONE else []
        if owner_phones and phone not in owner_phones: return jsonify({'status':'ok'})

        media    = payload.get('media')
        has_media= payload.get('hasMedia', False)
        body     = payload.get('body','')
        texto    = (body.get('text','') or body.get('conversation','')) if isinstance(body,dict) else (str(body) if body else '')
        if not texto: texto = payload.get('text','') or payload.get('content','')

        if not texto and has_media and media:
            mime = media.get('mimetype','')
            url  = media.get('url','')
            if 'audio' in mime or 'ogg' in mime:
                transcrito = transcrever_audio(url)
                if transcrito:
                    enviar_mensagem(phone_raw, f'🎤 Percebi: "{transcrito}"')
                    texto = transcrito
                else:
                    enviar_mensagem(phone_raw, "Nao percebi o audio 😕 Escreve!"); return jsonify({'status':'ok'})
            elif 'image' in mime:
                resultado = ler_foto_talao(url, mime)
                if resultado:
                    valor_lido = extrair_valor(resultado)
                    if valor_lido > 500:
                        enviar_mensagem(phone_raw, f'📸 Vi no recibo: {valor_lido:.2f}€ — e esse o teu salario?')
                        set_estado(phone, 'confirmar_salario', {'valor': valor_lido})
                    else:
                        enviar_mensagem(phone_raw, f'📸 Li: {resultado}')
                        texto = resultado
                else:
                    enviar_mensagem(phone_raw, "Nao consegui ler 😕 Escreve o valor!"); return jsonify({'status':'ok'})
            elif 'pdf' in mime or 'application' in mime:
                resultado = ler_pdf_salario(url)
                if resultado:
                    enviar_mensagem(phone_raw, f'📄 Vi no recibo: {resultado:.2f}€ — e esse o teu salario?')
                    set_estado(phone, 'confirmar_salario', {'valor': resultado})
                else:
                    enviar_mensagem(phone_raw, "Nao li o PDF 😕 Diz: recebi X euros")
                return jsonify({'status':'ok'})

        if not texto: return jsonify({'status':'ok'})
        log.info(f"Msg de {phone}: {texto}")
        with app.app_context():
            processar_texto(phone_raw, phone, texto)
    except Exception as e:
        log.error(f'Webhook: {e}', exc_info=True)
    return jsonify({'status':'ok'})

@app.route('/', methods=['GET'])
def health():
    return jsonify({'status':'ok','bot':'Ze das Financas v5'})

# ─── MEDIA ───────────────────────────────────────────────────
def baixar_media(url):
    import requests as req
    from urllib.parse import urlparse
    if not url: return None
    if 'localhost' in url or '127.0.0.1' in url:
        parsed = urlparse(url)
        url = WAHA_URL.rstrip('/') + parsed.path + (('?'+parsed.query) if parsed.query else '')
    try:
        r = req.get(url, headers={'X-Api-Key': WAHA_API_KEY}, timeout=30)
        log.info(f'Media: {r.status_code} {len(r.content)}b')
        return r.content if r.status_code==200 and r.content else None
    except Exception as e:
        log.error(f'Download: {e}'); return None

def transcrever_audio(url):
    try:
        from groq import Groq
        c = baixar_media(url)
        if not c: return ''
        with tempfile.NamedTemporaryFile(suffix='.ogg', delete=False) as f:
            f.write(c); fname = f.name
        t = Groq(api_key=GROQ_API_KEY).audio.transcriptions.create(
            model='whisper-large-v3', file=(os.path.basename(fname), open(fname,'rb').read()),
            language='pt', prompt='Gastos em euros. Lojas: Continente, BK, McDonald, Galp, Zara.')
        try: os.unlink(fname)
        except: pass
        log.info(f'Audio: {t.text}'); return t.text.strip()
    except Exception as e:
        log.error(f'Audio: {e}', exc_info=True); return ''

def ler_foto_talao(url, mimetype='image/jpeg'):
    try:
        from groq import Groq
        c = baixar_media(url)
        if not c: return ''
        mt = 'image/png' if 'png' in mimetype else ('image/webp' if 'webp' in mimetype else 'image/jpeg')
        img = base64.b64encode(c).decode()
        resp = Groq(api_key=GROQ_API_KEY).chat.completions.create(
            model='meta-llama/llama-4-scout-17b-16e-instruct', max_tokens=80,
            messages=[{'role':'user','content':[
                {'type':'image_url','image_url':{'url':f'data:{mt};base64,{img}'}},
                {'type':'text','text':'Le este documento. Se recibo salario: "X,XX euros SALARIO". Se talao compra: "X,XX euros LOJA". Valor liquido/total. Ex: "1327,92 euros SALARIO" ou "25,50 euros Continente". Se nao deres: erro'}
            ]}])
        txt = resp.choices[0].message.content.strip()
        log.info(f'Foto: {txt}')
        return '' if 'erro' in txt.lower() else txt
    except Exception as e:
        log.error(f'Foto: {e}', exc_info=True); return ''

def ler_pdf_salario(url):
    try:
        import requests as req
        from urllib.parse import urlparse
        if 'localhost' in url or '127.0.0.1' in url:
            url = WAHA_URL.rstrip('/') + urlparse(url).path
        r = req.get(url, headers={'X-Api-Key': WAHA_API_KEY}, timeout=30)
        if r.status_code != 200: return None
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as f:
            f.write(r.content); fname = f.name
        valor = extrair_salario_pdf(fname)
        try: os.unlink(fname)
        except: pass
        return valor
    except Exception as e:
        log.error(f'PDF: {e}'); return None

# ─── EXTRAIR VALOR ───────────────────────────────────────────
def extrair_valor(texto):
    padrao = re.findall(r'\d[\d.,]*\d|\d+', texto)
    for n in padrao:
        try:
            if '.' in n and ',' in n:
                v = float(n.replace('.','').replace(',','.'))
            elif ',' in n:
                v = float(n.replace(',','.'))
            elif '.' in n:
                decimais = n[n.rfind('.')+1:]
                v = float(n.replace('.','')) if (len(decimais)==3 and n.replace('.','').isdigit()) else float(n)
            else:
                v = float(n)
            if v > 0: return v
        except: continue
    return 0

def tem_numero(texto):
    return bool(re.search(r'[0-9]+[.,]?[0-9]*', texto))

def eh_gasto(texto):
    t = texto.lower()
    if any(v in t for v in ['gastei','paguei','comprei','almocei','jantei','custou','meti','abasteci','lanchei']): return True
    if '€' in t or 'euro' in t: return True
    cat, _, _ = categorizar(texto)
    return cat != 'outros'

# ─── CÁLCULOS ────────────────────────────────────────────────
def calcular_plano(salario):
    mes = agora().month
    fixos = {'carro':350,'ordem':20,'conjunta':50,'unhas':50 if mes<=9 else 25,'combustivel':BASE_COMBUSTIVEL}
    total_fixos = sum(fixos.values())
    fundo  = round(salario * FUNDO_PCT, 2)
    gastar = GASTAR_SUBSIDIO if mes in [6,11] else GASTAR_NORMAL
    poupanca = round(salario - total_fixos - fundo - gastar, 2)
    return {**fixos,'total_fixos':total_fixos,'salario':salario,'fundo':fundo,'gastar':gastar,'poupanca':poupanca,'subsidio':mes in [6,11]}

def calcular_disponivel(usuario):
    mes=agora().month; ano=agora().year
    gastos = db.session.query(db.func.sum(Despesa.valor)).filter(
        Despesa.usuario_id==usuario.id,
        db.extract('month',Despesa.data)==mes, db.extract('year',Despesa.data)==ano,
        ~Despesa.descricao.like('[conjunta]%')
    ).scalar() or 0
    gastar = GASTAR_SUBSIDIO if mes in [6,11] else GASTAR_NORMAL
    return gastar - gastos

def gastos_categoria_mes(usuario, categoria, mes, ano):
    return db.session.query(db.func.sum(Despesa.valor)).filter(
        Despesa.usuario_id==usuario.id, Despesa.categoria==categoria,
        db.extract('month',Despesa.data)==mes, db.extract('year',Despesa.data)==ano
    ).scalar() or 0

# ─── PROCESSAR TEXTO ─────────────────────────────────────────
def processar_texto(phone_raw, phone, texto):
    usuario = Usuario.query.filter_by(phone=phone).first()
    if not usuario:
        usuario = Usuario(phone=phone, nome='Luana')
        db.session.add(usuario); db.session.commit()

    t = texto.lower().strip()

    # ESTADOS
    estado, dados_estado = get_estado(phone)
    if estado == 'confirmar_salario':
        if any(p in t for p in ['sim','yes','correto','certo','exato','é isso','e isso']):
            valor = dados_estado.get('valor', 0)
            limpar_estado(phone)
            processar_receita(phone_raw, usuario, f"recebi {valor}")
        elif tem_numero(texto):
            limpar_estado(phone)
            processar_receita(phone_raw, usuario, texto)
        else:
            limpar_estado(phone)
            enviar_mensagem(phone_raw, "Ok, diz tu: recebi X euros 💰")
        return

    if estado == 'aguardar_recibo':
        if any(p in t for p in ['sim','yes','quero','manda','envia']):
            limpar_estado(phone)
            enviar_mensagem(phone_raw, "Manda o PDF ou foto do recibo 📄")
        elif any(p in t for p in ['nao','não','valor','digo']) or tem_numero(texto):
            limpar_estado(phone)
            if tem_numero(texto): processar_receita(phone_raw, usuario, texto)
            else: enviar_mensagem(phone_raw, "Ok, diz o valor: recebi X euros 💰")
        return

    # APRENDER
    m = re.search(r'aprende que (.+?) (?:é|e|sao|são) (?:da categoria |categoria )?(\w+)', t)
    if m:
        chave = m.group(1).strip().strip('"\'')
        cat = normalizar_categoria(m.group(2))
        if cat in CATEGORIAS_VALIDAS:
            enviar_mensagem(phone_raw, f"🧠 Aprendido! '{chave}' = {cat.capitalize()} p/ sempre 😎") if guardar_aprendida(chave, cat) else enviar_mensagem(phone_raw, "Ops 😕")
        else:
            enviar_mensagem(phone_raw, f"Nao conheço essa categoria 🤔\nUsa: {', '.join(CATEGORIAS_VALIDAS)}")
        return

    # CORRIGIR
    m2 = re.search(r'(?:corrige|corrigir|muda|mudar|afinal|isso é|isso e|o ultimo|o último) (?:para |o )*(\w+)', t)
    if m2 and any(p in t for p in ['corrige','corrigir','muda','mudar','afinal','isso é','isso e','ultimo','último']):
        cat = normalizar_categoria(m2.group(1))
        if cat in CATEGORIAS_VALIDAS:
            corrigir_ultimo(phone_raw, usuario, cat); return

    # CANCELAR DESPESA FUTURA
    if any(p in t for p in ['afinal nao','afinal não','cancela','cancelo','remove a despesa','apaga despesa','nao tenho mais','não tenho mais']):
        processar_despesa_futura(phone_raw, usuario, texto); return

    # CRIADOR
    if any(p in t for p in ['quem criou','quem te fez','quem te criou','criador','quem te programou']):
        enviar_mensagem(phone_raw, "Fui criado pelo tuga27 🚀\nO mesmo genio por tras do Zeflix e agora do teu gestor financeiro. Sortuda! 😎"); return

    if any(p in t for p in ['ajuda','help','/start','comandos']):
        enviar_ajuda(phone_raw); return

    if t in ['ola','olá','oi','boas','hey','hello'] or 'bom dia' in t or 'boa tarde' in t or 'boa noite' in t:
        enviar_boas_vindas(phone_raw, usuario); return

    if 'estou teso' in t or 'tou teso' in t or 'sem dinheiro' in t or 'liso' in t:
        modo_teso(phone_raw, usuario); return

    if any(p in t for p in ['gasolina mais barata','posto mais barato','gasolina barata']):
        gasolina_barata(phone_raw, t); return

    if 'conjunta' in t and any(p in t for p in ['quanto','tenho','sobra','resta']):
        enviar_conjunta(phone_raw, usuario); return

    if any(p in t for p in ['quanto tenho','quanto me resta','quanto sobra','saldo']):
        enviar_quanto_tenho(phone_raw, usuario); return

    if any(p in t for p in ['resumo anterior','mes passado','mes anterior']):
        mes_ant = agora().month-1 if agora().month>1 else 12
        ano_ant = agora().year if agora().month>1 else agora().year-1
        enviar_resumo(phone_raw, usuario, mes_ant, ano_ant); return

    if any(p in t for p in ['resumo','como estou','quanto gastei','situacao','situação']):
        enviar_resumo(phone_raw, usuario); return

    if any(p in t for p in ['plano','transferencia','transferência','distribuicao']):
        enviar_plano_mes(phone_raw, usuario); return

    if 'score' in t or 'conquistas' in t or 'badges' in t:
        enviar_score(phone_raw, usuario); return

    if any(p in t for p in ['poupar para','quero poupar','objetivo','objectivo']):
        enviar_mensagem(phone_raw, processar_mensagem_ia(texto, usuario, 'objetivo')); return

    if any(p in t for p in ['mes que vem','mês que vem','proximo mes','próximo mês','este mes tenho','este mês tenho']) and tem_numero(texto):
        processar_despesa_futura(phone_raw, usuario, texto); return

    if any(p in t for p in ['dentista','seguro','inspecao','inspeção']) and 'mes' in t and tem_numero(texto):
        processar_despesa_futura(phone_raw, usuario, texto); return

    if any(p in t for p in ['posso comprar','posso gastar','vale a pena','consigo comprar']):
        simular_compra(phone_raw, usuario, texto); return

    if any(p in t for p in ['recebi','ordenado','salario','salário','recibo','vencimento']) and tem_numero(texto):
        processar_receita(phone_raw, usuario, texto); return

    if any(p in t for p in ['quanto gastei com','gastei com','fui com']):
        resumo_por_pessoa(phone_raw, usuario, texto); return

    if tem_numero(texto) and eh_gasto(texto):
        processar_despesa(phone_raw, usuario, texto); return

    enviar_mensagem(phone_raw, perguntar_ia(texto, usuario))

# ─── PROCESSAR DESPESA ───────────────────────────────────────
def processar_despesa(phone_raw, usuario, texto):
    valor = extrair_valor(texto)
    if valor == 0:
        enviar_mensagem(phone_raw, "Nao percebi o valor 🤔 Diz tipo: 25 conti"); return

    categoria, emoji, nome_loja = categorizar(texto)
    na_conjunta = 'conjunta' in texto.lower()

    # Extrai pessoa se mencionada (@nome ou "com a ana")
    pessoa = None
    m_pessoa = re.search(r'com (?:a |o |as |os )?([A-Za-zÀ-ú]+)', texto, re.IGNORECASE)
    if m_pessoa and m_pessoa.group(1).lower() not in ['conjunta','ruben']:
        pessoa = m_pessoa.group(1).capitalize()

    descricao = ('[conjunta] ' if na_conjunta else '') + texto[:90]
    despesa = Despesa(usuario_id=usuario.id, valor=valor, categoria=categoria,
                      descricao=descricao, data=agora().replace(tzinfo=None))
    db.session.add(despesa); db.session.commit()

    # Regista pessoa se mencionada
    if pessoa:
        try:
            db.session.execute(text("INSERT INTO pessoas_gastos (usuario_id,despesa_id,pessoa) VALUES (:u,:d,:p)"),
                               {'u':usuario.id,'d':despesa.id,'p':pessoa})
            db.session.commit()
        except Exception: db.session.rollback()

    mes=agora().month; ano=agora().year
    mes_ant = mes-1 if mes>1 else 12
    ano_ant = ano if mes>1 else ano-1

    total_cat     = gastos_categoria_mes(usuario, categoria, mes, ano)
    total_cat_ant = gastos_categoria_mes(usuario, categoria, mes_ant, ano_ant)
    disp = calcular_disponivel(usuario)
    gastar = GASTAR_SUBSIDIO if mes in [6,11] else GASTAR_NORMAL
    pct_usado = ((gastar - disp) / gastar * 100) if gastar > 0 else 0

    # Comentário
    extra = ''
    inicio_semana = agora().replace(tzinfo=None) - timedelta(days=agora().weekday())
    vezes_semana = db.session.query(db.func.count(Despesa.id)).filter(
        Despesa.usuario_id==usuario.id, Despesa.categoria==categoria,
        Despesa.data>=inicio_semana).scalar() or 0

    if categoria == 'fastfood' and vezes_semana >= 3:
        extra = f'\n😏 Ja e a {vezes_semana}a vez de fast food esta semana hein!'
    elif categoria == 'gota' and total_cat > 30:
        extra = f'\n🧃 {total_cat:.0f}€ em bebidas este mes... abranda!'
    elif categoria == 'combustivel':
        if total_cat > BASE_COMBUSTIVEL * 1.5:
            extra = f'\n⛽ {total_cat:.0f}€ em gasolina, acima da base de {BASE_COMBUSTIVEL}€!'
        elif total_cat > BASE_COMBUSTIVEL:
            extra = f'\n⛽ Passaste a base de {BASE_COMBUSTIVEL}€ em gasolina'
    elif agora().weekday() in [4,5] and agora().hour >= 19 and categoria in ['restaurante','fastfood']:
        extra = '\n🍻 Fim de semana a noite, la vem o costume!'
    elif total_cat_ant > 0 and total_cat > total_cat_ant * 1.3:
        extra = f'\n⚠️ Ja gastaste mais em {categoria} que o mes passado todo!'
    elif total_cat_ant > 0 and total_cat < total_cat_ant * 0.6:
        extra = f'\n✅ Muito menos em {categoria} que o mes passado. Bora!'

    # Aviso orçamento
    aviso = ''
    if pct_usado >= 100: aviso = f'\n\n🔴 Passaste o orcamento! {abs(disp):.0f}€ a mais.'
    elif pct_usado >= 80: aviso = f'\n\n🔔 Ja usaste {pct_usado:.0f}% do orcamento!'

    conjunta_txt = ' (conjunta 💑)' if na_conjunta else ''
    pessoa_txt = f' (com {pessoa})' if pessoa else ''
    msg = (f"{emoji} Bora! {nome_loja} {valor:.2f}€{conjunta_txt}{pessoa_txt}\n"
           f"{categoria.capitalize()}: {total_cat:.2f}€ este mes{extra}{aviso}\n"
           f"💚 Disponivel: {disp:.2f}€ ({100-pct_usado:.0f}% livre)")
    enviar_mensagem(phone_raw, msg)
    verificar_badges(usuario, phone_raw)

# ─── RESUMO POR PESSOA ───────────────────────────────────────
def resumo_por_pessoa(phone_raw, usuario, texto):
    t = texto.lower()
    palavras = re.findall(r'[a-zà-ú]+', t)
    stop = {'quanto','gastei','com','fui','o','a','os','as','de','que','em','e'}
    nomes = [p.capitalize() for p in palavras if p not in stop and len(p) > 2]
    if not nomes:
        enviar_mensagem(phone_raw, "Com quem? Ex: quanto gastei com a Ana"); return
    nome = nomes[0]
    try:
        rows = db.session.execute(text("""
            SELECT d.valor, d.categoria, d.descricao, d.data
            FROM despesas d JOIN pessoas_gastos pg ON d.id=pg.despesa_id
            WHERE pg.usuario_id=:uid AND LOWER(pg.pessoa)=:p
            ORDER BY d.data DESC LIMIT 20
        """), {'uid':usuario.id,'p':nome.lower()}).fetchall()
        if not rows:
            enviar_mensagem(phone_raw, f"Nao encontrei gastos com {nome} 🤔"); return
        total = sum(r[0] for r in rows)
        msg = f"💸 Gastos com {nome}:\n"
        for r in rows[:5]:
            msg += f"• {r[0]:.0f}€ — {r[2][:25]}\n"
        if len(rows) > 5: msg += f"... e mais {len(rows)-5}\n"
        msg += f"\n💰 Total: {total:.2f}€"
        enviar_mensagem(phone_raw, msg)
    except Exception as e:
        log.error(f"resumo pessoa: {e}"); enviar_mensagem(phone_raw, "Erro ao buscar gastos 😕")

# ─── RECEITA ─────────────────────────────────────────────────
def processar_receita(phone_raw, usuario, texto):
    valor = extrair_valor(texto)
    if valor == 0:
        enviar_mensagem(phone_raw, "Quanto recebeste? 💰"); return
    usuario.salario_liquido = valor
    db.session.add(Receita(usuario_id=usuario.id, valor=valor, descricao='Salario', data=agora().replace(tzinfo=None)))
    db.session.commit()
    enviar_plano_salario(phone_raw, usuario, valor)

def enviar_plano_salario(phone_raw, usuario, salario):
    p = calcular_plano(salario)
    futuras = DespesaFutura.query.filter(DespesaFutura.usuario_id==usuario.id, DespesaFutura.pago==False).all()
    total_fut = sum(d.valor_reserva_mensal for d in futuras)
    poupanca_final = p['poupanca'] - total_fut

    msg  = f"💰 Boa, recebeste {salario:.2f}€!\n\n📋 Plano do mes:\n"
    msg += f"🏠 Fixos: {p['total_fixos']:.0f}€\n"
    msg += f"   🚗 Carro {p['carro']:.0f} | 💼 Ordem {p['ordem']:.0f} | 💅 Unhas {p['unhas']:.0f} | 💑 Conjunta {p['conjunta']:.0f} | ⛽ Gasolina {p['combustivel']:.0f}\n"
    msg += f"🛡️ Fundo emergencia: {p['fundo']:.2f}€ (Revolut, nao mexas!)\n"
    msg += f"💳 Para gastar: {p['gastar']:.0f}€\n"
    if total_fut > 0:
        msg += f"📅 Reservas: {total_fut:.2f}€\n"
        for d in futuras: msg += f"   {d.descricao}: {d.valor_reserva_mensal:.0f}€\n"
    msg += f"💎 Poupanca: {poupanca_final:.2f}€ 🔥"
    if p['subsidio']: msg += "\n\n🌴 Mes de subsidio! Mais margem. Aproveita com juizo 😉"
    enviar_mensagem(phone_raw, msg)

    mes_ant = agora().month-1 if agora().month>1 else 12
    ano_ant = agora().year if agora().month>1 else agora().year-1
    nomes = ['Janeiro','Fevereiro','Marco','Abril','Maio','Junho','Julho','Agosto','Setembro','Outubro','Novembro','Dezembro']

    total_ant = db.session.query(db.func.sum(Despesa.valor)).filter(
        Despesa.usuario_id==usuario.id,
        db.extract('month',Despesa.data)==mes_ant,
        db.extract('year',Despesa.data)==ano_ant).scalar() or 0

    if total_ant > 0:
        enviar_mensagem(phone_raw, f"📊 Como correu {nomes[mes_ant-1]}:")
        enviar_resumo(phone_raw, usuario, mes_ant, ano_ant)
    else:
        enviar_mensagem(phone_raw, f"💡 Primeiro mes a usar o bot! A partir de agora vou guardar tudo 💪")

    phone = phone_raw.replace('@lid','').replace('@c.us','').split('@')[0]
    set_estado(phone, 'fecho_feito', {'mes':agora().month,'ano':agora().year})

# ─── QUANTO TENHO ────────────────────────────────────────────
def enviar_quanto_tenho(phone_raw, usuario):
    disp = calcular_disponivel(usuario)
    p = calcular_plano(usuario.salario_liquido or 0)
    if disp < 0:
        msg = f"😬 Passaste o orcamento em {abs(disp):.2f}€! Cuidado!"
    elif disp < 30:
        msg = f"💸 So tens {disp:.2f}€ para gastar. Aperta o cinto!"
    else:
        msg = f"💚 Tens {disp:.2f}€ para gastar 😎\n🛡️ Fundo: {p['fundo']:.0f}€ | 💎 Poupanca prevista: {p['poupanca']:.0f}€"
    enviar_mensagem(phone_raw, msg)

def enviar_conjunta(phone_raw, usuario):
    mes=agora().month; ano=agora().year
    gasto = db.session.query(db.func.sum(Despesa.valor)).filter(
        Despesa.usuario_id==usuario.id,
        db.extract('month',Despesa.data)==mes, db.extract('year',Despesa.data)==ano,
        Despesa.descricao.like('[conjunta]%')).scalar() or 0
    resta = 50 - gasto
    estado = "✅ Dentro do orcamento!" if resta >= 0 else f"⚠️ Passaste {abs(resta):.0f}€!"
    enviar_mensagem(phone_raw, f"💑 Conjunta (jantares, cinema, lanches):\n💰 Tua parte: 50€\n🛒 Gastaste: {gasto:.2f}€\n💚 Resta: {max(resta,0):.2f}€\n{estado}\n\nPara marcar: 'jantar 30 na conjunta'")

# ─── RESUMO ──────────────────────────────────────────────────
def enviar_resumo(phone_raw, usuario, mes_override=None, ano_override=None):
    mes = mes_override or agora().month
    ano = ano_override or agora().year
    receita = db.session.query(db.func.sum(Receita.valor)).filter(
        Receita.usuario_id==usuario.id,
        db.extract('month',Receita.data)==mes, db.extract('year',Receita.data)==ano
    ).scalar() or usuario.salario_liquido or 0

    gp = db.session.query(db.func.sum(Despesa.valor)).filter(
        Despesa.usuario_id==usuario.id,
        db.extract('month',Despesa.data)==mes, db.extract('year',Despesa.data)==ano,
        ~Despesa.descricao.like('[conjunta]%')).scalar() or 0

    gc = db.session.query(db.func.sum(Despesa.valor)).filter(
        Despesa.usuario_id==usuario.id,
        db.extract('month',Despesa.data)==mes, db.extract('year',Despesa.data)==ano,
        Despesa.descricao.like('[conjunta]%')).scalar() or 0

    por_cat = db.session.query(Despesa.categoria, db.func.sum(Despesa.valor)).filter(
        Despesa.usuario_id==usuario.id,
        db.extract('month',Despesa.data)==mes, db.extract('year',Despesa.data)==ano
    ).group_by(Despesa.categoria).all()

    gastar = GASTAR_SUBSIDIO if mes in [6,11] else GASTAR_NORMAL
    disp = gastar - gp
    nomes = ['Janeiro','Fevereiro','Marco','Abril','Maio','Junho','Julho','Agosto','Setembro','Outubro','Novembro','Dezembro']

    msg = f"📊 {nomes[mes-1]}\n\n💰 Receita: {receita:.0f}€\n🛒 Gastos: {gp:.2f}€\n💑 Conjunta: {gc:.2f}€\n💚 Disponivel: {disp:.2f}€\n\n📈 Categorias:"
    for cat, total in sorted(por_cat, key=lambda x:-x[1]):
        msg += f"\n{EMOJI_CAT.get(cat,'💳')} {cat.capitalize()}: {total:.2f}€"

    if not mes_override:
        dia = agora().day
        if dia > 3 and gp > 0:
            ritmo = gp/dia*30
            msg += f"\n\n🔮 Ao ritmo atual: ~{ritmo:.0f}€ no fim do mes"
        if por_cat:
            top_cat, top_val = max(por_cat, key=lambda x:x[1])
            if top_val > 50:
                msg += f"\n💡 Se reduzisses {top_cat} em 30%, poupas ~{top_val*0.3*12:.0f}€/ano"

    enviar_mensagem(phone_raw, msg)

def enviar_plano_mes(phone_raw, usuario):
    if not usuario.salario_liquido:
        enviar_mensagem(phone_raw, "Ainda nao sei o teu salario 🤔 Diz: recebi 1300 euros"); return
    enviar_plano_salario(phone_raw, usuario, usuario.salario_liquido)

# ─── SCORE + BADGES ──────────────────────────────────────────
def enviar_score(phone_raw, usuario):
    disp = calcular_disponivel(usuario)
    gastar = GASTAR_SUBSIDIO if agora().month in [6,11] else GASTAR_NORMAL
    pct = (gastar-disp)/gastar*100 if gastar>0 else 0
    if pct < 50:    score, txt = 9, "Mestre da poupanca! 🏆"
    elif pct < 75:  score, txt = 7, "Vais bem, continua! 👍"
    elif pct < 100: score, txt = 5, "Cuidado com os gastos 😬"
    else:           score, txt = 2, "Passaste o orcamento 🔴"

    try:
        badges_usuario = db.session.execute(
            text("SELECT badge FROM badges WHERE usuario_id=:id ORDER BY obtido_em DESC"), {'id':usuario.id}).fetchall()
        badges_txt = ''
        if badges_usuario:
            badges_txt = '\n\n🎖️ Conquistas:\n' + '\n'.join(f"{BADGES[r[0]][0]} {BADGES[r[0]][1]}" for r in badges_usuario if r[0] in BADGES)
    except Exception:
        badges_txt = ''

    enviar_mensagem(phone_raw, f"⭐ Score: {score}/10\n{txt}{badges_txt}")

# ─── MODO TESO ───────────────────────────────────────────────
def modo_teso(phone_raw, usuario):
    disp = calcular_disponivel(usuario)
    dias = dias_para_salario()
    msg = (f"😅 Modo teso ativado!\n\n💚 Tens {disp:.2f}€\n📅 ~{dias} dias p/ o salario\n\n"
           f"Dicas de sobrevivencia:\n🍳 Cozinha em casa, chega de take-away\n"
           f"🚶 Anda a pe quando deres\n🛒 So o essencial\n"
           f"☕ Cafe de maquina, sem SBX por uns dias 😬\n💪 Consegues, eu acredito em ti!")
    enviar_mensagem(phone_raw, msg)

# ─── CORRIGIR ────────────────────────────────────────────────
def corrigir_ultimo(phone_raw, usuario, nova_cat):
    ultima = Despesa.query.filter_by(usuario_id=usuario.id).order_by(Despesa.id.desc()).first()
    if not ultima:
        enviar_mensagem(phone_raw, "Nao tenho nenhum gasto p/ corrigir 🤔"); return
    cat_antiga = ultima.categoria
    ultima.categoria = nova_cat; db.session.commit()
    desc = ultima.descricao.replace('[conjunta] ','').lower()
    palavras = [w for w in re.findall(r"[a-zà-ú&']+", desc)
                if len(w)>1 and w not in ['gastei','paguei','comprei','almocei','euros','euro','no','na','em']]
    aprendido = ''
    if palavras:
        chave = palavras[-1]
        if guardar_aprendida(chave, nova_cat):
            aprendido = f"\n🧠 Aprendi: '{chave}' = {nova_cat.capitalize()} p/ sempre!"
    enviar_mensagem(phone_raw, f"{EMOJI_CAT.get(nova_cat,'💳')} Corrigido! {cat_antiga.capitalize()} → {nova_cat.capitalize()}{aprendido}")

# ─── GASOLINA ────────────────────────────────────────────────
MUNICIPIOS_DGEG = {
    'barreiro':223,'moita':225,'montijo':226,'seixal':229,
    'almada':222,'setubal':231,'setúbal':231,'palmela':227,
    'alcochete':221,'grandola':224,'sesimbra':230,'sines':232,
}

def buscar_postos_dgeg(ids, id_comb='3201'):
    import requests as req
    todos = []
    for idm in ids:
        try:
            r = req.get("https://precoscombustiveis.dgeg.gov.pt/api/PrecoComb/PesquisarPostos",
                params={'qtdPorPagina':'500','pagina':'1','idsTiposComb':id_comb,
                        'idMarca':'','idTipoPosto':'','idDistrito':'15','idsMunicipios':str(idm),'placeId':''},
                timeout=25, headers={'User-Agent':'Mozilla/5.0'})
            log.info(f'DGEG {idm}: {r.status_code}')
            for p in (r.json().get('resultado') or []):
                try:
                    preco = float(str(p.get('Preco','')).replace(' €/litro','').replace('€','').replace(',','.').strip())
                    if preco > 0: todos.append({'nome':p.get('Nome','?'),'marca':p.get('Marca',''),'preco':preco})
                except: pass
        except Exception as e: log.error(f'DGEG {idm}: {e}')
    return sorted(todos, key=lambda x:x['preco'])

def gasolina_barata(phone_raw, texto):
    ids=[]; nomes=[]
    for chave, idm in MUNICIPIOS_DGEG.items():
        if chave in texto: ids.append(idm); nomes.append(chave.capitalize())
    if not ids: ids=[223,225]; nomes=['Barreiro','Moita']
    postos = buscar_postos_dgeg(ids)
    if not postos:
        enviar_mensagem(phone_raw, "⛽ Nao consegui buscar agora 😕\nhttps://precoscombustiveis.dgeg.gov.pt"); return
    zona = ' e '.join(nomes)
    msg = f"⛽ Gasolina 95 em {zona}:\n\n"
    medalhas = ['🥇','🥈','🥉','4️⃣','5️⃣']
    for i, p in enumerate(postos[:5]):
        marca = f" ({p['marca']})" if p['marca'] else ''
        msg += f"{medalhas[i]} {p['preco']:.3f}€/L — {p['nome'][:28]}{marca}\n"
    msg += "\n💡 Dados DGEG, hoje!"
    enviar_mensagem(phone_raw, msg)

# ─── DESPESA FUTURA ──────────────────────────────────────────
def processar_despesa_futura(phone_raw, usuario, texto):
    t = texto.lower()

    if any(p in t for p in ['afinal','cancela','cancelo','remove','apaga','nao tenho','não tenho']):
        palavras_chave = [w for w in re.findall(r'[a-zà-ú]+', t)
                         if w not in ['afinal','nao','não','tenho','cancela','cancelo','remove','apaga','que','vem','mes','mês','o','a']]
        futuras = DespesaFutura.query.filter(DespesaFutura.usuario_id==usuario.id, DespesaFutura.pago==False).all()
        if palavras_chave:
            chave = palavras_chave[0]
            removidas = [f for f in futuras if chave in f.descricao.lower()]
            if removidas:
                for f in removidas: db.session.delete(f)
                db.session.commit()
                enviar_mensagem(phone_raw, f"🗑️ Removido! '{removidas[0].descricao}' apagado das despesas futuras 👍")
            else:
                if futuras:
                    lista = '\n'.join(f"• {d.descricao} — {d.valor_total:.0f}€" for d in futuras)
                    enviar_mensagem(phone_raw, f"Nao encontrei '{chave}'. Tens:\n{lista}\n\nQual cancelas?")
                else:
                    enviar_mensagem(phone_raw, "Nao tens despesas futuras 😊")
        else:
            if futuras:
                lista = '\n'.join(f"• {d.descricao} — {d.valor_total:.0f}€" for d in futuras)
                enviar_mensagem(phone_raw, f"Qual queres cancelar?\n{lista}")
            else:
                enviar_mensagem(phone_raw, "Nao tens despesas futuras 😊")
        return

    valor = extrair_valor(texto)
    if valor == 0:
        enviar_mensagem(phone_raw, "Quanto vai custar? Ex: mes que vem dentista 40€"); return

    if 'dentista' in t: desc='Dentista'
    elif 'seguro' in t: desc='Seguro'
    elif 'inspe' in t: desc='Inspecao'
    elif 'renda' in t: desc='Renda'
    else:
        palavras = [w for w in texto.split() if not re.match(r'[0-9€,.]',w) and len(w)>3
                    and w.lower() not in ['mes','mês','que','vem','tenho','este','esse','proximo','próximo','afinal']]
        desc = ' '.join(palavras[:2]).capitalize() if palavras else 'Despesa futura'

    meses = 2 if ('2 meses' in t or 'dois meses' in t) else (3 if '3 meses' in t else 1)
    dia_match = re.search(r'dia (\d{1,2})', t)
    dia = int(dia_match.group(1)) if dia_match else None

    hoje = agora().replace(tzinfo=None)
    if dia:
        mes_alvo = hoje.month + meses; ano_alvo = hoje.year
        while mes_alvo > 12: mes_alvo -= 12; ano_alvo += 1
        try: data_prev = hoje.replace(year=ano_alvo, month=mes_alvo, day=min(dia,28))
        except: data_prev = hoje + timedelta(days=30*meses)
    else:
        data_prev = hoje + timedelta(days=30*meses)

    reserva = round(valor/meses, 2)
    db.session.add(DespesaFutura(usuario_id=usuario.id, descricao=desc, valor_total=valor,
        valor_reserva_mensal=reserva, meses=meses, data_prevista=data_prev))
    db.session.commit()

    dia_txt = f" (dia {dia})" if dia else ""
    when_txt = f"este mes{dia_txt}" if ('este mes' in t or 'este mês' in t) else f"daqui a {meses} mes{'es' if meses>1 else ''}{dia_txt}"
    enviar_mensagem(phone_raw, f"📅 Anotado! {desc}: {valor:.0f}€ {when_txt}\nGuardo {reserva:.0f}€/mes p/ isso 👍\n\nPara cancelar: 'afinal nao tenho {desc.lower()}'")

# ─── SIMULAR ─────────────────────────────────────────────────
def simular_compra(phone_raw, usuario, texto):
    valor = extrair_valor(texto)
    disp  = calcular_disponivel(usuario)
    if valor == 0:
        enviar_mensagem(phone_raw, f"💚 Tens {disp:.2f}€ para gastar este mes."); return
    if disp <= 0:
        enviar_mensagem(phone_raw, f"🔴 Nem penses! Ja nao tens orcamento 😅"); return
    pct = valor/disp*100
    if pct <= 30:    resp = f"✅ Vai nessa! {valor:.0f}€ e so {pct:.0f}% do disponivel. Ficas com {disp-valor:.0f}€ 🛍️"
    elif pct <= 60:  resp = f"🟡 Da, mas pesa. Ficas com {disp-valor:.0f}€. Precisas mesmo?"
    elif pct <= 100: resp = f"🟠 Tecnicamente sim mas ficas quase a zero ({disp-valor:.0f}€). Cuidado!"
    else:            resp = f"🔴 Nao da. Faltam {valor-disp:.0f}€. Deixa p/ o mes que vem 😬"
    enviar_mensagem(phone_raw, resp)

# ─── BOAS VINDAS ─────────────────────────────────────────────
def enviar_boas_vindas(phone_raw, usuario=None):
    dias = dias_para_salario()
    tem_salario = usuario and usuario.salario_liquido

    if not tem_salario:
        intro = f"Ola! 👋 Sou o Ze das Financas, o teu novo bestie financeiro! 💸\n\nSe ainda nao recebi o teu salario fico meio inutil, mas nao te preocupes — daqui a uns {dias} dia{'s' if dias!=1 else ''} vamos ser inseparaveis! 😄\n\nEntretanto podes comecar a registar gastos e eu fico a conhecer os teus habitos:\n• 15 bk | 25 conti | 50 galp\n• foto ou audio do gasto\n• PDF ou print do recibo\n\nEste sistema esta sempre a crescer — tens sugestoes de coisas fixes que eu devia saber fazer? Diz! 🚀\n\nDiz 'ajuda' para veres tudo o que ja sei 😎"
    else:
        disp = calcular_disponivel(usuario)
        intro = f"Ola de volta! 👋 Ainda tens {disp:.0f}€ para gastar este mes 💚\n\nTens sugestoes de melhorias p/ mim? Estou sempre a aprender! 🧠\n\nDiz 'ajuda' se precisares de algo 😎"

    enviar_mensagem(phone_raw, intro)

def enviar_ajuda(phone_raw):
    enviar_mensagem(phone_raw, """😎 Tudo o que sei fazer:

💸 Gastos:
• 15 bk | 25 conti | 50 galp
• foto do talao | audio | PDF do recibo
• jantar 30 na conjunta

📊 Consultas:
• resumo | plano | quanto tenho
• quanto tenho na conjunta
• score (+ conquistas)
• resumo anterior

💰 Salario:
• recebi 1300 euros
• [manda o PDF/foto do recibo]

🎯 Planear:
• posso comprar tenis 90€?
• mes que vem dentista 40€ dia 15
• afinal nao tenho dentista

⛽ Gasolina:
• gasolina mais barata no barreiro

🆘 Modo teso:
• estou teso

🧠 Ensinar:
• aprende que X e roupa
• corrige para roupa

💡 Tens sugestoes? Manda! Estou sempre a melhorar 🚀""")

# ─── IA ──────────────────────────────────────────────────────
def perguntar_ia(texto, usuario):
    try:
        from groq import Groq
        disp = calcular_disponivel(usuario)
        sys = f"""Es o Ze das Financas, assistente financeiro portugues criado pelo tuga27.
Falas portugues europeu informal, curto e com piada. Es querido e motivador.
Sabes: BK=Burger King, Mac=McDonald's, conti=Continente, PD=Pingo Doce, galp/bp=gasolina, JD=JD Sports.
Disponivel: {disp:.0f}€. Salario: {usuario.salario_liquido or '?'}€.
Responde em max 2 linhas com 1 emoji. Nunca digas que BK e Banco de Portugal."""
        resp = Groq(api_key=GROQ_API_KEY).chat.completions.create(
            model='llama-3.3-70b-versatile',
            messages=[{'role':'system','content':sys},{'role':'user','content':texto}],
            max_tokens=150)
        return resp.choices[0].message.content
    except Exception as e:
        log.error(f'IA: {e}'); return "Nao percebi 🤔 Diz 'ajuda' p/ veres o que sei!"

# ─── SCHEDULER ───────────────────────────────────────────────
def lembrete_recibo():
    with app.app_context():
        hoje = agora()
        dia_rec = dia_recibo_mes(hoje.year, hoje.month)
        if hoje.day == dia_rec.day and hoje.hour == 11:
            for u in Usuario.query.all():
                if u.phone:
                    set_estado(u.phone, 'aguardar_recibo', {})
                    enviar_mensagem(f"{u.phone}@lid",
                        "Ola! 📄 Hoje deve ter chegado o teu recibo!\nJa recebeste? Queres mandar o PDF/foto ou preferes dizer o valor?")

def lembrete_salario():
    with app.app_context():
        hoje = agora()
        dia_pag = dia_pagamento_mes(hoje.year, hoje.month)
        if hoje.day == dia_pag.day and hoje.hour == 9:
            for u in Usuario.query.all():
                if u.phone:
                    enviar_mensagem(f"{u.phone}@lid", "💰 Hoje e dia de salario! Ja recebeste? Manda o recibo ou diz o valor 🚀")

def fecho_mes():
    with app.app_context():
        hoje = agora()
        dia_pag = dia_pagamento_mes(hoje.year, hoje.month)
        if hoje.day == dia_pag.day and hoje.hour == 10:
            mes_ant = hoje.month-1 if hoje.month>1 else 12
            nomes = ['Janeiro','Fevereiro','Marco','Abril','Maio','Junho','Julho','Agosto','Setembro','Outubro','Novembro','Dezembro']
            for u in Usuario.query.all():
                if not u.phone: continue
                estado, dados = get_estado(u.phone)
                if estado == 'fecho_feito' and dados.get('mes')==hoje.month and dados.get('ano')==hoje.year:
                    continue
                enviar_mensagem(f"{u.phone}@lid",
                    f"📅 Novo mes financeiro!\nDiz 'resumo anterior' p/ veres como correu {nomes[mes_ant-1]} 📊")

def aviso_meio_mes():
    with app.app_context():
        hoje = agora()
        if hoje.day == 15 and hoje.hour == 10:
            for u in Usuario.query.all():
                if u.phone and u.salario_liquido:
                    disp = calcular_disponivel(u)
                    gastar = GASTAR_SUBSIDIO if hoje.month in [6,11] else GASTAR_NORMAL
                    pct = (gastar-disp)/gastar*100 if gastar>0 else 0
                    if pct > 70:
                        enviar_mensagem(f"{u.phone}@lid",
                            f"⚠️ A meio do mes e ja usaste {pct:.0f}% do orcamento!\nAo ritmo atual podes passar o limite. Nos proximos dias vai com calma! 💪")

def resumo_semanal():
    with app.app_context():
        if agora().weekday()==0 and agora().hour==9:
            for u in Usuario.query.all():
                if u.phone: enviar_resumo(f"{u.phone}@lid", u)

def verificar_despesas_futuras():
    with app.app_context():
        amanha = agora().replace(tzinfo=None) + timedelta(days=1)
        for d in DespesaFutura.query.filter(DespesaFutura.pago==False).all():
            if d.data_prevista and d.data_prevista.date() <= amanha.date():
                u = Usuario.query.get(d.usuario_id)
                if u and u.phone:
                    enviar_mensagem(f"{u.phone}@lid", f"⚠️ Lembrete: {d.descricao} — {d.valor_total:.0f}€ amanha!")

# ─── ARRANQUE ────────────────────────────────────────────────
with app.app_context():
    try: db.create_all()
    except Exception as e: log.warning(f"db: {e}")
    criar_tabelas()

scheduler.add_job(lembrete_recibo,           'cron', hour=11, minute=0)
scheduler.add_job(lembrete_salario,          'cron', hour=9,  minute=0)
scheduler.add_job(fecho_mes,                 'cron', hour=10, minute=0)
scheduler.add_job(aviso_meio_mes,            'cron', hour=10, minute=0)
scheduler.add_job(resumo_semanal,            'cron', hour=9,  minute=30, day_of_week='mon')
scheduler.add_job(verificar_despesas_futuras,'cron', hour=8,  minute=0)
scheduler.start()
log.info("Ze das Financas v5 iniciado")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
