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
TAVILY_API_KEY = os.environ.get('TAVILY_API_KEY', '')

FUNDO_PCT        = 0.05
BASE_COMBUSTIVEL = 50

MODOS_POUPANCA = {
    'maximo':      {'gastar_pct': 0.20, 'poupar_pct': 0.80, 'emoji': '💎', 'nome': 'Maxima',
                    'desc': 'Modo monge 🧘 Poupes o maximo, gastas so o essencial.'},
    'equilibrado': {'gastar_pct': 0.30, 'poupar_pct': 0.70, 'emoji': '⚖️', 'nome': 'Equilibrado',
                    'desc': 'O meio termo. Poupas bem e ainda tens margem para viver.'},
    'relaxado':    {'gastar_pct': 0.45, 'poupar_pct': 0.55, 'emoji': '😎', 'nome': 'Relaxado',
                    'desc': 'Vives a vida mas ainda poupas. Sem stress.'},
}
MODO_DEFAULT = 'equilibrado'

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

# ─── CATEGORIAS E LOJAS ──────────────────────────────────────
LOJAS = {
    # Fast food
    'bk':'fastfood','burger king':'fastfood','mac':'fastfood','mc':'fastfood',
    'mcd':'fastfood','mcdonald':'fastfood',"mcdonald's":'fastfood','mcdonalds':'fastfood',
    'kfc':'fastfood','sbx':'fastfood','starbucks':'fastfood','sub':'fastfood','subway':'fastfood',
    'telepizza':'fastfood','dominos':'fastfood','nandos':'fastfood',
    # Restaurante
    'zen':'restaurante','sushi':'restaurante','alcochete':'restaurante',
    'cafe':'restaurante','café':'restaurante','kebab':'restaurante',
    'tasca':'restaurante','pizza':'restaurante','restaurante':'restaurante',
    'jantar':'restaurante','almoco':'restaurante','almoço':'restaurante',
    'lanche':'restaurante','snack':'restaurante','pastelaria':'restaurante',
    # Roupa
    'foot':'roupa','fl':'roupa','foot locker':'roupa','jd':'roupa','jd sports':'roupa',
    'snipes':'roupa','zara':'roupa','hm':'roupa','h&m':'roupa',
    'primark':'roupa','shein':'roupa','bershka':'roupa','nike':'roupa','nk':'roupa',
    'nke':'roupa','adidas':'roupa','ads':'roupa','adi':'roupa',
    'mango':'roupa','lefties':'roupa','subdued':'roupa','pull':'roupa',
    'stradivarius':'roupa','springfield':'roupa',
    # Tecnologia
    'apl':'tecnologia','apple':'tecnologia','sam':'tecnologia','smg':'tecnologia',
    'samsung':'tecnologia','ps':'tecnologia','psn':'tecnologia','xb':'tecnologia',
    'xbx':'tecnologia','wrt':'tecnologia','worten':'tecnologia','rp':'tecnologia',
    'fnac':'tecnologia','radio popular':'tecnologia',
    # Supermercado
    'conti':'supermercado','cnt':'supermercado','continente':'supermercado',
    'pd':'supermercado','pingo doce':'supermercado','pingo':'supermercado',
    'lidl':'supermercado','aldi':'supermercado','mercadona':'supermercado',
    'minipreco':'supermercado','intermarche':'supermercado',
    # Casa/IKEA
    'ik':'casa','ikea':'casa','zara home':'casa',
    # Combustível
    'bp':'combustivel','galp':'combustivel','repsol':'combustivel','shell':'combustivel',
    'prio':'combustivel','cepsa':'combustivel','gasolina':'combustivel',
    # Gota
    'gota':'gota','agua':'gota','água':'gota',
    # Saúde
    'farmacia':'saude','farmácia':'saude','wells':'saude','dentista':'saude','medico':'saude',
    # Pessoal
    'unhas':'pessoal','cabelo':'pessoal','cabeleireiro':'pessoal','estetica':'pessoal',
    # Carro
    'oficina':'carro','mecanico':'carro','seguro':'carro','portagem':'carro','via verde':'carro',
    # Lazer
    'cinema':'lazer','concerto':'lazer','bowling':'lazer','netflix':'subscricoes',
    'spotify':'subscricoes','disney':'subscricoes',
}
LOJAS_NOME = {
    'bk':'Burger King','mac':"McDonald's",'mc':"McDonald's",'mcd':"McDonald's",
    'mcdonald':"McDonald's",'mcdonalds':"McDonald's",'kfc':'KFC','sbx':'Starbucks',
    'starbucks':'Starbucks','sub':'Subway','subway':'Subway','zen':'Zen Sushi',
    'foot':'Foot Locker','fl':'Foot Locker','jd':'JD Sports','snipes':'Snipes',
    'zara':'Zara','hm':'H&M','nike':'Nike','nk':'Nike','adidas':'Adidas',
    'ads':'Adidas','adi':'Adidas','apl':'Apple','sam':'Samsung','smg':'Samsung',
    'ps':'PlayStation','psn':'PlayStation','xb':'Xbox','xbx':'Xbox',
    'wrt':'Worten','worten':'Worten','rp':'Radio Popular','fnac':'FNAC',
    'conti':'Continente','cnt':'Continente','continente':'Continente',
    'pd':'Pingo Doce','pingo doce':'Pingo Doce','pingo':'Pingo Doce',
    'lidl':'Lidl','aldi':'Aldi','ikea':'IKEA','ik':'IKEA',
    'bp':'BP','galp':'Galp','repsol':'Repsol','shell':'Shell','prio':'Prio','cepsa':'Cepsa',
    'gota':'Gota','agua':'Água',
}
EMOJI_CAT = {
    'fastfood':'🍔','restaurante':'🍽️','roupa':'👗','tecnologia':'📱',
    'supermercado':'🛒','combustivel':'⛽','gota':'🧃','saude':'💊',
    'pessoal':'💅','carro':'🚗','lazer':'🎭','subscricoes':'📺',
    'casa':'🏠','outros':'💳',
}
CATEGORIAS_VALIDAS = list(EMOJI_CAT.keys())
ALIAS_CAT = {
    'comida':'supermercado','mercado':'supermercado','super':'supermercado',
    'fast food':'fastfood','hamburguer':'fastfood','burger':'fastfood',
    'restaurantes':'restaurante','sushi':'restaurante','pizza':'restaurante',
    'kebab':'restaurante','jantar':'restaurante','almoco':'restaurante',
    'roupas':'roupa','sapatilhas':'roupa','tenis':'roupa','sapatos':'roupa',
    'sneakers':'roupa','calcado':'roupa','moda':'roupa',
    'tech':'tecnologia','eletronica':'tecnologia','gaming':'tecnologia',
    'gasolina':'combustivel','gasoleo':'combustivel','posto':'combustivel',
    'agua':'gota','bebida':'gota','bebidas':'gota',
    'farmacia':'saude','medico':'saude','dentista':'saude',
    'unhas':'pessoal','cabelo':'pessoal','beleza':'pessoal',
    'automovel':'carro','oficina':'carro','portagem':'carro',
    'cinema':'lazer','diversao':'lazer','concerto':'lazer',
    'netflix':'subscricoes','subscricao':'subscricoes','spotify':'subscricoes',
    'casa':'casa','decoracao':'casa','mobilia':'casa','ikea':'casa','home':'casa',
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

# ─── BD: TABELAS E HELPERS ───────────────────────────────────
def criar_tabelas():
    sqls = [
        "CREATE TABLE IF NOT EXISTS aprendizagem (chave VARCHAR(100) PRIMARY KEY, categoria VARCHAR(50) NOT NULL)",
        "CREATE TABLE IF NOT EXISTS estado_utilizador (phone VARCHAR(50) PRIMARY KEY, estado VARCHAR(100), dados TEXT, atualizado TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
        "CREATE TABLE IF NOT EXISTS badges (id SERIAL PRIMARY KEY, usuario_id INTEGER, badge VARCHAR(100), obtido_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
        "CREATE TABLE IF NOT EXISTS pessoas_gastos (id SERIAL PRIMARY KEY, usuario_id INTEGER, despesa_id INTEGER, pessoa VARCHAR(100))",
        "CREATE TABLE IF NOT EXISTS reserva_emergencia (usuario_id INTEGER PRIMARY KEY, saldo FLOAT DEFAULT 0, atualizado TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
        "CREATE TABLE IF NOT EXISTS modo_poupanca (usuario_id INTEGER PRIMARY KEY, modo VARCHAR(20) DEFAULT 'equilibrado')",
        "CREATE TABLE IF NOT EXISTS aniversarios (id SERIAL PRIMARY KEY, usuario_id INTEGER, nome VARCHAR(100), data_aniv DATE)",
        "CREATE TABLE IF NOT EXISTS wishlist (id SERIAL PRIMARY KEY, usuario_id INTEGER, descricao VARCHAR(200), preco FLOAT, link VARCHAR(500), marca VARCHAR(100), categoria VARCHAR(50), estacao VARCHAR(20), comprado BOOLEAN DEFAULT FALSE, criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
        "CREATE TABLE IF NOT EXISTS splitting (id SERIAL PRIMARY KEY, usuario_id INTEGER, descricao VARCHAR(200), valor_total FLOAT, valor_cada FLOAT, pessoa VARCHAR(100), pago BOOLEAN DEFAULT FALSE, criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
        "CREATE TABLE IF NOT EXISTS km_combustivel (id SERIAL PRIMARY KEY, usuario_id INTEGER, km INTEGER, litros FLOAT, valor FLOAT, consumo_l100 FLOAT, custo_km FLOAT, data TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
        "CREATE TABLE IF NOT EXISTS objetivos_poupanca (id SERIAL PRIMARY KEY, usuario_id INTEGER, descricao VARCHAR(200), valor_objetivo FLOAT, valor_atual FLOAT DEFAULT 0, concluido BOOLEAN DEFAULT FALSE, criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
        "CREATE TABLE IF NOT EXISTS marcos_objetivo (id SERIAL PRIMARY KEY, objetivo_id INTEGER, marco INTEGER)",
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

def get_modo(usuario_id):
    try:
        r = db.session.execute(text("SELECT modo FROM modo_poupanca WHERE usuario_id=:id"), {'id':usuario_id}).fetchone()
        return r[0] if r else MODO_DEFAULT
    except Exception:
        db.session.rollback(); return MODO_DEFAULT

def set_modo(usuario_id, modo):
    try:
        db.session.execute(
            text("INSERT INTO modo_poupanca (usuario_id,modo) VALUES (:id,:m) ON CONFLICT (usuario_id) DO UPDATE SET modo=:m"),
            {'id':usuario_id,'m':modo})
        db.session.commit()
    except Exception as e:
        log.error(f"set_modo: {e}"); db.session.rollback()

def get_reserva(usuario_id):
    try:
        r = db.session.execute(text("SELECT saldo FROM reserva_emergencia WHERE usuario_id=:id"), {'id':usuario_id}).fetchone()
        return float(r[0]) if r else 0.0
    except Exception:
        db.session.rollback(); return 0.0

def set_reserva(usuario_id, saldo):
    try:
        db.session.execute(
            text("INSERT INTO reserva_emergencia (usuario_id,saldo,atualizado) VALUES (:id,:s,NOW()) ON CONFLICT (usuario_id) DO UPDATE SET saldo=:s,atualizado=NOW()"),
            {'id':usuario_id,'s':max(0,saldo)})
        db.session.commit()
    except Exception as e:
        log.error(f"set_reserva: {e}"); db.session.rollback()

# ─── CÁLCULOS ────────────────────────────────────────────────
def calcular_plano(salario, modo='equilibrado', despesas_futuras_valor=0):
    mes = agora().month
    fixos = {'carro':350,'ordem':20,'conjunta':50,'unhas':50 if mes<=9 else 25,'combustivel':BASE_COMBUSTIVEL}
    if despesas_futuras_valor > 0:
        fixos['despesas_mes'] = despesas_futuras_valor
    total_fixos = sum(fixos.values())
    fundo = round(salario * FUNDO_PCT, 2)
    sobra = max(salario - total_fixos - fundo, 0)
    m = MODOS_POUPANCA.get(modo, MODOS_POUPANCA[MODO_DEFAULT])
    gastar   = round(sobra * m['gastar_pct'], 2)
    poupanca = round(sobra * m['poupar_pct'], 2)
    return {**fixos,'total_fixos':total_fixos,'salario':salario,'fundo':fundo,'sobra':sobra,
            'gastar':gastar,'poupanca':poupanca,'modo':modo,'subsidio':mes in [6,11]}

def calcular_disponivel(usuario):
    mes=agora().month; ano=agora().year
    modo = get_modo(usuario.id)
    futuras = db.session.query(db.func.sum(DespesaFutura.valor_reserva_mensal)).filter(
        DespesaFutura.usuario_id==usuario.id, DespesaFutura.pago==False).scalar() or 0
    p = calcular_plano(usuario.salario_liquido or 0, modo, futuras)
    gastos = db.session.query(db.func.sum(Despesa.valor)).filter(
        Despesa.usuario_id==usuario.id,
        db.extract('month',Despesa.data)==mes, db.extract('year',Despesa.data)==ano,
        ~Despesa.descricao.like('[conjunta]%'),
        ~Despesa.descricao.like('[reserva]%'),
    ).scalar() or 0
    return p['gastar'] - gastos, p

def gastos_cat_mes(usuario, cat, mes, ano):
    return db.session.query(db.func.sum(Despesa.valor)).filter(
        Despesa.usuario_id==usuario.id, Despesa.categoria==cat,
        db.extract('month',Despesa.data)==mes, db.extract('year',Despesa.data)==ano
    ).scalar() or 0

def extrair_valor(texto):
    padrao = re.findall(r'\d[\d.,]*\d|\d+', texto)
    for n in padrao:
        try:
            if '.' in n and ',' in n: v = float(n.replace('.','').replace(',','.'))
            elif ',' in n: v = float(n.replace(',','.'))
            elif '.' in n:
                decimais = n[n.rfind('.')+1:]
                v = float(n.replace('.','')) if (len(decimais)==3 and n.replace('.','').isdigit()) else float(n)
            else: v = float(n)
            if v > 0: return v
        except: continue
    return 0

def tem_numero(texto):
    return bool(re.search(r'[0-9]+', texto))

def eh_gasto(texto):
    t = texto.lower()
    verbos = ['gastei','paguei','comprei','almocei','jantei','custou','meti','abasteci','lanchei','fui ao','fui à']
    if any(v in t for v in verbos): return True
    if '€' in t or ' euro' in t or 'euros' in t: return True
    cat, _, _ = categorizar(texto)
    return cat != 'outros'

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
        if from_me or (isinstance(msg_id,str) and msg_id.startswith('true_')): return jsonify({'status':'ok'})
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
            mime = media.get('mimetype',''); url = media.get('url','')
            if 'audio' in mime or 'ogg' in mime:
                transcrito = transcrever_audio(url)
                if transcrito:
                    enviar_mensagem(phone_raw, f'🎤 Percebi: "{transcrito}"'); texto = transcrito
                else:
                    enviar_mensagem(phone_raw, "Nao percebi 😕 Escreve!"); return jsonify({'status':'ok'})
            elif 'image' in mime:
                resultado = ler_foto_talao(url, mime)
                if resultado:
                    e_salario = 'SALARIO' in resultado.upper()
                    valor_lido = extrair_valor(resultado)
                    if e_salario and valor_lido > 200:
                        enviar_mensagem(phone_raw, f'📸 Vi no recibo: {valor_lido:.2f}€ — e esse o teu salario?')
                        set_estado(phone, 'confirmar_salario', {'valor': valor_lido})
                    else:
                        u_temp = Usuario.query.filter_by(phone=phone).first()
                        if u_temp and ler_etiqueta_wishlist(phone_raw, u_temp, url, mime):
                            return jsonify({'status':'ok'})
                        enviar_mensagem(phone_raw, f'📸 Li: {resultado}'); texto = resultado
                else:
                    u_temp = Usuario.query.filter_by(phone=phone).first()
                    if u_temp and ler_foto_km(phone_raw, u_temp, url, mime): return jsonify({'status':'ok'})
                    if u_temp and ler_etiqueta_wishlist(phone_raw, u_temp, url, mime): return jsonify({'status':'ok'})
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
        log.info(f"Msg de {phone}: {texto[:100]}")
        with app.app_context():
            processar_texto(phone_raw, phone, texto)
    except Exception as e:
        log.error(f'Webhook: {e}', exc_info=True)
    return jsonify({'status':'ok'})

@app.route('/', methods=['GET'])
def health():
    return jsonify({'status':'ok','bot':'Ze das Financas v7'})

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
            model='whisper-large-v3',
            file=(os.path.basename(fname), open(fname,'rb').read()),
            language='pt',
            prompt='Gastos em euros. Lojas: Continente, BK, McDonald, Galp, Zara.')
        try: os.unlink(fname)
        except: pass
        return t.text.strip()
    except Exception as e:
        log.error(f'Audio: {e}', exc_info=True); return ''

def ler_foto_talao(url, mimetype='image/jpeg'):
    try:
        from groq import Groq
        c = baixar_media(url)
        if not c: return ''
        mt = 'image/png' if 'png' in mimetype else 'image/jpeg'
        img = base64.b64encode(c).decode()
        resp = Groq(api_key=GROQ_API_KEY).chat.completions.create(
            model='meta-llama/llama-4-scout-17b-16e-instruct', max_tokens=80,
            messages=[{'role':'user','content':[
                {'type':'image_url','image_url':{'url':f'data:{mt};base64,{img}'}},
                {'type':'text','text':'Le este documento. Se recibo salario responde: "X,XX euros SALARIO". Se talao compra responde: "X,XX euros LOJA". Exemplo: "1327,92 euros SALARIO" ou "25,50 euros Continente". Se nao deres: erro'}
            ]}])
        txt = resp.choices[0].message.content.strip()
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

# ─── PROCESSAR TEXTO ─────────────────────────────────────────
def processar_texto(phone_raw, phone, texto):
    try:
        usuario = Usuario.query.filter_by(phone=phone).first()
        if not usuario:
            usuario = Usuario(phone=phone, nome='Luana')
            db.session.add(usuario); db.session.commit()

        t = texto.lower().strip()

        # ── ESTADOS (tratados antes de tudo) ──
        estado, dados_estado = get_estado(phone)

        if estado == 'confirmar_salario':
            if any(p in t for p in ['sim','yes','correto','certo','exato','e isso','é isso']):
                valor = dados_estado.get('valor', 0); limpar_estado(phone)
                processar_receita(phone_raw, usuario, f"recebi {valor}")
            elif tem_numero(texto):
                limpar_estado(phone); processar_receita(phone_raw, usuario, texto)
            else:
                limpar_estado(phone); enviar_mensagem(phone_raw, "Ok, diz: recebi X euros 💰")
            return

        if estado == 'aguardar_recibo':
            if any(p in t for p in ['sim','yes','quero','manda','envia']):
                limpar_estado(phone); enviar_mensagem(phone_raw, "Manda o PDF ou foto do recibo 📄")
            elif any(p in t for p in ['nao','não','valor','digo']) or tem_numero(texto):
                limpar_estado(phone)
                if tem_numero(texto): processar_receita(phone_raw, usuario, texto)
                else: enviar_mensagem(phone_raw, "Ok, diz: recebi X euros 💰")
            else:
                # nao reconheceu — limpa estado e processa normalmente
                limpar_estado(phone)
                processar_texto(phone_raw, phone, texto)
            return

        # NOTA: estado 'escolher_modo' NAO bloqueia mais o bot
        # O utilizador pode escolher modo mas continuar a usar o bot normalmente
        if estado == 'escolher_modo':
            if 'maximo' in t or 'máximo' in t or t.strip() == 'modo 1' or t.strip() == 'opcao 1':
                set_modo(usuario.id, 'maximo'); limpar_estado(phone)
                enviar_mensagem(phone_raw, "💎 Modo Maxima ativado! Modo monge ON 🧘"); return
            elif 'equilibrado' in t or t.strip() == 'modo 2' or t.strip() == 'opcao 2':
                set_modo(usuario.id, 'equilibrado'); limpar_estado(phone)
                enviar_mensagem(phone_raw, "⚖️ Modo Equilibrado ativado! 😊"); return
            elif 'relaxado' in t or t.strip() == 'modo 3' or t.strip() == 'opcao 3':
                set_modo(usuario.id, 'relaxado'); limpar_estado(phone)
                enviar_mensagem(phone_raw, "😎 Modo Relaxado ativado!"); return
            # Se nao escolheu modo, continua a processar normalmente (nao bloqueia!)

        # ── MUDAR MODO ──
        if any(p in t for p in ['muda modo','modo maximo','modo equilibrado','modo relaxado','alterar modo']):
            if 'maximo' in t or 'máximo' in t:
                set_modo(usuario.id, 'maximo')
                enviar_mensagem(phone_raw, "💎 Modo Maxima ativado!"); return
            elif 'equilibrado' in t:
                set_modo(usuario.id, 'equilibrado')
                enviar_mensagem(phone_raw, "⚖️ Modo Equilibrado ativado!"); return
            elif 'relaxado' in t:
                set_modo(usuario.id, 'relaxado')
                enviar_mensagem(phone_raw, "😎 Modo Relaxado ativado!"); return
            else:
                mostrar_modos(phone_raw); return

        # ── RESERVA ──
        # Deteta "gastei/usei/tirei X da reserva" com número no meio
        if re.search(r'(?:gastei|usei|tirei|meti|fui|busquei).{0,20}reserva', t) or \
           re.search(r'reserva.{0,20}(?:gastei|usei|tirei)', t):
            processar_gasto_reserva(phone_raw, usuario, texto); return
        if any(p in t for p in ['quanto tenho na reserva','saldo da reserva','ver reserva','minha reserva']):
            r = get_reserva(usuario.id)
            enviar_mensagem(phone_raw, f"🛡️ Reserva de emergencia: {r:.2f}€\n\nPara usar: 'gastei 30 da reserva'"); return

        # ── ANIVERSÁRIOS ──
        if any(p in t for p in ['aniversario','aniversário','faz anos']) or t.strip() == 'aniversarios':
            processar_aniversario(phone_raw, usuario, texto); return

        # ── APRENDER ──
        m = re.search(r'aprende que (.+?) (?:é|e|sao|são) (?:da categoria |categoria )?(\w[\w\s]*)', t)
        if m:
            chave = m.group(1).strip().strip('"\'')
            cat = normalizar_categoria(m.group(2).strip())
            if cat in CATEGORIAS_VALIDAS:
                enviar_mensagem(phone_raw, f"🧠 Aprendido! '{chave}' = {cat.capitalize()} p/ sempre 😎") if guardar_aprendida(chave, cat) else enviar_mensagem(phone_raw, "Ops 😕")
            else:
                enviar_mensagem(phone_raw, f"Nao conheço '{m.group(2)}' 🤔\nCategorias: {', '.join(CATEGORIAS_VALIDAS)}")
            return

        # ── CORRIGIR ──
        if re.search(r'(?:corrige|corrigir|muda|mudar|afinal|isso é|isso e)\s+(?:para\s+)?(\w+)', t) and \
           any(p in t for p in ['corrige','corrigir','isso é','isso e','afinal era']):
            m2 = re.search(r'(?:corrige|corrigir|muda|mudar|afinal|isso é|isso e)\s+(?:para\s+)?(\w+)', t)
            if m2:
                cat = normalizar_categoria(m2.group(1))
                if cat in CATEGORIAS_VALIDAS:
                    corrigir_ultimo(phone_raw, usuario, cat); return

        # ── CRIADOR ──
        if any(p in t for p in ['quem criou','quem te fez','quem te criou','criador','quem te programou']):
            enviar_mensagem(phone_raw, "Fui criado pelo tuga27 🚀\nO mesmo genio por tras do Zeflix e agora do teu gestor financeiro 😎"); return

        # ── AJUDA / BOAS VINDAS ──
        if any(p in t for p in ['ajuda','help','/start','comandos']):
            enviar_ajuda(phone_raw); return

        if t in ['ola','olá','oi','boas','hey','hello'] or any(p in t for p in ['bom dia','boa tarde','boa noite']):
            enviar_boas_vindas(phone_raw, usuario, phone); return

        # ── MODO TESO ──
        if any(p in t for p in ['estou teso','tou teso','sem dinheiro','estou liso']):
            modo_teso(phone_raw, usuario); return

        # ── GASOLINA ──
        gasolina_keywords = ['gasolina mais barata','posto mais barato','gasolina barata',
                             'valor gasolina','preco gasolina','preço gasolina','onde e a gasolina',
                             'onde é a gasolina','gasolina mais barata','combustivel mais barato']
        municipios = ['barreiro','moita','seixal','almada','montijo','palmela','alcochete','setubal','setúbal']
        if any(p in t for p in gasolina_keywords) or \
           (any(m in t for m in municipios) and any(p in t for p in ['gasolina','combustivel','posto','barata','barato','preco','preço','valor'])):
            gasolina_barata(phone_raw, t); return

        # ── CONJUNTA ──
        if 'conjunta' in t and any(p in t for p in ['quanto','tenho','sobra','resta','ver']):
            enviar_conjunta(phone_raw, usuario); return

        # ── QUANTO TENHO / SALDO ──
        if any(p in t for p in ['quanto tenho','quanto me resta','quanto sobra','saldo']):
            enviar_quanto_tenho(phone_raw, usuario); return

        # ── RESUMO ──
        if any(p in t for p in ['resumo anterior','mes passado','mes anterior']):
            mes_ant = agora().month-1 if agora().month>1 else 12
            ano_ant = agora().year if agora().month>1 else agora().year-1
            enviar_resumo(phone_raw, usuario, mes_ant, ano_ant); return

        if any(p in t for p in ['resumo','como estou','quanto gastei','situacao','situação']):
            enviar_resumo(phone_raw, usuario); return

        # ── PLANO ──
        if any(p in t for p in ['plano','transferencia','transferência','distribuicao','ver plano']):
            enviar_plano_mes(phone_raw, usuario); return

        # ── SCORE ──
        if any(p in t for p in ['score','conquistas','badges','pontuacao']):
            enviar_score(phone_raw, usuario); return

        # ── OBJETIVO POUPANÇA ──
        if any(p in t for p in ['quero poupar','poupar para','objetivo de poupanca','meta de poupanca']):
            processar_objetivo_poupanca(phone_raw, usuario, texto); return
        if any(p in t for p in ['objetivos','ver objetivos','metas','ver metas']):
            processar_objetivo_poupanca(phone_raw, usuario, 'ver objetivos'); return

        # ── DESPESAS FUTURAS ──
        if any(p in t for p in ['afinal nao','afinal não','cancela','cancelo']):
            processar_despesa_futura(phone_raw, usuario, texto); return

        if any(p in t for p in ['mes que vem','mês que vem','proximo mes','próximo mês',
                                  'este mes tenho','este mês tenho']) and tem_numero(texto):
            processar_despesa_futura(phone_raw, usuario, texto); return

        if any(p in t for p in ['dentista','seguro','inspecao','inspeção']) and \
           any(p in t for p in ['mes','mês']) and tem_numero(texto):
            processar_despesa_futura(phone_raw, usuario, texto); return

        # ── SIMULAR COMPRA ──
        if any(p in t for p in ['posso comprar','posso gastar','vale a pena','consigo comprar','devo comprar']):
            simular_compra(phone_raw, usuario, texto); return

        # ── RECEITA ──
        if any(p in t for p in ['recebi','ordenado','salario','salário','vencimento']) and tem_numero(texto):
            processar_receita(phone_raw, usuario, texto); return

        # ── RESUMO POR PESSOA ──
        if re.search(r'quanto gastei com|gastei com [a-z]', t):
            resumo_por_pessoa(phone_raw, usuario, texto); return

        # ── WISHLIST ──
        if any(p in t for p in ['wishlist','lista de desejos']):
            processar_wishlist(phone_raw, usuario, texto); return

        if any(p in t for p in ['quero comprar','adorei','vi e gostei','gostei disto']):
            processar_wishlist(phone_raw, usuario, texto); return

        # "quero X" só vai para wishlist se nao for gasto
        if t.startswith('quero ') and not any(p in t for p in ['poupar','gastar']):
            if not eh_gasto(texto):
                processar_wishlist(phone_raw, usuario, texto); return

        if any(p in t for p in ['comprei o ','comprei a ','já comprei','ja comprei']):
            if tem_numero(texto): processar_despesa(phone_raw, usuario, texto)
            else: marcar_wishlist_comprado(phone_raw, usuario, texto)
            return

        if 'wishlist' in t and any(p in t for p in ['remove','apaga','tira']):
            remover_wishlist(phone_raw, usuario, texto); return

        # Link direto vai para wishlist
        if re.search(r'https?://\S+', texto):
            processar_wishlist(phone_raw, usuario, texto); return

        # ── SPLITTING ──
        if any(p in t for p in ['dividi','dividir','a meias','partilhei']) and tem_numero(texto):
            processar_splitting(phone_raw, usuario, texto); return

        if any(p in t for p in ['splits','divididos','o que me devem','pendentes']):
            ver_splits(phone_raw, usuario); return

        # ── MODO DISCRETO ──
        if any(p in t for p in ['limpa conversa','apaga mensagens','modo discreto','limpar chat']):
            modo_discreto(phone_raw); return

        # ── GASTO (texto/sem keyword) ──
        if tem_numero(texto) and eh_gasto(texto):
            processar_despesa(phone_raw, usuario, texto); return

        # ── IA FALLBACK ──
        enviar_mensagem(phone_raw, perguntar_ia(texto, usuario))

    except Exception as e:
        log.error(f'processar_texto: {e}', exc_info=True)
        enviar_mensagem(phone_raw, "Ocorreu um erro 😕 Tenta de novo!")

# ─── PROCESSAR DESPESA ───────────────────────────────────────
def processar_despesa(phone_raw, usuario, texto):
    valor = extrair_valor(texto)
    if valor == 0:
        enviar_mensagem(phone_raw, "Nao percebi o valor 🤔 Ex: 25 conti"); return

    categoria, emoji, nome_loja = categorizar(texto)
    na_conjunta = 'conjunta' in texto.lower()

    pessoa = None
    m_p = re.search(r'\bcom (?:a |o |as |os )?([A-Za-zÀ-ú]{2,})\b', texto, re.IGNORECASE)
    if m_p and m_p.group(1).lower() not in {'conjunta','ruben','a','o','os','as'}:
        pessoa = m_p.group(1).capitalize()

    descricao = ('[conjunta] ' if na_conjunta else '') + texto[:90]
    despesa = Despesa(usuario_id=usuario.id, valor=valor, categoria=categoria,
                      descricao=descricao, data=agora().replace(tzinfo=None))
    db.session.add(despesa); db.session.commit()

    if pessoa:
        try:
            db.session.execute(text("INSERT INTO pessoas_gastos (usuario_id,despesa_id,pessoa) VALUES (:u,:d,:p)"),
                               {'u':usuario.id,'d':despesa.id,'p':pessoa})
            db.session.commit()
        except Exception: db.session.rollback()

    mes=agora().month; ano=agora().year
    mes_ant=mes-1 if mes>1 else 12; ano_ant=ano if mes>1 else ano-1
    total_cat     = gastos_cat_mes(usuario, categoria, mes, ano)
    total_cat_ant = gastos_cat_mes(usuario, categoria, mes_ant, ano_ant)
    disp, p = calcular_disponivel(usuario)
    gastar = p['gastar']
    pct_usado = ((gastar-disp)/gastar*100) if gastar>0 else 0

    extra = ''
    inicio_semana = agora().replace(tzinfo=None) - timedelta(days=agora().weekday())
    vezes_semana = db.session.query(db.func.count(Despesa.id)).filter(
        Despesa.usuario_id==usuario.id, Despesa.categoria==categoria,
        Despesa.data>=inicio_semana).scalar() or 0

    if categoria=='fastfood' and vezes_semana>=3:
        extra = f'\n😏 Ja e a {vezes_semana}a vez de fast food esta semana!'
    elif categoria=='gota' and total_cat>30:
        extra = f'\n🧃 {total_cat:.0f}€ em bebidas este mes... abranda!'
    elif categoria=='combustivel':
        if total_cat>BASE_COMBUSTIVEL*1.5: extra=f'\n⛽ {total_cat:.0f}€ em gasolina, bem acima dos {BASE_COMBUSTIVEL}€!'
        elif total_cat>BASE_COMBUSTIVEL: extra=f'\n⛽ Passaste a base de {BASE_COMBUSTIVEL}€ em gasolina'
    elif agora().weekday() in [4,5] and agora().hour>=19 and categoria in ['restaurante','fastfood']:
        extra = '\n🍻 Fim de semana a noite, la vem o costume!'
    elif total_cat_ant>0 and total_cat>total_cat_ant*1.3:
        extra = f'\n⚠️ Ja gastaste mais em {categoria} que o mes passado todo!'
    elif total_cat_ant>0 and total_cat<total_cat_ant*0.6:
        extra = f'\n✅ Muito menos em {categoria} que o mes passado!'

    aviso = ''
    if pct_usado >= 100: aviso = f'\n\n🔴 Passaste o orcamento! {abs(disp):.0f}€ a mais.'
    elif pct_usado >= 80: aviso = f'\n\n🔔 Ja usaste {pct_usado:.0f}% do orcamento!'

    conjunta_txt = ' (conjunta 💑)' if na_conjunta else ''
    pessoa_txt   = f' (com {pessoa})' if pessoa else ''
    modo = get_modo(usuario.id)
    m_info = MODOS_POUPANCA[modo]
    msg = (f"{emoji} Bora! {nome_loja} {valor:.2f}€{conjunta_txt}{pessoa_txt}\n"
           f"{categoria.capitalize()}: {total_cat:.2f}€ este mes{extra}{aviso}\n"
           f"💚 Disponivel: {disp:.2f}€ {m_info['emoji']}")
    enviar_mensagem(phone_raw, msg)
    verificar_badges(usuario, phone_raw)

# ─── RECEITA / PLANO ─────────────────────────────────────────
def processar_receita(phone_raw, usuario, texto):
    valor = extrair_valor(texto)
    if valor == 0:
        enviar_mensagem(phone_raw, "Quanto recebeste? 💰"); return
    usuario.salario_liquido = valor
    db.session.add(Receita(usuario_id=usuario.id, valor=valor, descricao='Salario', data=agora().replace(tzinfo=None)))
    db.session.commit()
    enviar_plano_salario(phone_raw, usuario, valor)

def enviar_plano_salario(phone_raw, usuario, salario):
    modo = get_modo(usuario.id)
    futuras = DespesaFutura.query.filter(DespesaFutura.usuario_id==usuario.id, DespesaFutura.pago==False).all()
    total_fut = sum(d.valor_reserva_mensal for d in futuras)
    p = calcular_plano(salario, modo, total_fut)
    m = MODOS_POUPANCA[modo]

    msg  = f"💰 Boa, recebeste {salario:.2f}€! {m['emoji']}\n\n📋 Plano:\n"
    msg += f"🏠 Fixos: {p['total_fixos']:.0f}€\n"
    msg += f"   🚗{p['carro']:.0f} | 💼{p['ordem']:.0f} | 💅{p['unhas']:.0f} | 💑{p['conjunta']:.0f} | ⛽{p['combustivel']:.0f}"
    if total_fut > 0:
        msg += f"\n   📅 Despesas mes: {total_fut:.0f}€"
        for d in futuras: msg += f"\n     {d.descricao}: {d.valor_reserva_mensal:.0f}€"
    msg += f"\n🛡️ Fundo: {p['fundo']:.2f}€ (Revolut!)\n"
    msg += f"💳 Para gastar: {p['gastar']:.0f}€\n"
    msg += f"💎 Poupanca: {p['poupanca']:.0f}€"
    if p['subsidio']:
        msg += "\n\n🌴 Mes de subsidio! 😉"
        # Verifica se tem wishlist
        try:
            rows = db.session.execute(text(
                "SELECT descricao, preco FROM wishlist WHERE usuario_id=:id AND comprado=FALSE ORDER BY criado_em DESC LIMIT 3"),
                {'id': usuario.id}).fetchall()
            if rows:
                msg += "\n\n🛍️ Mes de subsidio = mes de mimar! Tens na wishlist:\n"
                for r in rows:
                    preco_txt = f" — {r[1]:.2f}€" if r[1] else ""
                    msg += f"• {r[0]}{preco_txt}\n"
                msg += "\nTu mereces! 💕"
            else:
                msg += "\n\nAproveira para comprar umas roupas para ti, tu mereces! 🛍️💕"
        except Exception:
            msg += "\n\nAproveira para comprar umas roupas para ti, tu mereces! 🛍️💕"
    if agora().month == 11: msg += "\n\n🎂 Este mes e o teu aniversario!! 100€ so para ti! 🎁"
    enviar_mensagem(phone_raw, msg)

    reserva_atual = get_reserva(usuario.id)
    if reserva_atual > 0:
        enviar_mensagem(phone_raw, f"🛡️ Reserva de emergencia: {reserva_atual:.2f}€ 💪")

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
        verificar_sobra_mes(phone_raw, usuario, mes_ant, ano_ant)
    else:
        enviar_mensagem(phone_raw, "💡 Primeiro mes! A partir de agora vou guardar tudo 💪")

    # Aniversários este mes
    try:
        mes_atual = agora().month
        rows = db.session.execute(text(
            "SELECT nome, data_aniv FROM aniversarios WHERE usuario_id=:id AND EXTRACT(month FROM data_aniv)=:m ORDER BY EXTRACT(day FROM data_aniv)"),
            {'id': usuario.id, 'm': mes_atual}).fetchall()
        if rows:
            nomes_mes = ['Janeiro','Fevereiro','Marco','Abril','Maio','Junho','Julho','Agosto','Setembro','Outubro','Novembro','Dezembro']
            msg_aniv = f"🎂 Aniversarios em {nomes_mes[mes_atual-1]}:\n"
            for r in rows:
                dias = r[1].day - agora().day
                alerta = " ⚠️ PROXIMO!" if 0 < dias <= 7 else (" 🎉 HOJE!" if dias == 0 else "")
                msg_aniv += f"• {r[0]} — dia {r[1].day}{alerta}\n"
            msg_aniv += "\nDiz 'aniversarios' para a lista completa 🎁"
            enviar_mensagem(phone_raw, msg_aniv)
    except Exception as e:
        log.error(f"anivs plano: {e}")

    phone = phone_raw.replace('@lid','').replace('@c.us','').split('@')[0]
    set_estado(phone, 'fecho_feito', {'mes':agora().month,'ano':agora().year})

def verificar_sobra_mes(phone_raw, usuario, mes, ano):
    modo = get_modo(usuario.id)
    futuras = DespesaFutura.query.filter(DespesaFutura.usuario_id==usuario.id, DespesaFutura.pago==False).all()
    total_fut = sum(d.valor_reserva_mensal for d in futuras)
    if not usuario.salario_liquido: return
    p = calcular_plano(usuario.salario_liquido, modo, total_fut)
    gastos_mes = db.session.query(db.func.sum(Despesa.valor)).filter(
        Despesa.usuario_id==usuario.id,
        db.extract('month',Despesa.data)==mes, db.extract('year',Despesa.data)==ano,
        ~Despesa.descricao.like('[conjunta]%'),
        ~Despesa.descricao.like('[reserva]%'),
    ).scalar() or 0
    sobrou = p['gastar'] - gastos_mes
    if sobrou > 5:
        nova_reserva = get_reserva(usuario.id) + sobrou
        set_reserva(usuario.id, nova_reserva)
        enviar_mensagem(phone_raw,
            f"🎉 Sobraram {sobrou:.2f}€ do orcamento! Ja meti na reserva 💪\n"
            f"🛡️ Reserva total: {nova_reserva:.2f}€")

# ─── QUANTO TENHO / CONJUNTA / RESUMO ────────────────────────
def enviar_quanto_tenho(phone_raw, usuario):
    disp, p = calcular_disponivel(usuario)
    reserva = get_reserva(usuario.id)
    modo = get_modo(usuario.id)
    m = MODOS_POUPANCA[modo]
    if disp < 0:
        msg = f"😬 Passaste o orcamento em {abs(disp):.2f}€!\n🛡️ Reserva: {reserva:.2f}€"
    elif disp < 20:
        msg = f"💸 So tens {disp:.2f}€ para gastar. Aperta o cinto!\n🛡️ Reserva: {reserva:.2f}€"
    else:
        msg = (f"💚 Tens {disp:.2f}€ para gastar {m['emoji']}\n"
               f"💎 Poupanca prevista: {p['poupanca']:.0f}€\n"
               f"🛡️ Reserva emergencia: {reserva:.2f}€")
    enviar_mensagem(phone_raw, msg)

def enviar_conjunta(phone_raw, usuario):
    mes=agora().month; ano=agora().year
    gasto = db.session.query(db.func.sum(Despesa.valor)).filter(
        Despesa.usuario_id==usuario.id,
        db.extract('month',Despesa.data)==mes, db.extract('year',Despesa.data)==ano,
        Despesa.descricao.like('[conjunta]%')).scalar() or 0
    resta = 50 - gasto
    estado_txt = "✅ Dentro!" if resta >= 0 else f"⚠️ Passaste {abs(resta):.0f}€!"
    enviar_mensagem(phone_raw,
        f"💑 Conjunta (jantares, cinema, lanches):\n💰 Tua parte: 50€\n"
        f"🛒 Gastaste: {gasto:.2f}€\n💚 Resta: {max(resta,0):.2f}€ {estado_txt}\n\n"
        f"Para marcar: 'jantar 30 na conjunta'")

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

    modo = get_modo(usuario.id)
    futuras = DespesaFutura.query.filter(DespesaFutura.usuario_id==usuario.id, DespesaFutura.pago==False).all()
    total_fut = sum(d.valor_reserva_mensal for d in futuras)
    p = calcular_plano(receita or 0, modo, total_fut)
    disp = p['gastar'] - gp
    nomes = ['Janeiro','Fevereiro','Marco','Abril','Maio','Junho','Julho','Agosto','Setembro','Outubro','Novembro','Dezembro']

    msg = f"📊 {nomes[mes-1]}\n\n💰 Receita: {receita:.0f}€\n🛒 Gastos: {gp:.2f}€"
    if gc > 0: msg += f"\n💑 Conjunta: {gc:.2f}€"
    msg += f"\n💚 Disponivel: {disp:.2f}€\n💎 Poupanca: {p['poupanca']:.0f}€\n\n📈 Categorias:"
    for cat, total in sorted(por_cat, key=lambda x:-x[1]):
        msg += f"\n{EMOJI_CAT.get(cat,'💳')} {cat.capitalize()}: {total:.2f}€"

    if not mes_override and agora().day > 3 and gp > 0:
        ritmo = gp/agora().day*30
        msg += f"\n\n🔮 Ao ritmo atual: ~{ritmo:.0f}€ no fim do mes"
        if por_cat:
            top_cat, top_val = max(por_cat, key=lambda x:x[1])
            if top_val > 50:
                msg += f"\n💡 Reduz {top_cat} em 30% → poupa ~{top_val*0.3*12:.0f}€/ano"
    enviar_mensagem(phone_raw, msg)

def enviar_plano_mes(phone_raw, usuario):
    if not usuario.salario_liquido:
        enviar_mensagem(phone_raw, "Ainda nao sei o teu salario 🤔 Diz: recebi 1300 euros"); return
    enviar_plano_salario(phone_raw, usuario, usuario.salario_liquido)

# ─── SCORE + BADGES ──────────────────────────────────────────
BADGES = {
    'primeiro_gasto':  ('🎉', 'Primeiro Gasto!'),
    'mes_dentro':      ('🏆', 'Mes Perfeito'),
    'poupadora':       ('💎', 'Poupadora'),
    'meta_gasolina':   ('⛽', 'Combustivel Ok'),
    'reserva_100':     ('🛡️', 'Reserva Solida'),
    'reserva_500':     ('🏰', 'Fortaleza'),
}

def verificar_badges(usuario, phone_raw):
    try:
        badges_ok = {r[0] for r in db.session.execute(
            text("SELECT badge FROM badges WHERE usuario_id=:id"), {'id':usuario.id}).fetchall()}
        novos = []
        mes=agora().month; ano=agora().year
        total = db.session.query(db.func.count(Despesa.id)).filter_by(usuario_id=usuario.id).scalar() or 0
        if total == 1 and 'primeiro_gasto' not in badges_ok: novos.append('primeiro_gasto')
        reserva = get_reserva(usuario.id)
        if reserva >= 100 and 'reserva_100' not in badges_ok: novos.append('reserva_100')
        if reserva >= 500 and 'reserva_500' not in badges_ok: novos.append('reserva_500')
        for badge in novos:
            db.session.execute(text("INSERT INTO badges (usuario_id,badge) VALUES (:id,:b)"),
                               {'id':usuario.id,'b':badge})
            db.session.commit()
            e, nome = BADGES[badge]
            enviar_mensagem(phone_raw, f"🎖️ CONQUISTA!\n{e} {nome}\nEstas a mandar! 🙌")
    except Exception as e:
        log.error(f"badges: {e}"); db.session.rollback()

def enviar_score(phone_raw, usuario):
    disp, p = calcular_disponivel(usuario)
    gastar = p['gastar']
    pct = (gastar-disp)/gastar*100 if gastar>0 else 0
    if pct < 50:    score, txt = 9, "Mestre da poupanca! 🏆"
    elif pct < 75:  score, txt = 7, "Vais bem, continua! 👍"
    elif pct < 100: score, txt = 5, "Cuidado com os gastos 😬"
    else:           score, txt = 2, "Passaste o orcamento 🔴"
    reserva = get_reserva(usuario.id)
    try:
        bl = db.session.execute(text("SELECT badge FROM badges WHERE usuario_id=:id"), {'id':usuario.id}).fetchall()
        bt = ('\n\n🎖️ Conquistas:\n' + '\n'.join(f"{BADGES[r[0]][0]} {BADGES[r[0]][1]}" for r in bl if r[0] in BADGES)) if bl else ''
    except Exception: bt = ''
    modo = get_modo(usuario.id); m = MODOS_POUPANCA[modo]
    enviar_mensagem(phone_raw, f"⭐ Score: {score}/10 {m['emoji']}\n{txt}\n🛡️ Reserva: {reserva:.2f}€{bt}")

# ─── MODO TESO ───────────────────────────────────────────────
def modo_teso(phone_raw, usuario):
    disp, _ = calcular_disponivel(usuario)
    reserva = get_reserva(usuario.id)
    dias = dias_para_salario()
    msg = (f"😅 Modo teso!\n\n💚 Para gastar: {disp:.2f}€\n"
           f"🛡️ Reserva: {reserva:.2f}€ (so em emergencias!)\n"
           f"📅 ~{dias} dias p/ o salario\n\n"
           f"Dicas:\n🍳 Cozinha em casa\n🚶 Anda a pe\n"
           f"🛒 So o essencial\n☕ Cafe de maquina\n💪 Consegues!")
    enviar_mensagem(phone_raw, msg)

# ─── CORRIGIR ────────────────────────────────────────────────
def corrigir_ultimo(phone_raw, usuario, nova_cat):
    ultima = Despesa.query.filter_by(usuario_id=usuario.id).order_by(Despesa.id.desc()).first()
    if not ultima:
        enviar_mensagem(phone_raw, "Nao tenho nenhum gasto p/ corrigir 🤔"); return
    cat_antiga = ultima.categoria; ultima.categoria = nova_cat; db.session.commit()
    desc = ultima.descricao.replace('[conjunta] ','').replace('[reserva] ','').lower()
    stop = {'gastei','paguei','comprei','almocei','jantei','euros','euro','no','na','em',
            'da','do','de','reserva','conjunta','num','uma','uns','umas','com'}
    palavras = [w for w in re.findall(r"[a-zà-ú&']+", desc) if len(w)>2 and w not in stop]
    aprendido = ''
    if palavras:
        chave = palavras[-1]
        if guardar_aprendida(chave, nova_cat):
            aprendido = f"\n🧠 Aprendi: '{chave}' = {nova_cat.capitalize()}!"
    enviar_mensagem(phone_raw, f"{EMOJI_CAT.get(nova_cat,'💳')} Corrigido! {cat_antiga.capitalize()} → {nova_cat.capitalize()}{aprendido}")

# ─── GASTO RESERVA ───────────────────────────────────────────
def processar_gasto_reserva(phone_raw, usuario, texto):
    valor = extrair_valor(texto)
    if valor == 0:
        enviar_mensagem(phone_raw, "Quanto gastaste da reserva? Ex: gastei 30 da reserva"); return
    reserva_atual = get_reserva(usuario.id)
    nova_reserva = max(0, reserva_atual - valor)
    set_reserva(usuario.id, nova_reserva)
    despesa = Despesa(usuario_id=usuario.id, valor=valor, categoria='outros',
                      descricao=f'[reserva] {texto[:80]}', data=agora().replace(tzinfo=None))
    db.session.add(despesa); db.session.commit()
    msg = f"🛡️ Reserva usada: {valor:.2f}€\nReserva atual: {nova_reserva:.2f}€\n\nEspero que tenhas resolvido! 💪"
    if nova_reserva == 0: msg += "\n\nA reserva ficou a zero — repoe quando puderes!"
    enviar_mensagem(phone_raw, msg)

# ─── ANIVERSÁRIOS ────────────────────────────────────────────
def processar_aniversario(phone_raw, usuario, texto):
    t = texto.lower()
    if any(p in t for p in ['ver','lista','quais','mostrar']) or t.strip() in ['aniversarios','aniversários']:
        try:
            rows = db.session.execute(text(
                "SELECT nome, data_aniv FROM aniversarios WHERE usuario_id=:id ORDER BY EXTRACT(month FROM data_aniv), EXTRACT(day FROM data_aniv)"),
                {'id': usuario.id}).fetchall()
            if not rows:
                enviar_mensagem(phone_raw, "Nao tens aniversarios guardados 🎂\nAdiciona: 'aniversario da Ana 15/3'"); return
            meses = ['Jan','Fev','Mar','Abr','Mai','Jun','Jul','Ago','Set','Out','Nov','Dez']
            hoje = agora()
            msg = "🎂 Aniversarios:\n\n"; mes_atual = None
            for r in rows:
                if r[1].month != mes_atual:
                    mes_atual = r[1].month; msg += f"── {meses[r[1].month-1]} ──\n"
                dias_falta = (r[1].replace(year=hoje.year) - hoje.date()).days
                if dias_falta < 0: dias_falta += 365
                alerta = " 🔥" if dias_falta <= 5 else (" ⚠️" if dias_falta <= 14 else "")
                msg += f"• {r[0]} — dia {r[1].day}{alerta}\n"
            enviar_mensagem(phone_raw, msg)
        except Exception as e:
            log.error(f"anivs: {e}"); enviar_mensagem(phone_raw, "Erro 😕")
        return

    # Adicionar aniversário — formatos: "dia 15 marco", "15/3", "15-3"
    meses_map = {
        'janeiro':1,'fevereiro':2,'marco':3,'março':3,'abril':4,'maio':5,'junho':6,
        'julho':7,'agosto':8,'setembro':9,'outubro':10,'novembro':11,'dezembro':12,
        'jan':1,'fev':2,'mar':3,'abr':4,'mai':5,'jun':6,'jul':7,'ago':8,'set':9,'out':10,'nov':11,'dez':12
    }
    dia = mes_num = None
    # Formato DD/MM ou DD-MM
    m_data = re.search(r'(\d{1,2})[/\-](\d{1,2})', texto)
    if m_data:
        dia = int(m_data.group(1)); mes_num = int(m_data.group(2))
    else:
        # Formato "dia X mes"
        m_dm = re.search(r'dia (\d{1,2})(?:\s+de)?\s+([a-záàâãéêíóôõúç]+)', t)
        if m_dm:
            dia = int(m_dm.group(1)); mes_num = meses_map.get(m_dm.group(2))
        else:
            # Formato "X de mes" ou "X mes"
            m_xm = re.search(r'(\d{1,2})\s+(?:de\s+)?([a-záàâãéêíóôõúç]{3,})', t)
            if m_xm:
                dia = int(m_xm.group(1)); mes_num = meses_map.get(m_xm.group(2))

    # Extrai nome
    stop_n = {'aniversario','aniversário','faz','anos','dia','de','do','da','em','o','a','os','as','e','no','na'}
    m_nome = re.search(r'(?:d[aoe]\s+)([A-Za-zÀ-ú]{2,})', texto, re.IGNORECASE)
    if m_nome and m_nome.group(1).lower() not in stop_n and m_nome.group(1).lower() not in meses_map:
        nome = m_nome.group(1).capitalize()
    else:
        palavras = [w for w in re.findall(r'[A-Za-zÀ-ú]+', texto)
                    if w.lower() not in stop_n and w.lower() not in meses_map and len(w)>1]
        nome = palavras[0].capitalize() if palavras else None

    if nome and dia and mes_num and 1<=dia<=31 and 1<=mes_num<=12:
        try:
            data = f"2000-{mes_num:02d}-{min(dia,28):02d}"
            db.session.execute(text(
                "INSERT INTO aniversarios (usuario_id,nome,data_aniv) VALUES (:u,:n,:d) ON CONFLICT DO NOTHING"),
                {'u':usuario.id,'n':nome,'d':data})
            db.session.commit()
            meses_pt = ['Jan','Fev','Mar','Abr','Mai','Jun','Jul','Ago','Set','Out','Nov','Dez']
            enviar_mensagem(phone_raw, f"🎂 Anotado! {nome} faz anos a {dia} de {meses_pt[mes_num-1]}\nVou avisar antes! 🎉")
        except Exception as e:
            log.error(f"aniv add: {e}"); enviar_mensagem(phone_raw, "Erro 😕")
    else:
        enviar_mensagem(phone_raw, "Nao percebi bem 🤔 Tenta:\n• 'aniversario da Ana 15/3'\n• 'aniversario da Ana dia 15 marco'\n• 'aniversarios' para ver a lista")

# ─── PROCESSAR DESPESA FUTURA ────────────────────────────────
def processar_despesa_futura(phone_raw, usuario, texto):
    t = texto.lower()
    if any(p in t for p in ['afinal','cancela','cancelo','remove','apaga','nao tenho','não tenho']):
        futuras = DespesaFutura.query.filter(DespesaFutura.usuario_id==usuario.id, DespesaFutura.pago==False).all()
        palavras = [w for w in re.findall(r'[a-zà-ú]+', t)
                    if w not in {'afinal','nao','não','tenho','cancela','cancelo','remove','apaga','que','vem','mes','mês','o','a'}]
        if palavras:
            chave = palavras[0]
            removidas = [f for f in futuras if chave in f.descricao.lower()]
            if removidas:
                for f in removidas: db.session.delete(f)
                db.session.commit()
                enviar_mensagem(phone_raw, f"🗑️ '{removidas[0].descricao}' removido! 👍")
            else:
                lista = '\n'.join(f"• {d.descricao} — {d.valor_total:.0f}€" for d in futuras) if futuras else "Nenhuma"
                enviar_mensagem(phone_raw, f"Nao encontrei '{chave}'. Tens:\n{lista}")
        else:
            if futuras:
                lista = '\n'.join(f"• {d.descricao} — {d.valor_total:.0f}€" for d in futuras)
                enviar_mensagem(phone_raw, f"Qual cancelas?\n{lista}")
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
                    and w.lower() not in {'mes','mês','que','vem','tenho','este','esse','proximo','próximo','afinal'}]
        desc = ' '.join(palavras[:2]).capitalize() if palavras else 'Despesa futura'

    meses = 2 if ('2 meses' in t or 'dois meses' in t) else (3 if '3 meses' in t else 1)
    dia_match = re.search(r'dia (\d{1,2})', t)
    dia = int(dia_match.group(1)) if dia_match else None

    hoje = agora().replace(tzinfo=None)
    if dia:
        ma = hoje.month+meses; ya = hoje.year
        while ma>12: ma-=12; ya+=1
        try: data_prev = hoje.replace(year=ya, month=ma, day=min(dia,28))
        except: data_prev = hoje+timedelta(days=30*meses)
    else:
        data_prev = hoje+timedelta(days=30*meses)

    reserva = round(valor/meses, 2)
    db.session.add(DespesaFutura(usuario_id=usuario.id, descricao=desc, valor_total=valor,
        valor_reserva_mensal=reserva, meses=meses, data_prevista=data_prev))
    db.session.commit()

    dia_txt = f" (dia {dia})" if dia else ""
    when = f"este mes{dia_txt}" if ('este mes' in t or 'este mês' in t) else f"daqui a {meses} mes{'es' if meses>1 else ''}{dia_txt}"
    enviar_mensagem(phone_raw, f"📅 Anotado! {desc}: {valor:.0f}€ {when}\nVai aparecer nos fixos do mes 👍\n\nPara cancelar: 'afinal nao tenho {desc.lower()}'")

# ─── SIMULAR COMPRA ──────────────────────────────────────────
def simular_compra(phone_raw, usuario, texto):
    valor = extrair_valor(texto)
    disp, _ = calcular_disponivel(usuario)
    if valor == 0:
        enviar_mensagem(phone_raw, f"💚 Tens {disp:.2f}€ para gastar este mes."); return
    if disp <= 0:
        enviar_mensagem(phone_raw, f"🔴 Nem penses! Ja nao tens orcamento 😅"); return
    pct = valor/disp*100
    if pct <= 30:    resp = f"✅ Vai nessa! {valor:.0f}€ e so {pct:.0f}% do disponivel. Ficas com {disp-valor:.0f}€ 🛍️"
    elif pct <= 60:  resp = f"🟡 Da, mas pesa. Ficas com {disp-valor:.0f}€. Precisas mesmo?"
    elif pct <= 100: resp = f"🟠 Tecnicamente sim mas ficas quase a zero ({disp-valor:.0f}€)."
    else:            resp = f"🔴 Nao da. Faltam {valor-disp:.0f}€. Deixa p/ o mes que vem 😬"
    enviar_mensagem(phone_raw, resp)

# ─── RESUMO POR PESSOA ───────────────────────────────────────
def resumo_por_pessoa(phone_raw, usuario, texto):
    t = texto.lower()
    stop = {'quanto','gastei','com','fui','o','a','os','as','de','que','em','e'}
    nomes = [p.capitalize() for p in re.findall(r'[a-zà-ú]+', t) if p not in stop and len(p)>2]
    if not nomes:
        enviar_mensagem(phone_raw, "Com quem? Ex: quanto gastei com a Ana"); return
    nome = nomes[0]
    try:
        rows = db.session.execute(text("""
            SELECT d.valor, d.descricao FROM despesas d
            JOIN pessoas_gastos pg ON d.id=pg.despesa_id
            WHERE pg.usuario_id=:uid AND LOWER(pg.pessoa)=:p ORDER BY d.data DESC LIMIT 20
        """), {'uid':usuario.id,'p':nome.lower()}).fetchall()
        if not rows:
            enviar_mensagem(phone_raw, f"Nao encontrei gastos com {nome} 🤔"); return
        total = sum(r[0] for r in rows)
        msg = f"💸 Gastos com {nome}:\n"
        for r in rows[:5]: msg += f"• {r[0]:.0f}€ — {r[1][:30]}\n"
        msg += f"\n💰 Total: {total:.2f}€"
        enviar_mensagem(phone_raw, msg)
    except Exception as e:
        log.error(f"pessoa: {e}"); enviar_mensagem(phone_raw, "Erro 😕")

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
            for p in (r.json().get('resultado') or []):
                try:
                    preco = float(str(p.get('Preco','')).replace(' €/litro','').replace('€','').replace(',','.').strip())
                    if preco>0: todos.append({'nome':p.get('Nome','?'),'marca':p.get('Marca',''),'preco':preco})
                except: pass
        except Exception as e: log.error(f'DGEG: {e}')
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
        maps_q = p['nome'].replace(' ','+')
        msg += f"{medalhas[i]} {p['preco']:.3f}€/L — {p['nome'][:28]}{marca}\n📍 https://maps.google.com/?q={maps_q}\n\n"
    msg += "💡 Dados DGEG, hoje!"
    enviar_mensagem(phone_raw, msg)

# ─── WISHLIST ────────────────────────────────────────────────
WISHLIST_CATS = {
    'roupa':      ('👗', ['vestido','casaco','camisola','blusa','calcas','saia','top','hoodie','blazer']),
    'calcado':    ('👟', ['sapatilhas','sapatos','botas','botins','sandalias','tenis','sneakers']),
    'acessorios': ('👜', ['mala','carteira','cinto','oculos','bone','colar','brincos','pulseira']),
    'maquilagem': ('💄', ['batom','base','blush','sombra','mascara','perfume','creme','serum','hidratante']),
    'casa':       ('🏠', ['vela','quadro','almofada','planta','decoracao','ikea']),
    'tecnologia': ('📱', ['iphone','earbuds','auscultadores','capa','tablet','smartwatch']),
    'outros':     ('🛒', []),
}
ESTACOES = {
    'verao':   ['verao','verão','praia','bikini','shorts','leve','manga curta'],
    'inverno': ['inverno','casaco','blusao','la','lã','quente','grossa','botas'],
    'primavera':['primavera','floral','flores','colorido'],
    'outono':  ['outono','outonal','castanho','oversize'],
}

def detetar_cat_wishlist(texto):
    t = texto.lower()
    for cat, (emoji, palavras) in WISHLIST_CATS.items():
        if any(p in t for p in palavras): return cat, emoji
    return 'outros', '🛒'

def detetar_estacao(texto):
    t = texto.lower()
    for est, palavras in ESTACOES.items():
        if any(p in t for p in palavras): return est
    return None

def comparar_precos_tavily(desc, marca=None):
    try:
        import requests as req, urllib.parse
        query = f"{marca + ' ' if marca else ''}{desc} comprar Portugal preco"
        r = req.post("https://api.tavily.com/search",
            json={'api_key':TAVILY_API_KEY,'query':query,'search_depth':'advanced','max_results':5},
            timeout=20)
        if r.status_code != 200: return []
        lojas = []
        for res in r.json().get('results',[]):
            url = res.get('url',''); conteudo = res.get('content','')
            dominio = urllib.parse.urlparse(url).netloc.replace('www.','').split('.')[0].capitalize()
            pm = re.search(r'(\d{1,3}[.,]\d{2})\s*€|€\s*(\d{1,3}[.,]\d{2})', conteudo)
            if pm:
                try:
                    preco = float((pm.group(1) or pm.group(2)).replace(',','.'))
                    if 0 < preco < 10000:
                        lojas.append({'loja':dominio,'preco':preco,'url':url})
                except: pass
        return sorted(lojas, key=lambda x:x['preco'])[:4]
    except Exception as e:
        log.error(f"Tavily: {e}"); return []

def processar_wishlist(phone_raw, usuario, texto):
    t = texto.lower()

    # Ver lista
    if any(p in t for p in ['ver','lista','mostrar','wishlist','desejos']) and \
       not any(p in t for p in ['quero','gostei','adorei','link']):
        filtro_cat = next((c for c in WISHLIST_CATS if c in t), None)
        filtro_est = next((e for e in ESTACOES if e in t), None)
        try:
            q = "SELECT id, descricao, preco, link, marca, categoria, estacao FROM wishlist WHERE usuario_id=:id AND comprado=FALSE"
            params = {'id':usuario.id}
            if filtro_cat: q += " AND categoria=:cat"; params['cat']=filtro_cat
            if filtro_est: q += " AND estacao=:est"; params['est']=filtro_est
            q += " ORDER BY criado_em DESC"
            rows = db.session.execute(text(q), params).fetchall()
            if not rows:
                enviar_mensagem(phone_raw, "Wishlist vazia 🛍️\nManda foto de etiqueta, link ou 'quero [produto]'!"); return
            total = sum(r[2] for r in rows if r[2])
            msg = f"🛍️ Wishlist ({len(rows)} items"
            if total: msg += f" | {total:.0f}€"
            msg += "):\n\n"
            for i, r in enumerate(rows[:8], 1):
                cat_e = WISHLIST_CATS.get(r[5],('🛒',))[0] if r[5] else '🛒'
                preco_txt = f" — {r[2]:.2f}€" if r[2] else ""
                marca_txt = f" ({r[4]})" if r[4] else ""
                est_txt   = f" [{r[6]}]" if r[6] else ""
                link_txt  = f"\n   🔗 {r[3]}" if r[3] else ""
                msg += f"{i}. {cat_e} {r[1]}{marca_txt}{preco_txt}{est_txt}{link_txt}\n"
            msg += "\n'comprei o [nome]' | 'wishlist roupa' para filtrar"
            enviar_mensagem(phone_raw, msg)
        except Exception as e:
            log.error(f"wishlist ver: {e}"); enviar_mensagem(phone_raw, "Erro 😕")
        return

    # Link direto
    link_match = re.search(r'https?://\S+', texto)
    if link_match:
        link = link_match.group(0)
        enviar_mensagem(phone_raw, "🔍 A analisar e a comparar precos...")
        lojas = comparar_precos_tavily(texto.replace(link,'').strip() or 'produto')
        cat, cat_e = detetar_cat_wishlist(texto)
        est = detetar_estacao(texto)
        preco_f = lojas[0]['preco'] if lojas else None
        try:
            db.session.execute(text("INSERT INTO wishlist (usuario_id,descricao,preco,link,categoria,estacao) VALUES (:u,:d,:p,:l,:c,:e)"),
                {'u':usuario.id,'d':'Produto (link)','p':preco_f,'l':link,'c':cat,'e':est})
            db.session.commit()
        except Exception: db.session.rollback()
        msg = f"🛍️ {cat_e} Guardado!\n"
        if lojas:
            msg += "\n💰 Precos:\n"
            for i, l in enumerate(lojas[:3]):
                msg += f"{'🥇🥈🥉'[i]} {l['loja']}: {l['preco']:.2f}€\n"
            if len(lojas)>1: msg += f"\n✅ Mais barato: {lojas[0]['loja']} ({lojas[0]['preco']:.2f}€)!"
        enviar_mensagem(phone_raw, msg); return

    # Adicionar por texto
    valor = extrair_valor(texto)
    stop = {'quero','isto','gostei','disto','comprar','uma','um','adorei','vi','e','de','da','do'}
    palavras = [w for w in texto.split() if not re.match(r'[0-9€,.]',w) and len(w)>2 and w.lower() not in stop]
    desc = ' '.join(palavras[:4]).capitalize() if palavras else 'Item'
    marca = next((m.capitalize() for m in ['zara','nike','adidas','hm','bershka','shein','mango','pull','stradivarius','primark','jd','snipes'] if m in t), None)
    cat, cat_e = detetar_cat_wishlist(texto)
    est = detetar_estacao(texto)
    link_f = None; preco_f = valor if valor > 0 else None
    lojas = []
    if TAVILY_API_KEY and len(desc) > 3:
        enviar_mensagem(phone_raw, f"🔍 A procurar '{desc}'...")
        lojas = comparar_precos_tavily(desc, marca)
        if lojas:
            link_f = lojas[0]['url']
            if not preco_f: preco_f = lojas[0]['preco']
    try:
        db.session.execute(text("INSERT INTO wishlist (usuario_id,descricao,preco,link,marca,categoria,estacao) VALUES (:u,:d,:p,:l,:m,:c,:e)"),
            {'u':usuario.id,'d':desc,'p':preco_f,'l':link_f,'m':marca,'c':cat,'e':est})
        db.session.commit()
    except Exception as e:
        log.error(f"wishlist add: {e}"); db.session.rollback()
    preco_txt = f" — {preco_f:.2f}€" if preco_f else ""
    msg = f"🛍️ {cat_e} Adicionado!\n{desc}{preco_txt}\n"
    if lojas:
        msg += "\n💰 Precos:\n"
        for i, l in enumerate(lojas[:3]): msg += f"{'🥇🥈🥉'[i]} {l['loja']}: {l['preco']:.2f}€\n"
        if len(lojas)>1: msg += f"\n✅ Mais barato: {lojas[0]['loja']} ({lojas[0]['preco']:.2f}€)"
    msg += "\n\nDiz 'wishlist' para ver tudo 😊"
    enviar_mensagem(phone_raw, msg)

def ler_etiqueta_wishlist(phone_raw, usuario, url, mimetype):
    try:
        from groq import Groq
        c = baixar_media(url)
        if not c: return False
        mt = 'image/png' if 'png' in mimetype else 'image/jpeg'
        img = base64.b64encode(c).decode()
        resp = Groq(api_key=GROQ_API_KEY).chat.completions.create(
            model='meta-llama/llama-4-scout-17b-16e-instruct', max_tokens=150,
            messages=[{'role':'user','content':[
                {'type':'image_url','image_url':{'url':f'data:{mt};base64,{img}'}},
                {'type':'text','text':'Le esta etiqueta de produto/roupa. Responde APENAS em JSON sem markdown: {"marca":"MARCA","produto":"NOME","preco":NUMERO_OU_NULL,"referencia":"REF_OU_NULL","tipo":"roupa/calcado/acessorio/maquilagem/outro"}. Se nao for etiqueta: {"erro":"nao_etiqueta"}'}
            ]}])
        txt = re.sub(r'```json|```','', resp.choices[0].message.content.strip()).strip()
        try: dados = json.loads(txt)
        except: return False
        if 'erro' in dados: return False

        desc  = dados.get('produto','Item')
        marca = dados.get('marca')
        preco = dados.get('preco')
        ref   = dados.get('referencia')
        tipo  = dados.get('tipo','outro')
        if ref and ref not in ['null','None',None]: desc = f"{desc} ({ref})"
        cat, cat_e = detetar_cat_wishlist(desc+' '+tipo)
        est = detetar_estacao(desc)

        lojas = []; link_f = None; preco_online = None
        if TAVILY_API_KEY and marca:
            enviar_mensagem(phone_raw, f"🔍 A procurar '{desc}' online...")
            lojas = comparar_precos_tavily(dados.get('produto',''), marca)
            if lojas: link_f = lojas[0]['url']; preco_online = lojas[0]['preco']

        preco_f = preco or preco_online
        try:
            db.session.execute(text("INSERT INTO wishlist (usuario_id,descricao,preco,link,marca,categoria,estacao) VALUES (:u,:d,:p,:l,:m,:c,:e)"),
                {'u':usuario.id,'d':desc,'p':preco_f,'l':link_f,'m':marca,'c':cat,'e':est})
            db.session.commit()
        except Exception as e:
            log.error(f"wishlist etiqueta bd: {e}"); db.session.rollback()

        preco_txt = f" — {preco_f:.2f}€" if preco_f else ""
        marca_txt = f" ({marca})" if marca else ""
        msg = f"🛍️ {cat_e} Guardado!\n{desc}{marca_txt}{preco_txt}\n"
        if lojas:
            msg += "\n💰 Precos:\n"
            for i, l in enumerate(lojas[:3]): msg += f"{'🥇🥈🥉'[i]} {l['loja']}: {l['preco']:.2f}€\n"
            if len(lojas)>1: msg += f"\n✅ Mais barato: {lojas[0]['loja']} ({lojas[0]['preco']:.2f}€)!"
        else: msg += "Nao encontrei precos online 😕"
        msg += "\n\nDiz 'wishlist' para ver tudo 😊"
        enviar_mensagem(phone_raw, msg); return True
    except Exception as e:
        log.error(f"etiqueta: {e}", exc_info=True); return False

def marcar_wishlist_comprado(phone_raw, usuario, texto):
    t = texto.lower()
    palavras = [w for w in re.findall(r'[a-zà-ú]+', t) if w not in {'comprei','ja','já','o','a','os','as'}]
    if not palavras:
        enviar_mensagem(phone_raw, "O que compraste? Ex: 'comprei o vestido'"); return
    chave = palavras[0]
    try:
        r = db.session.execute(text(
            "UPDATE wishlist SET comprado=TRUE WHERE usuario_id=:u AND LOWER(descricao) LIKE :c AND comprado=FALSE RETURNING descricao,preco"),
            {'u':usuario.id,'c':f'%{chave}%'}).fetchone()
        db.session.commit()
        if r: enviar_mensagem(phone_raw, f"✅ '{r[0]}' comprado! 🎉\nVai registar o gasto? Diz quanto pagaste!")
        else: enviar_mensagem(phone_raw, f"Nao encontrei '{chave}' 🤔 Diz 'wishlist' para ver.")
    except Exception as e:
        log.error(f"wishlist comprado: {e}"); enviar_mensagem(phone_raw, "Erro 😕")

def remover_wishlist(phone_raw, usuario, texto):
    t = texto.lower()
    palavras = [w for w in re.findall(r'[a-zà-ú]+', t) if w not in {'remove','apaga','da','wishlist','o','a','tira'}]
    if not palavras:
        enviar_mensagem(phone_raw, "O que queres remover? Ex: 'remove da wishlist o vestido'"); return
    chave = palavras[0]
    try:
        r = db.session.execute(text(
            "DELETE FROM wishlist WHERE usuario_id=:u AND LOWER(descricao) LIKE :c RETURNING descricao"),
            {'u':usuario.id,'c':f'%{chave}%'}).fetchone()
        db.session.commit()
        if r: enviar_mensagem(phone_raw, f"🗑️ '{r[0]}' removido!")
        else: enviar_mensagem(phone_raw, f"Nao encontrei '{chave}' 🤔")
    except Exception as e:
        log.error(f"wishlist remove: {e}"); enviar_mensagem(phone_raw, "Erro 😕")

# ─── FOTO KM ─────────────────────────────────────────────────
def ler_foto_km(phone_raw, usuario, url, mimetype):
    try:
        from groq import Groq
        c = baixar_media(url)
        if not c: return False
        mt = 'image/png' if 'png' in mimetype else 'image/jpeg'
        img = base64.b64encode(c).decode()
        resp = Groq(api_key=GROQ_API_KEY).chat.completions.create(
            model='meta-llama/llama-4-scout-17b-16e-instruct', max_tokens=50,
            messages=[{'role':'user','content':[
                {'type':'image_url','image_url':{'url':f'data:{mt};base64,{img}'}},
                {'type':'text','text':'Le o odometro/conta-km. Responde APENAS JSON: {"km":NUMERO}. Se nao for odometro: {"erro":"nao"}'}
            ]}])
        txt = re.sub(r'```json|```','', resp.choices[0].message.content.strip()).strip()
        try: dados = json.loads(txt)
        except: return False
        if 'erro' in dados: return False
        km = dados.get('km')
        if not km: return False

        ultimo = db.session.execute(text(
            "SELECT km, valor FROM km_combustivel WHERE usuario_id=:u ORDER BY data DESC LIMIT 1"),
            {'u':usuario.id}).fetchone()
        ultimo_gas = db.session.query(Despesa).filter(
            Despesa.usuario_id==usuario.id, Despesa.categoria=='combustivel'
        ).order_by(Despesa.id.desc()).first()
        valor_gas = ultimo_gas.valor if ultimo_gas else 0
        litros_est = round(valor_gas/1.9, 1) if valor_gas > 0 else None

        consumo = custo_km = None; msg_consumo = ''
        if ultimo and km > ultimo[0]:
            km_perc = km - ultimo[0]
            if litros_est and km_perc > 0:
                consumo = round(litros_est/km_perc*100, 1)
                custo_km = round(valor_gas/km_perc*100, 2) if valor_gas > 0 else None
                msg_consumo = f"\n\n📊 Desde o ultimo abastecimento:\n🛣️ {km_perc} km\n⛽ {consumo}L/100km\n💶 {custo_km:.2f}€/100km" if custo_km else f"\n\n🛣️ {km_perc} km percorridos"

        db.session.execute(text(
            "INSERT INTO km_combustivel (usuario_id,km,litros,valor,consumo_l100,custo_km) VALUES (:u,:k,:l,:v,:c,:ck)"),
            {'u':usuario.id,'k':km,'l':litros_est,'v':valor_gas,'c':consumo,'ck':custo_km})
        db.session.commit()
        enviar_mensagem(phone_raw, f"🚗 {km:,} km registado!{msg_consumo}\n\nProximo abastecimento manda foto do odometro de novo 📸")
        return True
    except Exception as e:
        log.error(f"foto km: {e}", exc_info=True); return False

# ─── SPLITTING ───────────────────────────────────────────────
def processar_splitting(phone_raw, usuario, texto):
    valor = extrair_valor(texto)
    if valor == 0:
        enviar_mensagem(phone_raw, "Quanto foi o total? Ex: dividi 60€ jantar com o Ruben"); return
    t = texto.lower()
    m_p = re.search(r'com (?:o |a |os |as )?([A-Za-zÀ-ú]+)', texto, re.IGNORECASE)
    pessoa = m_p.group(1).capitalize() if m_p else 'Alguem'
    stop = {'dividi','dividir','meias','split','partilhei','com','o','a'}
    palavras = [w for w in texto.split() if not re.match(r'[0-9€,.]',w) and len(w)>2 and w.lower() not in stop]
    desc = ' '.join(palavras[:3]).capitalize() if palavras else 'Gasto partilhado'
    valor_cada = round(valor/2, 2)
    try:
        db.session.execute(text("INSERT INTO splitting (usuario_id,descricao,valor_total,valor_cada,pessoa) VALUES (:u,:d,:vt,:vc,:p)"),
            {'u':usuario.id,'d':desc,'vt':valor,'vc':valor_cada,'p':pessoa})
        db.session.commit()
        enviar_mensagem(phone_raw, f"✂️ Split!\n{desc}: {valor:.2f}€\nA tua parte: {valor_cada:.2f}€\n{pessoa} fica-te a dever: {valor_cada:.2f}€")
    except Exception as e:
        log.error(f"splitting: {e}"); enviar_mensagem(phone_raw, "Erro 😕")

def ver_splits(phone_raw, usuario):
    try:
        rows = db.session.execute(text(
            "SELECT descricao, valor_cada, pessoa FROM splitting WHERE usuario_id=:u AND pago=FALSE ORDER BY criado_em DESC"),
            {'u':usuario.id}).fetchall()
        if not rows:
            enviar_mensagem(phone_raw, "Nao tens splits pendentes 😊"); return
        total = sum(r[1] for r in rows)
        msg = "✂️ Splits pendentes:\n\n"
        for r in rows: msg += f"• {r[0]} — {r[1]:.2f}€ ({r[2]})\n"
        msg += f"\n💰 Total a receber: {total:.2f}€"
        enviar_mensagem(phone_raw, msg)
    except Exception as e:
        log.error(f"splits: {e}"); enviar_mensagem(phone_raw, "Erro 😕")

# ─── OBJETIVO POUPANÇA ───────────────────────────────────────
def processar_objetivo_poupanca(phone_raw, usuario, texto):
    t = texto.lower()
    if any(p in t for p in ['ver','lista','objetivos','metas','mostrar']):
        try:
            rows = db.session.execute(text(
                "SELECT descricao, valor_objetivo, valor_atual FROM objetivos_poupanca WHERE usuario_id=:id AND concluido=FALSE"),
                {'id':usuario.id}).fetchall()
            if not rows:
                enviar_mensagem(phone_raw, "Nao tens objetivos ainda 🎯\nCria: 'quero poupar 500€ para ferias'"); return
            msg = "🎯 Objetivos:\n\n"
            for r in rows:
                pct = int(r[2]/r[1]*100) if r[1]>0 else 0
                barra = '█'*(pct//10) + '░'*(10-pct//10)
                msg += f"📌 {r[0]}\n{barra} {pct}%\n{r[2]:.0f}€ de {r[1]:.0f}€\n\n"
            enviar_mensagem(phone_raw, msg)
        except Exception as e:
            log.error(f"objetivos: {e}"); enviar_mensagem(phone_raw, "Erro 😕")
        return

    valor = extrair_valor(texto)
    if valor == 0:
        enviar_mensagem(phone_raw, "Quanto queres poupar? Ex: 'quero poupar 500€ para ferias'"); return
    stop = {'quero','poupar','para','objetivo','meta','de','poupanca','poupança'}
    palavras = [w for w in texto.split() if not re.match(r'[0-9€,.]',w) and len(w)>2 and w.lower() not in stop]
    desc = ' '.join(palavras[:3]).capitalize() if palavras else 'Objetivo'
    try:
        db.session.execute(text(
            "INSERT INTO objetivos_poupanca (usuario_id,descricao,valor_objetivo,valor_atual) VALUES (:u,:d,:v,0)"),
            {'u':usuario.id,'d':desc,'v':valor})
        db.session.commit()
        modo = get_modo(usuario.id)
        p = calcular_plano(usuario.salario_liquido or 0, modo)
        meses_est = round(valor/p['poupanca']) if p['poupanca']>0 else '?'
        enviar_mensagem(phone_raw,
            f"🎯 Objetivo criado!\n📌 {desc}: {valor:.0f}€\n"
            f"⏱️ Ao ritmo atual (~{p['poupanca']:.0f}€/mes) chegas la em ~{meses_est} meses 💪\n"
            f"Diz 'objetivos' para ver o progresso")
    except Exception as e:
        log.error(f"criar objetivo: {e}"); enviar_mensagem(phone_raw, "Erro 😕")

# ─── MODO DISCRETO ───────────────────────────────────────────
def modo_discreto(phone_raw):
    try:
        import requests as req
        r = req.get(f"{WAHA_URL}/api/default/messages",
                    headers={'X-Api-Key': WAHA_API_KEY},
                    params={'chatId': phone_raw, 'limit': 30}, timeout=15)
        if r.status_code != 200:
            enviar_mensagem(phone_raw, "Nao consigo apagar agora 😕\nO WAHA pode nao suportar esta funcao."); return
        msgs = r.json(); apagadas = 0
        for msg in msgs:
            msg_id = msg.get('id','')
            if msg_id:
                req.delete(f"{WAHA_URL}/api/default/messages/{msg_id}",
                           headers={'X-Api-Key': WAHA_API_KEY}, timeout=10)
                apagadas += 1
        enviar_mensagem(phone_raw, f"🔒 Apaguei {apagadas} mensagens!")
    except Exception as e:
        log.error(f"discreto: {e}"); enviar_mensagem(phone_raw, "Nao consegui apagar 😕")

# ─── BOAS VINDAS / AJUDA / MODOS ─────────────────────────────
def mostrar_modos(phone_raw):
    msg = "Escolhe o modo de poupanca:\n\n"
    for i, (k, m) in enumerate(MODOS_POUPANCA.items(), 1):
        msg += f"{i}. {m['emoji']} {m['nome']}\n{m['desc']}\n\n"
    msg += "Responde com o nome: 'modo maximo', 'modo equilibrado' ou 'modo relaxado' 😊"
    enviar_mensagem(phone_raw, msg)

def enviar_boas_vindas(phone_raw, usuario=None, phone=None):
    dias = dias_para_salario()
    tem_salario = usuario and usuario.salario_liquido
    if not tem_salario:
        if phone: set_estado(phone, 'escolher_modo', {})
        msg = (f"Ola Luana! 👋 Eu sou o Ze das Financas!\n"
               f"Fui criado pelo tuga27 especialmente para ti 💸\n\n"
               f"Antes de comecarmos, escolhe como queres poupar:\n\n"
               f"💎 Modo Maximo — poupes tudo, gastas so o essencial\n"
               f"⚖️ Modo Equilibrado — poupas bem mas ainda vives 😊\n"
               f"😎 Modo Relaxado — vives a vida mas ainda poupas\n\n"
               f"Responde com: 'modo maximo', 'modo equilibrado' ou 'modo relaxado'\n\n"
               f"(Podes mudar a qualquer momento! E daqui a {dias} dia{'s' if dias!=1 else ''} vamos juntos com o salario 🚀)")
    else:
        disp, p = calcular_disponivel(usuario)
        m = MODOS_POUPANCA[get_modo(usuario.id)]
        msg = (f"Ola de volta! 👋 {m['emoji']}\n"
               f"Tens {disp:.0f}€ para gastar | Poupanca: {p['poupanca']:.0f}€\n"
               f"🛡️ Reserva: {get_reserva(usuario.id):.2f}€\n\n"
               f"Sugestoes de melhorias? Manda! 🚀\n"
               f"Diz 'ajuda' se precisares 😎")
    enviar_mensagem(phone_raw, msg)

def enviar_ajuda(phone_raw):
    enviar_mensagem(phone_raw, """😎 Ze das Financas:

💸 Gastos:
• 15 bk | 25 conti | 50 galp | jantar 30
• jantar 30 na conjunta
• gastei 30 da reserva
• foto talao | audio | PDF recibo
• [foto odometro] → km e consumo

🛍️ Wishlist:
• [foto etiqueta] → guarda + compara
• quero sapatilhas nike 89€
• [link produto] → compara precos
• wishlist | wishlist roupa | wishlist verao
• comprei o vestido | remove da wishlist X

🎯 Objetivos:
• quero poupar 500€ para ferias
• objetivos → progresso

✂️ Splitting:
• dividi 60€ jantar com o Ruben
• splits → pendentes

📊 Consultas:
• resumo | plano | quanto tenho
• quanto tenho na conjunta | score
• resumo anterior

🎂 Aniversarios:
• aniversario da Ana 15/3
• aniversarios → lista

⛽ Gasolina mais barata no barreiro
🆘 Estou teso
🔒 Limpa conversa
🔄 Muda modo (maximo/equilibrado/relaxado)
🧠 Aprende que X e roupa | corrige para roupa

💡 Sugestoes? Manda! 🚀""")

# ─── IA ──────────────────────────────────────────────────────
def filtrar_resposta(txt):
    subs = [
        (r'\bbrother\b','querida'),(r'\birmao\b','querida'),(r'\birmão\b','querida'),
        (r'\bmano\b','linda'),(r'\bchefe\b','querida'),(r'\bbro\b','querida'),
        (r'\bamigo\b','querida'),(r'\brapaz\b','rapariga'),(r'\bcara\b','querida'),
        (r'\bparceiro\b','parceira'),
    ]
    for p, s in subs: txt = re.sub(p, s, txt, flags=re.IGNORECASE)
    return txt

def perguntar_ia(texto, usuario):
    try:
        from groq import Groq
        disp, _ = calcular_disponivel(usuario)
        modo = get_modo(usuario.id); m = MODOS_POUPANCA[modo]
        sys = f"""Es o Ze das Financas, assistente financeiro portugues criado pelo tuga27 para a Luana.
REGRAS ABSOLUTAS:
1. Fala SEMPRE no feminino: "gastaste","tens","podes","estás","foste"
2. PROIBIDO: brother, irmao, mano, chefe, bro, cara, rapaz, amigo, parceiro
3. Usa: querida, linda, bora, fixe, top, ena, boa
4. Portugues europeu informal. Max 2 linhas + 1 emoji
5. NUNCA inventes precos de gasolina — diz "usa 'gasolina mais barata no barreiro'"
6. Se nao souberes: "Nao sei querida 🤔 Diz 'ajuda' para veres o que sei!"

CONTEXTO: Modo {m['nome']} | {disp:.0f}€ disponivel | Salario: {usuario.salario_liquido or 'nao registado'}€
SABER: BK=Burger King, Mac=McDonald's, conti=Continente, PD=Pingo Doce, JD=JD Sports, galp/bp=gasolina"""
        resp = Groq(api_key=GROQ_API_KEY).chat.completions.create(
            model='llama-3.3-70b-versatile',
            messages=[{'role':'system','content':sys},{'role':'user','content':texto}],
            max_tokens=150)
        return filtrar_resposta(resp.choices[0].message.content)
    except Exception as e:
        log.error(f'IA: {e}'); return "Nao percebi 🤔 Diz 'ajuda'!"

# ─── SCHEDULER ───────────────────────────────────────────────
def lembrete_recibo():
    with app.app_context():
        hoje = agora()
        if hoje.day == dia_recibo_mes(hoje.year, hoje.month).day and hoje.hour == 11:
            for u in Usuario.query.all():
                if u.phone:
                    set_estado(u.phone, 'aguardar_recibo', {})
                    enviar_mensagem(f"{u.phone}@lid", "Ola! 📄 Hoje deve ter chegado o teu recibo!\nQueres mandar o PDF/foto ou preferes dizer o valor?")

def lembrete_salario():
    with app.app_context():
        hoje = agora()
        if hoje.day == dia_pagamento_mes(hoje.year, hoje.month).day and hoje.hour == 9:
            for u in Usuario.query.all():
                if u.phone: enviar_mensagem(f"{u.phone}@lid", "💰 Hoje e dia de salario! Manda o recibo ou diz o valor 🚀")

def fecho_mes():
    with app.app_context():
        hoje = agora()
        if hoje.day == dia_pagamento_mes(hoje.year, hoje.month).day and hoje.hour == 10:
            mes_ant = hoje.month-1 if hoje.month>1 else 12
            nomes = ['Janeiro','Fevereiro','Marco','Abril','Maio','Junho','Julho','Agosto','Setembro','Outubro','Novembro','Dezembro']
            for u in Usuario.query.all():
                if not u.phone: continue
                estado, dados = get_estado(u.phone)
                if estado=='fecho_feito' and dados.get('mes')==hoje.month and dados.get('ano')==hoje.year: continue
                enviar_mensagem(f"{u.phone}@lid", f"📅 Novo mes! Diz 'resumo anterior' p/ veres {nomes[mes_ant-1]} 📊")

def aviso_meio_mes():
    with app.app_context():
        hoje = agora()
        if hoje.day == 15 and hoje.hour == 10:
            for u in Usuario.query.all():
                if u.phone and u.salario_liquido:
                    disp, p = calcular_disponivel(u)
                    gastar = p['gastar']
                    pct = (gastar-disp)/gastar*100 if gastar>0 else 0
                    if pct > 70:
                        enviar_mensagem(f"{u.phone}@lid", f"⚠️ A meio do mes e ja usaste {pct:.0f}% do orcamento! Vai com calma 💪")

def aviso_fim_mes_wishlist():
    with app.app_context():
        hoje = agora()
        dia_pag = dia_pagamento_mes(hoje.year, hoje.month)
        dias_para_fim = (dia_pag.date() - hoje.date()).days
        if dias_para_fim not in [2,3,4]: return
        for u in Usuario.query.all():
            if not u.phone or not u.salario_liquido: continue
            try:
                disp, _ = calcular_disponivel(u)
                if disp < 20: continue
                rows = db.session.execute(text(
                    "SELECT descricao, preco, link FROM wishlist WHERE usuario_id=:id AND comprado=FALSE AND preco IS NOT NULL AND preco <= :disp ORDER BY preco DESC LIMIT 3"),
                    {'id':u.id,'disp':disp}).fetchall()
                if not rows:
                    enviar_mensagem(f"{u.phone}@lid", f"💡 Faltam {dias_para_fim} dias e ainda tens {disp:.0f}€!\nSe nao gastares vai para a reserva automaticamente 💪")
                    continue
                msg = f"🛍️ Tens {disp:.0f}€ e o ciclo acaba em {dias_para_fim} dias!\n\nDa wishlist podes comprar:\n"
                for r in rows:
                    link_txt = f"\n   🔗 {r[2]}" if r[2] else ""
                    msg += f"• {r[0]} — {r[1]:.2f}€{link_txt}\n"
                msg += "\nSe nao gastares vai para a reserva 💪"
                enviar_mensagem(f"{u.phone}@lid", msg)
            except Exception as e: log.error(f"wishlist aviso: {e}")

def verificar_aniversarios():
    with app.app_context():
        hoje = agora()
        try:
            rows = db.session.execute(text("""
                SELECT u.phone, a.nome, a.data_aniv
                FROM aniversarios a
                JOIN usuarios u ON a.usuario_id=u.id
                WHERE EXTRACT(month FROM a.data_aniv)=:m
                AND EXTRACT(day FROM a.data_aniv) IN (:d0, :d1, :d5)
            """), {'m':hoje.month,'d0':hoje.day,'d1':hoje.day+1,'d5':hoje.day+5}).fetchall()
            for r in rows:
                phone, nome, data = r
                try:
                    dias = (data.replace(year=hoje.year) - hoje.date()).days
                except: dias = -1
                if dias == 5:
                    enviar_mensagem(f"{phone}@lid", f"🎂 Daqui a 5 dias e o aniversario de {nome}! Ja pensaste no presente? 🎁")
                elif dias == 1:
                    enviar_mensagem(f"{phone}@lid", f"🎂 AMANHA e o aniversario de {nome}! Nao te esqueças! 🎉")
                elif dias == 0:
                    enviar_mensagem(f"{phone}@lid", f"🎂🎉 HOJE e o aniversario de {nome}! Ja desejaste? 💕")
        except Exception as e: log.error(f"anivs scheduler: {e}")

def resumo_semanal():
    with app.app_context():
        if agora().weekday()==0 and agora().hour==9:
            for u in Usuario.query.all():
                if u.phone: enviar_resumo(f"{u.phone}@lid", u)

def verificar_despesas_futuras():
    with app.app_context():
        amanha = agora().replace(tzinfo=None)+timedelta(days=1)
        for d in DespesaFutura.query.filter(DespesaFutura.pago==False).all():
            if d.data_prevista and d.data_prevista.date() <= amanha.date():
                u = Usuario.query.get(d.usuario_id)
                if u and u.phone:
                    enviar_mensagem(f"{u.phone}@lid", f"⚠️ Lembrete: {d.descricao} — {d.valor_total:.0f}€ amanha!")

def wrapped_anual():
    with app.app_context():
        hoje = agora()
        if hoje.month==12 and hoje.day==31 and hoje.hour==20:
            for u in Usuario.query.all():
                if not u.phone or not u.salario_liquido: continue
                try:
                    tg = db.session.query(db.func.sum(Despesa.valor)).filter(
                        Despesa.usuario_id==u.id, db.extract('year',Despesa.data)==hoje.year).scalar() or 0
                    tr = db.session.query(db.func.sum(Receita.valor)).filter(
                        Receita.usuario_id==u.id, db.extract('year',Receita.data)==hoje.year).scalar() or 0
                    tc = db.session.query(Despesa.categoria, db.func.sum(Despesa.valor)).filter(
                        Despesa.usuario_id==u.id, db.extract('year',Despesa.data)==hoje.year
                    ).group_by(Despesa.categoria).order_by(db.func.sum(Despesa.valor).desc()).first()
                    reserva = get_reserva(u.id)
                    msg = (f"🎊 O teu {hoje.year} em numeros!\n\n"
                           f"💰 Recebeste: {tr:.0f}€\n🛒 Gastaste: {tg:.0f}€\n"
                           f"💎 Poupaste: {tr-tg:.0f}€\n🛡️ Reserva: {reserva:.2f}€\n")
                    if tc: msg += f"🏆 Maior categoria: {tc[0].capitalize()} ({tc[1]:.0f}€)\n"
                    msg += f"\nFeliz {hoje.year+1}! Vamos a mais um ano a bombar! 🚀🎉"
                    enviar_mensagem(f"{u.phone}@lid", msg)
                except Exception as e: log.error(f"wrapped: {e}")

# ─── ARRANQUE ────────────────────────────────────────────────
with app.app_context():
    try: db.create_all()
    except Exception as e: log.warning(f"db: {e}")
    criar_tabelas()

scheduler.add_job(lembrete_recibo,            'cron', hour=11, minute=0)
scheduler.add_job(lembrete_salario,           'cron', hour=9,  minute=0)
scheduler.add_job(fecho_mes,                  'cron', hour=10, minute=0)
scheduler.add_job(aviso_meio_mes,             'cron', hour=10, minute=0)
scheduler.add_job(aviso_fim_mes_wishlist,     'cron', hour=11, minute=0)
scheduler.add_job(resumo_semanal,             'cron', hour=9,  minute=30, day_of_week='mon')
scheduler.add_job(verificar_despesas_futuras, 'cron', hour=8,  minute=0)
scheduler.add_job(verificar_aniversarios,     'cron', hour=9,  minute=0)
scheduler.add_job(wrapped_anual,              'cron', hour=20, minute=0)
scheduler.start()
log.info("Ze das Financas v7 iniciado")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
