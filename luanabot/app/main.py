import os, json, logging, re, base64, tempfile
from datetime import datetime, timedelta
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Lisbon")
except Exception:
    TZ = None
from flask import Flask, request, jsonify
from flask import after_this_request
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import text
from models import db, Usuario, Despesa, Receita, DespesaFutura, ObjetivoFinanceiro, FundoEmergencia
from whatsapp import enviar_mensagem
from claude_ai import processar_mensagem_ia
from pdf_reader import extrair_salario_pdf

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

app = Flask(__name__)

@app.after_request
def add_cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, X-Token'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    return response

@app.route('/api/comando', methods=['POST'])
def api_comando():
    """Executa comando como se fosse mensagem WhatsApp do utilizador."""
    data = request.get_json() or {}
    phone = data.get('phone','')
    token = data.get('token','')
    cmd   = data.get('cmd','')
    if not phone or token != phone[:8]+'zef':
        return jsonify({'error':'unauthorized'}), 401
    if not cmd:
        return jsonify({'error':'empty command'}), 400
    try:
        usuario = Usuario.query.filter_by(phone=phone).first()
        if not usuario:
            return jsonify({'error':'user not found'}), 404
        phone_raw = f"{phone}@lid"
        processar_texto(phone, phone_raw, cmd)
        return jsonify({'ok': True, 'cmd': cmd})
    except Exception as e:
        log.error(f"api_comando: {e}")
        return jsonify({'error': str(e)}), 500
def dashboard_options():
    return '', 204
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///luana.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(app)

OWNER_PHONE  = os.environ.get('OWNER_PHONE', '')
PHONE_RUBEN  = os.environ.get('PHONE_RUBEN', '264909371768998')
PHONE_LUANA  = os.environ.get('PHONE_LUANA', '84516500680875')

# ── MODO CASAL ──────────────────────────────────────────────
CASAL = {
    PHONE_RUBEN: PHONE_LUANA,
    PHONE_LUANA: PHONE_RUBEN,
}
NOMES_CASAL = {
    PHONE_RUBEN: 'Ruben',
    PHONE_LUANA: 'Luana',
}

def get_parceiro_phone(phone):
    return CASAL.get(phone)

def get_parceiro_raw(phone):
    p = get_parceiro_phone(phone)
    return f"{p}@lid" if p else None

def notificar_parceiro(phone_origem, mensagem):
    """Envia notificação ao parceiro."""
    parceiro_raw = get_parceiro_raw(phone_origem)
    if parceiro_raw:
        enviar_mensagem(parceiro_raw, mensagem)

def get_perfil(phone):
    """Devolve o perfil do utilizador com base no número."""
    if phone == PHONE_RUBEN:
        return {
            'nome': 'Ruben', 'genero': 'M',
            'tratamento': 'mano', 'emoji_cumprimento': '🤙',
            'estilo': 'direto e brincalhão entre amigos',
            'expressoes': 'mano, bro, bora, top, fixe, boa, pa',
            'proibido': '',
        }
    return {
        'nome': 'Luana', 'genero': 'F',
        'tratamento': 'querida', 'emoji_cumprimento': '👋',
        'estilo': 'fofo, motivador e carinhoso',
        'expressoes': 'querida, linda, bora, fixe, top, boa',
        'proibido': 'brother, irmao, mano, chefe, bro, cara, rapaz',
    }
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

# ─── HORÁRIO RUBEN (horas extras) ───────────────────────────
HORARIO_RUBEN = {
    'dias_normais': [1, 2, 3, 4, 5],  # Ter Qua Qui Sex Sab
    'dias_folga':   [0, 6],            # Seg Dom
    'horas_dia':    7,
    'hora_entrada': 9,
}

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
    # Casa/Deco
    'ik':'casa','ikea':'casa','zara home':'casa','leroy':'casa','leroy merlin':'casa',
    'worten casa':'casa','aki':'casa',
    # Animais
    'veterinario':'animais','vet':'animais','zoomalia':'animais','racao':'animais',
    # Presentes
    'presente':'presentes','prenda':'presentes','fnac presente':'presentes',
    # Desporto
    'decathlon':'desporto','sport':'desporto','ginasio':'desporto','gym':'desporto',
    # Viagem
    'ryanair':'viagem','tap':'viagem','booking':'viagem','airbnb':'viagem',
    # Educação
    'livro':'educacao','curso':'educacao','udemy':'educacao',
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
    'casa':'🏠','animais':'🐶','presentes':'🎁','educacao':'📚',
    'desporto':'🏃','viagem':'✈️','outros':'💳',
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
        "CREATE TABLE IF NOT EXISTS conjunta_depositos (id SERIAL PRIMARY KEY, usuario_id INTEGER NOT NULL, valor FLOAT NOT NULL, descricao VARCHAR(200), data TIMESTAMP DEFAULT NOW())",
        "CREATE TABLE IF NOT EXISTS picos (id SERIAL PRIMARY KEY, user_phone VARCHAR(50) NOT NULL, data DATE NOT NULL, entrada TIMESTAMP, saida TIMESTAMP, horas_trabalhadas FLOAT DEFAULT 0, horas_extra FLOAT DEFAULT 0, dia_folga BOOLEAN DEFAULT FALSE, created_at TIMESTAMP DEFAULT NOW(), UNIQUE(user_phone, data))",
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
                    # Verifica se a loja foi identificada ou ficou genérica
                    loja_generica = re.search(r'\bLOJA\b', resultado.upper()) is not None
                    if e_salario and valor_lido > 200:
                        enviar_mensagem(phone_raw, f'📸 Vi no recibo: {valor_lido:.2f}€ — é esse o teu salário?')
                        set_estado(phone, 'confirmar_salario', {'valor': valor_lido})
                    elif loja_generica and valor_lido > 0:
                        # Não identificou a loja — pergunta
                        enviar_mensagem(phone_raw, f'📸 Vi {valor_lido:.2f}€ no talão mas não percebi a loja.\nQue loja foi? (ex: Pingo Doce, Continente, Zara...)')
                        set_estado(phone, 'aguardar_loja_talao', {'valor': valor_lido})
                    else:
                        u_temp = Usuario.query.filter_by(phone=phone).first()
                        if u_temp and ler_etiqueta_wishlist(phone_raw, u_temp, url, mime):
                            return jsonify({'status':'ok'})
                        # Vai direto para processar como gasto sem mensagem intermédia
                        texto = resultado
                else:
                    u_temp = Usuario.query.filter_by(phone=phone).first()
                    if u_temp and ler_foto_km(phone_raw, u_temp, url, mime): return jsonify({'status':'ok'})
                    if u_temp and ler_etiqueta_wishlist(phone_raw, u_temp, url, mime): return jsonify({'status':'ok'})
                    enviar_mensagem(phone_raw, "Não consegui ler 😕 Escreve o valor e a loja!"); return jsonify({'status':'ok'})
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

# ─── API DASHBOARD ───────────────────────────────────────────
@app.route('/api/dashboard', methods=['GET'])
def api_dashboard():
    """API para o dashboard visual. Autentica por token."""
    token = request.args.get('token') or request.headers.get('X-Token','')
    phone = request.args.get('phone','')
    # Token simples: primeiros 8 chars do phone + "zef"
    expected = (phone[:8] + 'zef') if phone else ''
    if not token or token != expected:
        return jsonify({'error':'unauthorized'}), 401

    usuario = Usuario.query.filter_by(phone=phone).first()
    if not usuario:
        return jsonify({'error':'user not found'}), 404

    mes = agora().month; ano = agora().year
    mes_ant = mes-1 if mes>1 else 12; ano_ant = ano if mes>1 else ano-1

    # Gastos por categoria este mês
    por_cat = db.session.query(Despesa.categoria, db.func.sum(Despesa.valor)).filter(
        Despesa.usuario_id==usuario.id,
        db.extract('month',Despesa.data)==mes,
        db.extract('year',Despesa.data)==ano
    ).group_by(Despesa.categoria).all()

    # Gastos últimos 6 meses
    historico = []
    for i in range(5, -1, -1):
        m = mes - i; y = ano
        if m <= 0: m += 12; y -= 1
        total = db.session.query(db.func.sum(Despesa.valor)).filter(
            Despesa.usuario_id==usuario.id,
            db.extract('month',Despesa.data)==m,
            db.extract('year',Despesa.data)==y
        ).scalar() or 0
        nomes = ['Jan','Fev','Mar','Abr','Mai','Jun','Jul','Ago','Set','Out','Nov','Dez']
        historico.append({'mes': nomes[m-1], 'total': round(total, 2)})

    # Disponível
    modo = get_modo(usuario.id)
    futuras = DespesaFutura.query.filter(DespesaFutura.usuario_id==usuario.id, DespesaFutura.pago==False).all()
    total_fut = sum(d.valor_reserva_mensal for d in futuras)
    p = calcular_plano(usuario.salario_liquido or 0, modo, total_fut)
    gastos_mes = sum(v for _, v in por_cat)
    disp = p['gastar'] - gastos_mes
    reserva = get_reserva(usuario.id)

    # Wishlist
    wishlist = db.session.execute(text(
        "SELECT descricao, preco, marca, categoria FROM wishlist WHERE usuario_id=:id AND comprado=FALSE ORDER BY criado_em DESC LIMIT 10"),
        {'id':usuario.id}).fetchall()

    # Splits pendentes
    splits = db.session.execute(text(
        "SELECT descricao, valor_cada, pessoa FROM splitting WHERE usuario_id=:u AND pago=FALSE"),
        {'u':usuario.id}).fetchall()

    # Objetivos
    objetivos = db.session.execute(text(
        "SELECT descricao, valor_objetivo, valor_atual FROM objetivos_poupanca WHERE usuario_id=:id AND concluido=FALSE"),
        {'id':usuario.id}).fetchall()

    # Transações recentes (últimos 30 registos)
    transacoes = db.session.execute(text(
        "SELECT descricao, valor, categoria, data FROM despesas WHERE usuario_id=:id ORDER BY data DESC LIMIT 30"),
        {'id':usuario.id}).fetchall()

    nomes_mes_full = ['Janeiro','Fevereiro','Março','Abril','Maio','Junho','Julho','Agosto','Setembro','Outubro','Novembro','Dezembro']

    return jsonify({
        'nome': usuario.nome or 'Utilizador',
        'mes': nomes_mes_full[mes-1],
        'salario': usuario.salario_liquido or 0,
        'disponivel': round(disp, 2),
        'poupanca_prevista': p['poupanca'],
        'reserva': reserva,
        'modo': modo,
        'gastos_mes': round(gastos_mes, 2),
        'por_categoria': [{'cat': c, 'total': round(v, 2)} for c, v in sorted(por_cat, key=lambda x:-x[1])],
        'historico_6m': historico,
        'wishlist': [{'nome': r[0], 'preco': r[1], 'marca': r[2], 'cat': r[3]} for r in wishlist],
        'splits': [{'desc': r[0], 'valor': r[1], 'pessoa': r[2]} for r in splits],
        'objetivos': [{'desc': r[0], 'objetivo': r[1], 'atual': r[2], 'pct': round(r[2]/r[1]*100 if r[1] else 0)} for r in objetivos],
        'dias_salario': dias_para_salario(),
        'transacoes': [{'desc': r[0], 'valor': round(r[1],2), 'cat': r[2], 'data': r[3].strftime('%d/%m %H:%M') if r[3] else ''} for r in transacoes],
    })

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
                {'type':'text','text':(
                    'Le este talao/recibo. '
                    'Se for salario: "X,XX euros SALARIO". '
                    'Se for talao de compra: "X,XX euros NOME_DA_LOJA" onde NOME_DA_LOJA e o nome real da loja no cabecalho (ex: Pingo Doce, Continente, Zara, McDonald\'s, Lidl). '
                    'Exemplos: "7,27 euros Pingo Doce" ou "1327,92 euros SALARIO" ou "25,50 euros Continente". '
                    'Nunca uses a palavra LOJA — usa sempre o nome real. '
                    'Se nao conseguires ler: "erro"'
                )}
            ]}])
        txt = resp.choices[0].message.content.strip()
        log.info(f"Talao: {txt}")
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


# ─── PICOS / HORAS EXTRAS ────────────────────────────────────
def _parse_hora_pico(texto):
    m = re.search(r"(\d{1,2})(?:[h:](\d{0,2}))?", texto.strip().lower())
    if not m: return None
    hora = int(m.group(1)); minuto = int(m.group(2)) if m.group(2) else 0
    return (hora, minuto) if 0 <= hora <= 23 and 0 <= minuto <= 59 else None

def _eh_dia_folga_ruben(d):
    return d.weekday() in HORARIO_RUBEN["dias_folga"]

def pico_entrada(phone, texto):
    agora_dt = agora().replace(tzinfo=None); hora_entrada = agora_dt
    parte = re.sub(r"entr(ei|e|a)", "", texto.lower()).strip()
    if parte:
        h = _parse_hora_pico(parte)
        if h: hora_entrada = agora_dt.replace(hour=h[0], minute=h[1], second=0, microsecond=0)
    hoje = hora_entrada.date(); folga = _eh_dia_folga_ruben(hoje)
    dias_pt = ["Seg","Ter","Qua","Qui","Sex","Sab","Dom"]
    try:
        db.session.execute(text(
            "INSERT INTO picos (user_phone,data,entrada,dia_folga) VALUES (:p,:d,:e,:f) "
            "ON CONFLICT (user_phone,data) DO UPDATE SET entrada=EXCLUDED.entrada,dia_folga=EXCLUDED.dia_folga"),
            {"p":phone,"d":hoje,"e":hora_entrada,"f":folga})
        db.session.commit()
    except Exception as e:
        log.error(f"pico_entrada: {e}"); db.session.rollback(); return "Erro ao registar entrada"
    aviso = "\n📌 Dia de folga - tudo conta como extra!" if folga else ""
    return (f"Entrada: {hora_entrada.strftime('%H:%M')} "
            f"({dias_pt[hoje.weekday()]} {hoje.strftime('%d/%m')}){aviso}\nDiz sai quando saíres.")

def pico_saida(phone, texto):
    agora_dt = agora().replace(tzinfo=None); hora_saida = agora_dt
    parte = re.sub(r"sa[ii]u?", "", texto.lower()).strip()
    if parte:
        h = _parse_hora_pico(parte)
        if h: hora_saida = agora_dt.replace(hour=h[0], minute=h[1], second=0, microsecond=0)
    hoje = hora_saida.date()
    try:
        row = db.session.execute(text(
            "SELECT entrada,dia_folga FROM picos WHERE user_phone=:p AND data=:d"),
            {"p":phone,"d":hoje}).fetchone()
    except Exception as e:
        log.error(f"pico_saida: {e}"); db.session.rollback(); return "Erro"
    if not row or not row[0]:
        return "Nao encontrei entrada de hoje.\nRegista primeiro: entrei 9h"
    entrada = row[0]; folga = row[1]
    horas_total = (hora_saida - entrada).total_seconds() / 3600
    horas_extra = round(horas_total if folga else max(0, horas_total - HORARIO_RUBEN["horas_dia"]), 2)
    try:
        db.session.execute(text(
            "UPDATE picos SET saida=:s,horas_trabalhadas=:ht,horas_extra=:he WHERE user_phone=:p AND data=:d"),
            {"s":hora_saida,"ht":round(horas_total,2),"he":horas_extra,"p":phone,"d":hoje})
        db.session.commit()
    except Exception as e:
        log.error(f"pico_saida update: {e}"); db.session.rollback(); return "Erro"
    dias_pt = ["Seg","Ter","Qua","Qui","Sex","Sab","Dom"]
    dia_nome = dias_pt[hoje.weekday()]
    h = int(horas_extra); m_min = int((horas_extra-h)*60)
    extra_str = f"{h}h{m_min:02d}" if m_min else f"{h}h"
    if horas_extra > 0:
        return (f"Saida: {hora_saida.strftime('%H:%M')} ({dia_nome} {hoje.strftime('%d/%m')})"
                f"\nTrabalhaste {horas_total:.1f}h - {extra_str} extra registado!")
    return (f"Saida: {hora_saida.strftime('%H:%M')} ({dia_nome} {hoje.strftime('%d/%m')})"
            f"\nTrabalhaste {horas_total:.1f}h - sem extras hoje.")

def picos_hoje_fn(phone):
    hoje = agora().date()
    dias_pt = ["Seg","Ter","Qua","Qui","Sex","Sab","Dom"]
    folga = _eh_dia_folga_ruben(hoje)
    prefixo = f"Hoje - {dias_pt[hoje.weekday()]} {hoje.strftime('%d/%m')}"
    if folga: prefixo += " (folga - tudo e extra)"
    try:
        row = db.session.execute(text(
            "SELECT entrada,saida,horas_trabalhadas,horas_extra FROM picos WHERE user_phone=:p AND data=:d"),
            {"p":phone,"d":hoje}).fetchone()
    except Exception as e:
        log.error(f"picos_hoje: {e}"); db.session.rollback(); return "Erro"
    if not row or not row[0]:
        return f"{prefixo}\nSem entrada. Diz entrei quando chegares."
    entrada, saida, horas, extra = row
    if not saida:
        decorrido = (agora().replace(tzinfo=None) - entrada).total_seconds() / 3600
        return f"{prefixo}\nEntrada: {entrada.strftime('%H:%M')} - a trabalhar ha {decorrido:.1f}h"
    h = int(extra); m_min = int((extra-h)*60)
    extra_str = f"{h}h{m_min:02d}" if m_min else f"{h}h"
    msg_extra = f"{extra_str} extra" if extra > 0 else "sem horas extra"
    return f"{prefixo}\n{entrada.strftime('%H:%M')} -> {saida.strftime('%H:%M')} ({horas:.1f}h) - {msg_extra}"

def picos_resumo_fn(phone, mes=None, ano=None):
    import calendar
    hoje = agora().date(); mes = mes or hoje.month; ano = ano or hoje.year
    _, ultimo_dia = calendar.monthrange(ano, mes)
    try:
        rows = db.session.execute(text(
            "SELECT data,horas_extra,dia_folga FROM picos "
            "WHERE user_phone=:p AND data>=:ini AND data<=:fim AND horas_extra>0 ORDER BY data"),
            {"p":phone,"ini":f"{ano}-{mes:02d}-01","fim":f"{ano}-{mes:02d}-{ultimo_dia:02d}"}).fetchall()
    except Exception as e:
        log.error(f"picos_resumo: {e}"); db.session.rollback(); return "Erro"
    nomes_m = ["","Janeiro","Fevereiro","Marco","Abril","Maio","Junho",
               "Julho","Agosto","Setembro","Outubro","Novembro","Dezembro"]
    dias_pt = ["Seg","Ter","Qua","Qui","Sex","Sab","Dom"]
    if not rows: return f"Sem horas extra em {nomes_m[mes]}."
    linhas = []; total = 0
    for data, extra, folga in rows:
        sufixo = " (folga)" if folga else ""
        h = int(extra); m_min = int((extra-h)*60)
        extra_str = f"{h}h{m_min:02d}" if m_min else f"{h}h"
        linhas.append(f"{data.strftime('%d/%m')} ({dias_pt[data.weekday()]}{sufixo}) - {extra_str} extra")
        total += extra
    th = int(total); tm = int((total-th)*60)
    total_str = f"{th}h{tm:02d}" if tm else f"{th}h"
    return f"Horas extra {nomes_m[mes]}:\n\n" + "\n".join(linhas) + f"\n\nTotal: {total_str}"

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

        if estado == 'wishlist_tipo_pendente':
            dados_t = dados_estado
            tipo_escolha = t.strip()
            nome_tipo = TIPOS_ROUPA.get(tipo_escolha)
            if nome_tipo:
                limpar_estado(phone)
                marca = dados_t.get('marca','')
                preco = dados_t.get('preco')
                link  = dados_t.get('link')
                cat   = dados_t.get('cat', 'roupa')
                est   = dados_t.get('est')
                ref   = dados_t.get('ref','')
                desc  = f"{nome_tipo} ({marca})" if marca else nome_tipo
                if ref and ref.strip(): desc += f" ({ref})"
                # Categoria por tipo
                if nome_tipo == 'Calçado': cat = 'calcado'
                elif nome_tipo in ['T-Shirt','Polo','Camisa','Sweatshirt','Casaco','Calças','Calções','Jeans']: cat = 'roupa'
                try:
                    db.session.execute(text(
                        "INSERT INTO wishlist (usuario_id,descricao,preco,link,marca,categoria,estacao) VALUES (:u,:d,:p,:l,:m,:c,:e)"),
                        {'u':usuario.id,'d':desc,'p':preco,'l':link,'m':marca,'c':cat,'e':est})
                    db.session.commit()
                    preco_txt = f" — {preco:.2f}€" if preco else ""
                    enviar_mensagem(phone_raw, f"🛍️ Guardado! {desc}{preco_txt}\nDiz 'wishlist' para ver 😊")
                except Exception as e:
                    log.error(f"wishlist tipo: {e}"); enviar_mensagem(phone_raw, "Erro 😕")
            else:
                enviar_mensagem(phone_raw, "Responde com o número:\n1 T-Shirt   2 Polo   3 Camisa\n4 Sweatshirt   5 Casaco   6 Calças\n7 Calções   8 Jeans   9 Calçado   0 Outro")
            return
            mapa = dados_estado.get('mapa', {})
            # "comprar 1" ou "comprei 1"
            m_comprar = re.search(r'(?:comprar|comprei|comprado)\s+(\d+)', t)
            if m_comprar:
                num = m_comprar.group(1)
                item_id = mapa.get(num)
                if item_id:
                    try:
                        r = db.session.execute(text(
                            "UPDATE wishlist SET comprado=TRUE WHERE id=:id AND usuario_id=:u RETURNING descricao,preco"),
                            {'id':item_id,'u':usuario.id}).fetchone()
                        db.session.commit()
                        if r:
                            nome_limpo = limpar_nome_wishlist(r[0])
                            enviar_mensagem(phone_raw, f"✅ '{nome_limpo}' marcado como comprado! 🎉\nVai registar o gasto?")
                    except Exception: enviar_mensagem(phone_raw, "Erro 😕")
                return
            # "remover 1" ou "remove 1"
            m_remover = re.search(r'(?:remover?|apagar?|tira[r]?)\s+(\d+)', t)
            if m_remover:
                num = m_remover.group(1)
                item_id = mapa.get(num)
                if item_id:
                    try:
                        r = db.session.execute(text(
                            "DELETE FROM wishlist WHERE id=:id AND usuario_id=:u RETURNING descricao"),
                            {'id':item_id,'u':usuario.id}).fetchone()
                        db.session.commit()
                        if r:
                            nome_limpo = limpar_nome_wishlist(r[0])
                            enviar_mensagem(phone_raw, f"🗑️ '{nome_limpo}' removido!")
                    except Exception: enviar_mensagem(phone_raw, "Erro 😕")
                return
            # Estado não consumido — continua processamento normal
            valor = dados_estado.get('valor', 0)
            limpar_estado(phone)
            if valor > 0 and texto.strip():
                processar_despesa(phone_raw, usuario, f"{valor} {texto.strip()}")
            else:
                enviar_mensagem(phone_raw, "Ok! Diz: X euros [loja] — ex: 7,27 Pingo Doce")
            return

        if estado == 'resumo_categoria':
            cats = dados_estado.get('cats', {})
            mes_r = dados_estado.get('mes', agora().month)
            ano_r = dados_estado.get('ano', agora().year)
            if t.strip() in cats:
                cat_sel = cats[t.strip()]
                limpar_estado(phone)
                try:
                    rows = db.session.query(Despesa).filter(
                        Despesa.usuario_id==usuario.id,
                        db.extract('month',Despesa.data)==mes_r,
                        db.extract('year',Despesa.data)==ano_r,
                        Despesa.categoria==cat_sel
                    ).order_by(Despesa.data.desc()).all()
                    total = sum(d.valor for d in rows)
                    nomes_m = ['Jan','Fev','Mar','Abr','Mai','Jun','Jul','Ago','Set','Out','Nov','Dez']
                    msg = f"{EMOJI_CAT.get(cat_sel,'💳')} {cat_sel.capitalize()} — {nomes_m[mes_r-1]}\n\n"
                    for d in rows[:15]:
                        desc = d.descricao.replace('[conjunta] ','').replace('[reserva] ','')[:30]
                        msg += f"• {d.valor:.2f}€ — {desc}\n"
                    msg += f"\n💰 Total: {total:.2f}€"
                    enviar_mensagem(phone_raw, msg)
                except Exception as e:
                    log.error(f"cat detalhe: {e}"); enviar_mensagem(phone_raw, "Erro 😕")
                return
            else:
                limpar_estado(phone)
                # continua processamento normal

        if estado == 'aguardar_confirmacao_wishlist':
            desc_w = dados_estado.get('desc','')
            preco_w = dados_estado.get('preco')
            cat_w = dados_estado.get('cat','outros')
            if any(p in t for p in ['sim','s','yes','1','quero','adiciona']):
                limpar_estado(phone)
                try:
                    db.session.execute(text("INSERT INTO wishlist (usuario_id,descricao,preco,categoria) VALUES (:u,:d,:p,:c)"),
                        {'u':usuario.id,'d':desc_w,'p':preco_w,'c':cat_w})
                    db.session.commit()
                    enviar_mensagem(phone_raw, f"🛍️ '{desc_w}' adicionado à wishlist! Diz 'wishlist' para ver 😊")
                except Exception: enviar_mensagem(phone_raw, "Erro 😕")
            elif any(p in t for p in ['nao','não','n','2']):
                limpar_estado(phone)
                processar_despesa(phone_raw, usuario, f"{desc_w} {preco_w}€")
            else:
                enviar_mensagem(phone_raw, "Adicionar à wishlist? Responde sim ou não")
            return

        if estado == 'objetivo_data':
            valor_obj = dados_estado.get('valor', 0)
            desc_obj = dados_estado.get('desc', 'Objetivo')
            limpar_estado(phone)
            # Tenta extrair mês/data da resposta
            meses_map2 = {'janeiro':1,'fevereiro':2,'marco':3,'março':3,'abril':4,'maio':5,'junho':6,
                         'julho':7,'agosto':8,'setembro':9,'outubro':10,'novembro':11,'dezembro':12}
            mes_alvo = None
            for nome_m, num_m in meses_map2.items():
                if nome_m in t: mes_alvo = num_m; break
            if not mes_alvo:
                m_num = re.search(r'(\d{1,2})', t)
                if m_num: mes_alvo = int(m_num.group(1))
            hoje = agora()
            if mes_alvo:
                ano_alvo = hoje.year if mes_alvo > hoje.month else hoje.year + 1
                meses_falta = (ano_alvo - hoje.year) * 12 + (mes_alvo - hoje.month)
                meses_falta = max(meses_falta, 1)
            else:
                meses_falta = 6
            por_mes = round(valor_obj / meses_falta, 2)
            try:
                db.session.execute(text(
                    "INSERT INTO objetivos_poupanca (usuario_id,descricao,valor_objetivo,valor_atual) VALUES (:u,:d,:v,0)"),
                    {'u':usuario.id,'d':desc_obj,'v':valor_obj})
                db.session.commit()
                nomes_m = ['Jan','Fev','Mar','Abr','Mai','Jun','Jul','Ago','Set','Out','Nov','Dez']
                mes_txt = nomes_m[mes_alvo-1] if mes_alvo else '?'
                enviar_mensagem(phone_raw,
                    f"🎯 Objetivo criado!\n📌 {desc_obj}: {valor_obj:.0f}€\n"
                    f"📅 Meta: {mes_txt}\n"
                    f"💰 Precisas de guardar ~{por_mes:.0f}€/mês durante {meses_falta} meses\n\n"
                    f"Vou avisar-te quando atingires 25%, 50%, 75% e 100%! 💪")
            except Exception as e:
                log.error(f"obj data: {e}"); enviar_mensagem(phone_raw, "Erro 😕")
            return


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

        # ── PICOS / HORAS EXTRAS (so Ruben) ──────────────────────────
        if phone == PHONE_RUBEN:
            if re.match(r'^entr(ei|e|a)', t):
                enviar_mensagem(phone_raw, pico_entrada(phone, t)); return
            if re.match(r'^sa[ii]', t) or t.strip() in ['sai','saiu']:
                enviar_mensagem(phone_raw, pico_saida(phone, t)); return
            if t.strip() == 'picos hoje':
                enviar_mensagem(phone_raw, picos_hoje_fn(phone)); return
            if re.match(r'^picos', t):
                meses_map = {
                    'janeiro':1,'fevereiro':2,'marco':3,'marco':3,'abril':4,
                    'maio':5,'junho':6,'julho':7,'agosto':8,'setembro':9,
                    'outubro':10,'novembro':11,'dezembro':12
                }
                mes_n = next((v for k,v in meses_map.items() if k in t), None)
                enviar_mensagem(phone_raw, picos_resumo_fn(phone, mes=mes_n)); return
        # ──────────────────────────────────────────────────────────────
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
                             'onde é a gasolina','combustivel mais barato']
        municipios = ['barreiro','moita','seixal','almada','montijo','palmela','alcochete','setubal','setúbal']
        e_municipio_gas = any(m in t for m in municipios) and any(p in t for p in ['gasolina','combustivel','posto','barata','barato','preco','preço','valor'])
        e_na_local = bool(re.search(r'^e\s+(na|no)\s+(moita|barreiro|seixal|almada|montijo)[\s?!.]*$', t.strip()))
        if any(p in t for p in gasolina_keywords) or e_municipio_gas or e_na_local:
            gasolina_barata(phone_raw, t); return

        if any(p in t for p in ['recebemos','metemos','depositamos','deposito']) and 'conjunta' in t and tem_numero(texto):
            registar_deposito_conjunta(phone_raw, usuario, texto); return
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
            # Mesmo com valor vai para wishlist — pergunta se é wishlist ou gasto
            valor_q = extrair_valor(texto)
            stop_q = {'quero','uma','um','umas','uns'}
            palavras_q = [w for w in texto.split() if not re.match(r'[0-9€,.]',w) and len(w)>2 and w.lower() not in stop_q]
            desc_q = ' '.join(palavras_q[:4]).capitalize() if palavras_q else 'Item'
            cat_q, _ = detetar_cat_wishlist(texto)
            phone_tmp = phone_raw.replace('@lid','').replace('@c.us','').split('@')[0]
            set_estado(phone_tmp, 'aguardar_confirmacao_wishlist', {'desc':desc_q,'preco':valor_q if valor_q>0 else None,'cat':cat_q})
            preco_txt = f" — {valor_q:.2f}€" if valor_q > 0 else ""
            enviar_mensagem(phone_raw, f"🛍️ {desc_q}{preco_txt}\nAdicionas à wishlist?\n1 - Sim\n2 - Não (registar como gasto)")
            return

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

        # ── MODO CASAL ──
        if any(p in t for p in ['resumo casal','financas casal','finanças casal','dashboard casal']):
            enviar_resumo_casal(phone_raw, usuario); return

        if re.search(r'objetivo casal|meta casal|poupar.*casal|casal.*poupar', t):
            processar_objetivo_casal(phone_raw, usuario, texto); return

        if any(p in t for p in ['quem gastou mais','comparar','competicao','competição','batalha']):
            enviar_comparacao_casal(phone_raw, usuario); return

        # ── X PAGOU / MARCAR SPLIT PAGO ──
        m_pagou = re.search(r'([A-Za-zÀ-ú]{2,})\s+pagou', t)
        if m_pagou:
            pessoa_pagou = m_pagou.group(1).capitalize()
            try:
                r = db.session.execute(text(
                    "UPDATE splitting SET pago=TRUE WHERE usuario_id=:u AND LOWER(pessoa)=LOWER(:p) AND pago=FALSE RETURNING descricao,valor_cada"),
                    {'u':usuario.id,'p':pessoa_pagou}).fetchone()
                db.session.commit()
                if r: enviar_mensagem(phone_raw, f"✅ {pessoa_pagou} pagou {r[1]:.2f}€ — {r[0]}! 🎉")
                else: enviar_mensagem(phone_raw, f"Não encontrei splits pendentes com {pessoa_pagou} 🤔")
            except Exception as e:
                log.error(f"pagou: {e}"); enviar_mensagem(phone_raw, "Erro 😕")
            return

        # ── DÍVIDAS ──
        if any(p in t for p in ['devo ','deve-me','devem-me','me deve']) and tem_numero(texto):
            processar_dividas(phone_raw, usuario, texto); return

        # ── MODO DISCRETO ──
        if any(p in t for p in ['limpa conversa','apaga mensagens','modo discreto','limpar chat']):
            modo_discreto(phone_raw); return

        # ── VW TAIGO — calcula km ao abastecer ──
        if any(p in t for p in ['abasteci','meti gasolina','pus gasolina']) and tem_numero(texto):
            valor_gas = extrair_valor(texto)
            if valor_gas > 5:
                litros = round(valor_gas / 1.9, 1)
                km_est = round(litros / 5.4 * 100)
                processar_despesa(phone_raw, usuario, texto)
                enviar_mensagem(phone_raw, f"🚗 Com {valor_gas:.0f}€ tens ~{litros:.1f}L\n📍 Dá para ~{km_est} km no Taigo!\nManda foto do odómetro 📸")
                return

        # ── RESPOSTAS CURTAS ──
        respostas_curtas = {'obrigada','obrigado','obg','thanks','fixe','ok','okay','boa','top','perfeito','ótimo','otimo'}
        if t.strip() in respostas_curtas:
            import random
            resps = ["😊","De nada! 💪","Boa! 😎","Sempre! 🙌","👍"]
            enviar_mensagem(phone_raw, random.choice(resps)); return

        # ── GASTO (texto/sem keyword) ──
        if tem_numero(texto) and eh_gasto(texto):
            valor_check = extrair_valor(texto)
            # Gasto grande
            if valor_check >= 50:
                disp_check, _ = calcular_disponivel(usuario)
                if valor_check > disp_check * 0.4:
                    processar_despesa(phone_raw, usuario, texto)
                    enviar_mensagem(phone_raw, f"😮 {valor_check:.0f}€ de uma vez! Foi especial ou necessário? 😅")
                    return
            # Detetar duplicados
            categoria_check, _, nome_check = categorizar(texto)
            hoje_inicio = agora().replace(hour=0, minute=0, second=0, tzinfo=None)
            valor_hoje_cat = db.session.query(db.func.sum(Despesa.valor)).filter(
                Despesa.usuario_id==usuario.id, Despesa.categoria==categoria_check,
                Despesa.data>=hoje_inicio).scalar() or 0
            if valor_hoje_cat > 0 and abs(valor_check - valor_hoje_cat) < 2:
                processar_despesa(phone_raw, usuario, texto)
                enviar_mensagem(phone_raw, f"⚠️ Já tinhas {valor_hoje_cat:.2f}€ em {nome_check} hoje — é outro gasto?")
                return
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
    total_cat = gastos_cat_mes(usuario, categoria, mes, ano)
    total_cat_ant = gastos_cat_mes(usuario, categoria, mes_ant, ano_ant)

    # Gasto na conjunta — mostra dados da conjunta, não do orçamento principal
    if na_conjunta:
        gc = db.session.query(db.func.sum(Despesa.valor)).filter(
            Despesa.usuario_id==usuario.id,
            db.extract('month',Despesa.data)==mes, db.extract('year',Despesa.data)==ano,
            Despesa.descricao.like('[conjunta]%')).scalar() or 0
        resta_conj = 50 - gc
        pessoa_txt = f" (com {pessoa})" if pessoa else ""
        msg = f"{emoji} {nome_loja} — {valor:.2f}€ 💑{pessoa_txt}\n"
        msg += f"📊 Conjunta este mês: {gc:.2f}€ de 50€\n"
        if resta_conj >= 0:
            msg += f"💑 Resta: {resta_conj:.2f}€ na conjunta"
        else:
            msg += f"⚠️ Passaste {abs(resta_conj):.2f}€ na conjunta!"
        enviar_mensagem(phone_raw, msg)

        # Notifica o parceiro
        meu_nome = NOMES_CASAL.get(usuario.phone, 'O parceiro')
        emoji_aviso = "💑"
        notif_msg = (f"{emoji_aviso} *{meu_nome}* gastou {valor:.2f}€ na conjunta\n"
                     f"📍 {nome_loja}\n"
                     f"💑 Conjunta este mês: {gc:.2f}€ de 50€ · Resta {max(resta_conj,0):.2f}€")
        notificar_parceiro(usuario.phone, notif_msg)
        return

    disp, p = calcular_disponivel(usuario)
    gastar = p['gastar']
    pct_usado = ((gastar-disp)/gastar*100) if gastar>0 else 0

    # Linha principal
    pessoa_txt = f" (com {pessoa})" if pessoa else ""
    msg = f"{emoji} {nome_loja} — {valor:.2f}€{pessoa_txt}\n"
    msg += f"📊 {categoria.capitalize()} este mês: {total_cat:.2f}€"

    # Comentário de padrão
    inicio_semana = agora().replace(tzinfo=None) - timedelta(days=agora().weekday())
    vezes_semana = db.session.query(db.func.count(Despesa.id)).filter(
        Despesa.usuario_id==usuario.id, Despesa.categoria==categoria,
        Despesa.data>=inicio_semana).scalar() or 0

    if categoria=='fastfood' and vezes_semana>=3:
        msg += f"\n😏 Já é a {vezes_semana}.ª vez de fast food esta semana!"
    elif categoria=='gota' and total_cat>30:
        msg += f"\n🧃 {total_cat:.0f}€ em bebidas este mês... abranda!"
    elif categoria=='combustivel' and total_cat>BASE_COMBUSTIVEL:
        msg += f"\n⛽ Passaste a base de {BASE_COMBUSTIVEL}€ em gasolina"
    elif agora().weekday() in [4,5] and agora().hour>=19 and categoria in ['restaurante','fastfood']:
        msg += "\n🍻 Fim de semana à noite, lá vem o costume!"
    elif total_cat_ant>0 and total_cat>total_cat_ant*1.3:
        msg += f"\n⚠️ Já gastaste mais em {categoria} que o mês passado todo!"
    elif total_cat_ant>0 and total_cat<total_cat_ant*0.6:
        msg += f"\n✅ Bem menos em {categoria} que o mês passado!"

    # Aviso orçamento
    if pct_usado >= 100:
        msg += f"\n⚠️ Passaste o orçamento em {abs(disp):.0f}€"
    elif pct_usado >= 80:
        msg += f"\n🔔 Usaste {pct_usado:.0f}% do orçamento"

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
        # Celebrar se poupou mais que o mês anterior
        try:
            gasto_mes_ant = db.session.query(db.func.sum(Despesa.valor)).filter(
                Despesa.usuario_id==usuario.id,
                db.extract('month',Despesa.data)==mes_ant,
                db.extract('year',Despesa.data)==ano_ant,
                ~Despesa.descricao.like('[conjunta]%')).scalar() or 0
            gasto_mes_ant_ant_mes = mes_ant-1 if mes_ant>1 else 12
            gasto_mes_ant_ant_ano = ano_ant if mes_ant>1 else ano_ant-1
            gasto_ant_ant = db.session.query(db.func.sum(Despesa.valor)).filter(
                Despesa.usuario_id==usuario.id,
                db.extract('month',Despesa.data)==gasto_mes_ant_ant_mes,
                db.extract('year',Despesa.data)==gasto_mes_ant_ant_ano,
                ~Despesa.descricao.like('[conjunta]%')).scalar() or 0
            if gasto_ant_ant > 0 and gasto_mes_ant < gasto_ant_ant:
                diferenca = gasto_ant_ant - gasto_mes_ant
                enviar_mensagem(phone_raw, f"🎉 No mês passado gastaste {diferenca:.0f}€ a menos que no mês anterior! Estás a bombar! 💪🏆")
        except Exception: pass
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
    mes=agora().month; ano=agora().year
    gc = db.session.query(db.func.sum(Despesa.valor)).filter(
        Despesa.usuario_id==usuario.id,
        db.extract('month',Despesa.data)==mes, db.extract('year',Despesa.data)==ano,
        Despesa.descricao.like('[conjunta]%')).scalar() or 0
    resta_conj = 50 - gc
    modo = get_modo(usuario.id); m = MODOS_POUPANCA[modo]
    dias = dias_para_salario()

    msg = f"💰 Resumo de saldos {m['emoji']}\n\n"
    msg += f"💳 Para gastar: {disp:.2f}€"
    if disp < 0: msg += " ⚠️"
    msg += f"\n💑 Conjunta: {max(resta_conj,0):.2f}€ disponíveis"
    msg += f"\n💎 Poupança prevista: {p['poupanca']:.0f}€"
    msg += f"\n🛡️ Reserva: {reserva:.2f}€"
    msg += f"\n\n📅 Faltam {dias} dia{'s' if dias!=1 else ''} para o salário"

    if disp > 0 and dias > 0:
        por_dia = round(disp/dias, 2)
        msg += f" — podes gastar ~{por_dia:.2f}€/dia"

    if disp < 0:
        msg += f"\n\n⚠️ Passaste o orçamento em {abs(disp):.0f}€!"
    elif disp < 30:
        msg += f"\n\n😬 Tás quase no limite!"
    enviar_mensagem(phone_raw, msg)

def registar_deposito_conjunta(phone_raw, usuario, texto):
    valor = extrair_valor(texto)
    if valor == 0:
        enviar_mensagem(phone_raw, "Quanto receberam? Ex: recebemos 100 euros na conjunta"); return
    stop = {'recebemos','metemos','deposito','euros','euro','na','conjunta','para','a','da'}
    palavras = [w for w in texto.split()
                if not re.match(r'[0-9€,.]', w) and len(w) > 2 and w.lower() not in stop]
    desc = ' '.join(palavras[:3]).capitalize() if palavras else 'Deposito'
    try:
        db.session.execute(text(
            "INSERT INTO conjunta_depositos (usuario_id, valor, descricao) VALUES (:u, :v, :d)"
        ), {'u': usuario.id, 'v': valor, 'd': desc})
        db.session.commit()
    except Exception as e:
        log.error(f"deposito_conjunta: {e}"); db.session.rollback()
        enviar_mensagem(phone_raw, "Erro 😕"); return
    meu_nome = NOMES_CASAL.get(usuario.phone, 'O parceiro')
    notificar_parceiro(usuario.phone, f"💑 {meu_nome} adicionou {valor:.0f}€ à conjunta ({desc})")
    enviar_mensagem(phone_raw,
        f"💑 +{valor:.0f}€ adicionado à conjunta!\n"
        f"📌 {desc}\n"
        f"Diz 'quanto tenho na conjunta' para ver o saldo 💚")

def enviar_conjunta(phone_raw, usuario):
    mes = agora().month; ano = agora().year
    gasto = db.session.query(db.func.sum(Despesa.valor)).filter(
        Despesa.usuario_id==usuario.id,
        db.extract('month',Despesa.data)==mes,
        db.extract('year',Despesa.data)==ano,
        Despesa.descricao.like('[conjunta]%')).scalar() or 0
    parceiro_phone = get_parceiro_phone(usuario.phone)
    parceiro = Usuario.query.filter_by(phone=parceiro_phone).first() if parceiro_phone else None
    ids = [usuario.id] + ([parceiro.id] if parceiro else [])
    total = 0
    for uid in ids:
        total += db.session.execute(text(
            "SELECT COALESCE(SUM(valor),0) FROM conjunta_depositos "
            "WHERE usuario_id=:u AND EXTRACT(month FROM data)=:m AND EXTRACT(year FROM data)=:y"
        ), {'u': uid, 'm': mes, 'y': ano}).scalar() or 0
    resta = total - gasto
    if total == 0:
        enviar_mensagem(phone_raw,
            f"💑 Conta conjunta\n"
            f"📭 Ainda nao meteram dinheiro este mes.\n\n"
            f"Para meter: 'metemos 80 na conjunta'")
        return
    status = "Dentro!" if resta >= 0 else f"Passaram {abs(resta):.0f} euros!"
    enviar_mensagem(phone_raw,
        f"💑 Conta conjunta\n"
        f"💰 Total: {total:.0f}€\n"
        f"🛒 Gasto: {gasto:.2f}€\n"
        f"💚 Resta: {max(resta,0):.2f}€ {status}\n\n"
        f"Para meter mais: 'metemos 50 na conjunta'")

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
    por_cat_sorted = sorted(por_cat, key=lambda x:-x[1])

    modo = get_modo(usuario.id)
    futuras = DespesaFutura.query.filter(DespesaFutura.usuario_id==usuario.id, DespesaFutura.pago==False).all()
    total_fut = sum(d.valor_reserva_mensal for d in futuras)
    p = calcular_plano(receita or 0, modo, total_fut)
    disp = p['gastar'] - gp
    nomes = ['Janeiro','Fevereiro','Marco','Abril','Maio','Junho','Julho','Agosto','Setembro','Outubro','Novembro','Dezembro']

    msg = f"📊 {nomes[mes-1]}\n\n"
    msg += f"💰 Receita: {receita:.0f}€\n"
    msg += f"🛒 Gastos: {gp:.2f}€\n"
    if gc > 0: msg += f"💑 Conjunta: {gc:.2f}€\n"
    msg += f"💚 Disponível: {disp:.2f}€\n"
    msg += f"💎 Poupança: {p['poupanca']:.0f}€\n"

    if por_cat_sorted:
        msg += "\n📈 Categorias:\n"
        for i, (cat, total) in enumerate(por_cat_sorted, 1):
            pct = round(total/gp*100) if gp > 0 else 0
            msg += f"{i}. {EMOJI_CAT.get(cat,'💳')} {cat.capitalize()}: {total:.2f}€ ({pct}%)\n"

    if not mes_override and agora().day > 3 and gp > 0:
        ritmo = gp/agora().day*30
        msg += f"\n🔮 Ao ritmo atual: ~{ritmo:.0f}€ no fim do mês"
        if por_cat_sorted:
            top_cat, top_val = por_cat_sorted[0]
            if top_val > 50:
                msg += f"\n💡 Reduz {top_cat} 30% → poupa ~{top_val*0.3*12:.0f}€/ano"

    msg += "\n\nResponde com o número para ver os detalhes de cada categoria"

    # Guarda as categorias no estado para o utilizador poder selecionar
    cats_estado = {str(i+1): cat for i, (cat, _) in enumerate(por_cat_sorted)}
    phone = phone_raw.replace('@lid','').replace('@c.us','').split('@')[0]
    set_estado(phone, 'resumo_categoria', {'cats': cats_estado, 'mes': mes, 'ano': ano})
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
    por_dia = round(disp/dias, 2) if dias > 0 and disp > 0 else 0
    msg = f"😅 Modo teso!\n\n"
    msg += f"💳 Disponível: {disp:.2f}€\n"
    if por_dia > 0:
        msg += f"📅 {dias} dias até ao salário → {por_dia:.2f}€/dia\n"
    else:
        msg += f"📅 {dias} dias até ao salário — cuidado!\n"
    msg += f"🛡️ Reserva: {reserva:.2f}€ (só em emergências!)\n\n"
    msg += "Dicas:\n🍳 Cozinha em casa\n🚶 Anda a pé\n🛒 Só o essencial\n☕ Café de máquina\n💪 Consegues!"
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
    if re.search(r'\b(ver|lista|quais|mostrar)\b', t) or t.strip() in ['aniversarios','aniversários']:
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

LOJAS_POPULARES = [
    ('Zara', 'zara.com'), ('H&M', 'hm.com'), ('Bershka', 'bershka.com'),
    ('Pull&Bear', 'pullandbear.com'), ('Mango', 'mango.com'), ('ASOS', 'asos.com'),
    ('Shein', 'shein.com'), ('Stradivarius', 'stradivarius.com'),
    ('Nike', 'nike.com'), ('Adidas', 'adidas.pt'), ('JD Sports', 'jdsports.pt'),
    ('Snipes', 'snipes.com'), ('Foot Locker', 'footlocker.pt'),
    ('Primark', 'primark.com'), ('Lefties', 'lefties.com'),
    ('IKEA', 'ikea.com'), ('El Corte Inglés', 'elcorteingles.pt'),
    # Beleza e perfumes
    ('Primor', 'primor.eu'), ('Sephora', 'sephora.pt'),
    ('Douglas', 'douglas.pt'), ('Notino', 'notino.pt'),
    ('Perfumes Club', 'perfumesclub.pt'), ('Druni', 'druni.pt'),
    ('Wells', 'wells.pt'), ('Wook', 'wook.pt'),
]

def limpar_url(url):
    """Remove parâmetros de tracking do URL."""
    import urllib.parse
    parsed = urllib.parse.urlparse(url)
    tracking = {'utm_source','utm_medium','utm_campaign','utm_term','utm_content',
                'utm_id','gad_source','gad_campaignid','gbraid','gclid','fbclid',
                'ref','affiliate','source','medium','campaign'}
    params_limpos = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()
                     if k.lower() not in tracking}
    query_limpa = urllib.parse.urlencode(params_limpos)
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, '', query_limpa, ''))

def extrair_nome_loja(url):
    """Extrai nome da loja do URL correctamente. pt.primor.eu → Primor"""
    import urllib.parse
    netloc = urllib.parse.urlparse(url).netloc.replace('www.','')
    parts = netloc.split('.')
    # pt.primor.eu → ['pt','primor','eu'] → toma penúltimo: 'primor'
    # zara.com → ['zara','com'] → toma primeiro: 'zara'
    if len(parts) >= 3:
        nome_base = parts[-2]
    else:
        nome_base = parts[0]
    # Encontra nas lojas populares
    return next((l[0] for l in LOJAS_POPULARES if l[1].split('.')[0].lower() == nome_base.lower()), nome_base.capitalize())

def extrair_nome_produto_url(url):
    """Extrai nome do produto do path do URL. /ralph-lauren-polo-67-eau-de-toilette-112592.html → Ralph Lauren Polo 67 Eau De Toilette"""
    import urllib.parse
    path = urllib.parse.urlparse(url).path
    # Pega o último segmento do path
    segmento = path.rstrip('/').split('/')[-1]
    # Remove extensão
    segmento = re.sub(r'\.(html?|php|aspx?).*$', '', segmento)
    # Remove ID numérico no final (ex: -112592)
    segmento = re.sub(r'-\d{4,}$', '', segmento)
    # Remove parâmetros tipo _pt_pt
    segmento = re.sub(r'^[a-z]{2}_[a-z]{2}[-_]', '', segmento)
    # Converte hífens para espaços e capitaliza
    nome = segmento.replace('-', ' ').replace('_', ' ').title().strip()
    return nome if len(nome) > 3 else ''
    """Remove parâmetros de tracking do URL."""
    import urllib.parse
    parsed = urllib.parse.urlparse(url)
    # Remove parâmetros de tracking comuns
    params_limpos = {}
    tracking = {'utm_source','utm_medium','utm_campaign','utm_term','utm_content',
                'utm_id','gad_source','gad_campaignid','gbraid','gclid','fbclid',
                'ref','affiliate','source','medium','campaign'}
    for k, v in urllib.parse.parse_qs(parsed.query).items():
        if k.lower() not in tracking:
            params_limpos[k] = v[0]
    query_limpa = urllib.parse.urlencode(params_limpos)
    # Remove âncoras (#...)
    url_limpo = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, '', query_limpa, ''))
    return url_limpo

def comparar_precos_tavily(desc, marca=None):
    """Pesquisa produto e compara precos em várias lojas."""
    try:
        import requests as req, urllib.parse
        # Query sem restrição de domínio mas com nomes de lojas
        query = f"{desc} comprar preco"
        if marca and marca.lower() not in desc.lower():
            query = f"{marca} {desc} comprar preco"
        r = req.post("https://api.tavily.com/search",
            json={
                'api_key': TAVILY_API_KEY,
                'query': query,
                'search_depth': 'advanced',
                'max_results': 8,
            }, timeout=20)
        if r.status_code != 200: return []
        lojas = []; dominios_vistos = set()
        for res in r.json().get('results', []):
            url = res.get('url', ''); conteudo = res.get('content', '')
            netloc = urllib.parse.urlparse(url).netloc.replace('www.', '')
            parts = netloc.split('.')
            nome_base = parts[-2] if len(parts) >= 3 else parts[0]
            # Só inclui lojas conhecidas
            nome_loja = next((l[0] for l in LOJAS_POPULARES if l[1].split('.')[0].lower() == nome_base.lower()), None)
            if not nome_loja: continue
            if nome_base in dominios_vistos: continue
            dominios_vistos.add(nome_base)
            pm = re.search(r'(\d{1,3}[.,]\d{2})\s*€|€\s*(\d{1,3}[.,]\d{2})', conteudo)
            if pm:
                try:
                    preco = float((pm.group(1) or pm.group(2)).replace(',', '.'))
                    if 0 < preco < 10000:
                        lojas.append({'loja': nome_loja, 'preco': preco, 'url': url})
                except: pass
        return sorted(lojas, key=lambda x: x['preco'])[:5]
    except Exception as e:
        log.error(f"Tavily: {e}"); return []

TIPOS_ROUPA = {
    '1':'T-Shirt', '2':'Polo', '3':'Camisa', '4':'Sweatshirt',
    '5':'Casaco', '6':'Calças', '7':'Calções', '8':'Jeans',
    '9':'Calçado', '0':'Outro',
}

def nome_produto_valido(nome):
    """Verifica se o nome do produto é válido para guardar."""
    if not nome: return False
    # Remove referências técnicas
    limpo = re.sub(r'\([0-9/\s]{3,}\)', '', nome).strip()
    limpo = re.sub(r'^[0-9/\s]+$', '', limpo).strip()
    if len(limpo) < 3: return False
    invalidos = {'item','none','não identificado','nao identificado','produto','desconhecido','unknown'}
    if limpo.lower() in invalidos: return False
    if re.match(r'^[\d\s/\-]+$', limpo): return False
    return True

def limpar_nome_wishlist(nome):
    """Limpa nome para display — remove referências técnicas."""
    if not nome: return 'Artigo'
    limpo = re.sub(r'\s*\([0-9/\s]{4,}\)', '', nome).strip()
    limpo = re.sub(r'\s+[0-9]{8,}', '', limpo).strip()
    return limpo if len(limpo) > 2 else nome

def wishlist_duplicado(usuario_id, link=None, referencia=None):
    """Verifica se o item já existe na wishlist."""
    try:
        if link:
            r = db.session.execute(text(
                "SELECT id FROM wishlist WHERE usuario_id=:u AND link=:l AND comprado=FALSE"),
                {'u':usuario_id,'l':link}).fetchone()
            if r: return True
        if referencia and len(referencia) > 5:
            r = db.session.execute(text(
                "SELECT id FROM wishlist WHERE usuario_id=:u AND descricao LIKE :r AND comprado=FALSE"),
                {'u':usuario_id,'r':f'%{referencia}%'}).fetchone()
            if r: return True
    except Exception: pass
    return False

CAT_DISPLAY = {
    'roupa':      '👗 Roupa',
    'calcado':    '👟 Calçado',
    'maquilagem': '💄 Perfumes & Beleza',
    'acessorios': '👜 Acessórios',
    'casa':       '🏠 Casa',
    'tecnologia': '📱 Tecnologia',
    'desporto':   '🏃 Desporto',
    'outros':     '🛒 Outros',
}

def processar_wishlist(phone_raw, usuario, texto):
    t = texto.lower()

    # ── VER WISHLIST ──
    if any(p in t for p in ['ver','lista','mostrar','wishlist','desejos']) and \
       not any(p in t for p in ['quero','gostei','adorei']):

        filtro_cat = next((c for c in WISHLIST_CATS if c in t), None)
        filtro_est = next((e for e in ESTACOES if e in t), None)
        filtro_marca = next((m for m in ['zara','nike','adidas','hm','pull','bershka','shein','mango','primor'] if m in t), None)
        m_max = re.search(r'abaixo\s+(\d+)', t)
        m_min = re.search(r'acima\s+(\d+)', t)

        try:
            q = "SELECT id, descricao, preco, link, marca, categoria, estacao FROM wishlist WHERE usuario_id=:id AND comprado=FALSE"
            params = {'id': usuario.id}
            if filtro_cat: q += " AND categoria=:cat"; params['cat'] = filtro_cat
            if filtro_est: q += " AND estacao=:est"; params['est'] = filtro_est
            if filtro_marca: q += " AND LOWER(marca) LIKE :marc"; params['marc'] = f"%{filtro_marca}%"
            if m_max: q += " AND (preco IS NULL OR preco <= :max)"; params['max'] = float(m_max.group(1))
            if m_min: q += " AND preco >= :min"; params['min'] = float(m_min.group(1))
            q += " ORDER BY categoria, preco DESC NULLS LAST"
            rows = db.session.execute(text(q), params).fetchall()

            if not rows:
                enviar_mensagem(phone_raw, "Wishlist vazia 🛍️\nManda foto de etiqueta, link ou 'quero [produto]'!"); return

            total = sum(r[2] for r in rows if r[2])

            # Agrupa por categoria
            por_cat = {}
            mapa = {}
            n = 1
            for r in rows:
                cat = r[5] or 'outros'
                if cat not in por_cat: por_cat[cat] = []
                por_cat[cat].append((n, r))
                mapa[str(n)] = r[0]
                n += 1

            msg = f"🛍️ Wishlist — {len(rows)} itens\n"

            for cat, items in por_cat.items():
                cat_label = CAT_DISPLAY.get(cat, f'🛒 {cat.capitalize()}')
                msg += f"\n{cat_label}\n"
                for num, r in items:
                    nome = limpar_nome_wishlist(r[1])
                    # Extrai referência do nome original se existir (ex: 4174/845/712)
                    ref_match = re.search(r'\(([0-9/]{4,})\)', r[1] or '')
                    ref_txt = f" ({ref_match.group(1)})" if ref_match else ""
                    marca_txt = f" ({r[4]})" if r[4] and r[4].lower() not in nome.lower() else ""
                    preco_txt = f" — {r[2]:.0f}€" if r[2] else ""
                    est_txt   = f"  ·  {r[6]}" if r[6] else ""
                    msg += f"{num}. {nome}{marca_txt}{ref_txt}{preco_txt}{est_txt}\n"
                    # Mostra URL curto clicável se tiver link
                    if r[3]:
                        try:
                            import urllib.parse
                            parsed = urllib.parse.urlparse(r[3])
                            netloc = parsed.netloc.replace('www.','')
                            path = parsed.path
                            url_curto = f"{netloc}{path[:40]}{'...' if len(path)>40 else ''}"
                            msg += f"   ↗ {r[3]}\n"
                        except Exception:
                            msg += f"   ↗ {r[3]}\n"

            if total: msg += f"\n💰 Total: {total:.0f}€\n"
            msg += "\ncomprar 3  ·  remover 3  ·  link 3"

            phone_n = phone_raw.replace('@lid','').replace('@c.us','').split('@')[0]
            set_estado(phone_n, 'wishlist_mapa', {'mapa': mapa})
            enviar_mensagem(phone_raw, msg)
        except Exception as e:
            log.error(f"wishlist ver: {e}"); enviar_mensagem(phone_raw, "Erro 😕")
        return

    # ── VER LINK ──
    m_link = re.search(r'(?:link|ver)\s+(\d+)', t)
    if m_link:
        phone_n = phone_raw.replace('@lid','').replace('@c.us','').split('@')[0]
        estado_w, dados_w = get_estado(phone_n)
        mapa = dados_w.get('mapa', {}) if estado_w == 'wishlist_mapa' else {}
        item_id = mapa.get(m_link.group(1))
        if item_id:
            try:
                r = db.session.execute(text("SELECT descricao, link FROM wishlist WHERE id=:id"), {'id':item_id}).fetchone()
                if r and r[1]:
                    enviar_mensagem(phone_raw, f"🔗 {limpar_nome_wishlist(r[0])}\n{r[1]}")
                else:
                    enviar_mensagem(phone_raw, "Sem link guardado para este item 😕")
            except Exception: enviar_mensagem(phone_raw, "Erro 😕")
        else:
            enviar_mensagem(phone_raw, "Diz 'wishlist' primeiro para ver a lista numerada.")
        return

    # Link direto
    link_match = re.search(r'https?://\S+', texto)
    if link_match:
        link_raw = link_match.group(0).rstrip(')')
        link = limpar_url(link_raw)
        enviar_mensagem(phone_raw, "🔍 A analisar o produto e a comparar preços nas lojas...")
        try:
            import requests as req
            loja_link = extrair_nome_loja(link)
            # Tenta extrair nome do produto do path do URL (muito mais fiável)
            nome_from_path = extrair_nome_produto_url(link)
            nome_prod = None; preco_prod = None

            # Busca info via Tavily usando o nome extraído do URL
            query_tavily = nome_from_path if nome_from_path else link
            r2 = req.post("https://api.tavily.com/search",
                json={'api_key':TAVILY_API_KEY,'query':query_tavily,'max_results':2,'search_depth':'basic'},
                timeout=15)
            if r2.status_code == 200:
                results = r2.json().get('results', [])
                if results:
                    titulo = results[0].get('title', '')
                    # Limpa título: remove loja e sufixos
                    nome_limpo = re.sub(r'\s*[\|\-]\s*.*$', '', titulo).strip()
                    nome_limpo = re.sub(r'\s*[-–]\s*(' + loja_link + r'|Portugal|PT|Shop).*', '', nome_limpo, flags=re.IGNORECASE).strip()
                    if len(nome_limpo) > 5: nome_prod = nome_limpo[:70]
                    conteudo = results[0].get('content', '')
                    pm = re.search(r'(\d{1,3}[.,]\d{2})\s*€|€\s*(\d{1,3}[.,]\d{2})', conteudo)
                    if pm:
                        try: preco_prod = float((pm.group(1) or pm.group(2)).replace(',','.'))
                        except: pass

            # Fallback: usa nome extraído do path do URL
            if not nome_prod or len(nome_prod) < 4:
                nome_prod = nome_from_path or f"Produto {loja_link}"

            # Compara preços noutras lojas
            lojas = comparar_precos_tavily(nome_prod, loja_link)
            lojas_alt = [l for l in lojas if l['loja'].lower() != loja_link.lower()]

        except Exception as e:
            log.error(f"link wishlist: {e}"); nome_prod = 'Produto'; preco_prod = None; lojas_alt = []; loja_link = '?'

        cat, cat_e = detetar_cat_wishlist(nome_prod + ' ' + texto)
        est = detetar_estacao(nome_prod + ' ' + texto)
        preco_f = preco_prod

        try:
            db.session.execute(text("INSERT INTO wishlist (usuario_id,descricao,preco,link,categoria,estacao) VALUES (:u,:d,:p,:l,:c,:e)"),
                {'u':usuario.id,'d':nome_prod,'p':preco_f,'l':link,'c':cat,'e':est})
            db.session.commit()
        except Exception: db.session.rollback()

        preco_txt = f" — {preco_f:.2f}€" if preco_f else ""
        msg = f"🛍️ {cat_e} {nome_prod}{preco_txt} ({loja_link})\n"
        msg += "Guardado na wishlist! ✅\n"
        if lojas_alt:
            msg += "\n💰 Também disponível em:\n"
            for i, l in enumerate(lojas_alt[:3]):
                medalha = ['🥇','🥈','🥉'][i]
                diff = ""
                if preco_f and l['preco'] < preco_f:
                    diff = f" (-{preco_f-l['preco']:.2f}€)"
                elif preco_f and l['preco'] > preco_f:
                    diff = f" (+{l['preco']-preco_f:.2f}€)"
                msg += f"{medalha} {l['loja']}: {l['preco']:.2f}€{diff}\n"
            if lojas_alt and preco_f and lojas_alt[0]['preco'] < preco_f:
                msg += f"\n✅ Mais barato na {lojas_alt[0]['loja']}! ({lojas_alt[0]['preco']:.2f}€)"
        else:
            msg += "Não encontrei comparações 😕"
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

def ler_qr_code_pyzbar(image_bytes):
    """Tenta ler QR code com múltiplas abordagens."""
    try:
        from PIL import Image, ImageEnhance, ImageFilter
        import io

        img_orig = Image.open(io.BytesIO(image_bytes)).convert('RGB')

        # Prepara várias versões da imagem para aumentar chances de leitura
        versoes = []

        # 1. Original em grayscale
        versoes.append(img_orig.convert('L'))

        # 2. Aumentada 2x + grayscale
        w, h = img_orig.size
        img_grande = img_orig.resize((w*2, h*2), Image.LANCZOS)
        versoes.append(img_grande.convert('L'))

        # 3. Alto contraste
        img_contraste = ImageEnhance.Contrast(img_orig).enhance(2.0)
        versoes.append(img_contraste.convert('L'))

        # 4. Nitidez aumentada
        img_sharp = ImageEnhance.Sharpness(img_orig).enhance(3.0)
        versoes.append(img_sharp.convert('L'))

        # Tenta zxing-cpp em cada versão
        try:
            import zxingcpp
            for img_v in versoes:
                try:
                    results = zxingcpp.read_barcodes(img_v)
                    for r in results:
                        if r.text:
                            log.info(f"QR zxingcpp: {r.text}")
                            return r.text
                except Exception:
                    pass
        except ImportError:
            pass

        # Tenta pyzbar como fallback
        try:
            from pyzbar.pyzbar import decode
            for img_v in versoes:
                try:
                    decoded = decode(img_v)
                    for d in decoded:
                        result = d.data.decode('utf-8')
                        log.info(f"QR pyzbar: {result}")
                        return result
                except Exception:
                    pass
        except Exception:
            pass

    except Exception as e:
        log.error(f"qr: {e}")
    return None

def ler_etiqueta_wishlist(phone_raw, usuario, url, mimetype):
    """Lê etiqueta com pyzbar para QR e Groq para texto."""
    try:
        from groq import Groq
        c = baixar_media(url)
        if not c: return False

        # 1. Tenta ler QR code com pyzbar (muito mais fiável que vision)
        qr_url = ler_qr_code_pyzbar(c)

        # 2. Groq lê o texto da etiqueta (marca, produto, preço, referência)
        mt = 'image/png' if 'png' in mimetype else 'image/jpeg'
        img_b64 = base64.b64encode(c).decode()
        resp = Groq(api_key=GROQ_API_KEY).chat.completions.create(
            model='meta-llama/llama-4-scout-17b-16e-instruct', max_tokens=200,
            messages=[{'role':'user','content':[
                {'type':'image_url','image_url':{'url':f'data:{mt};base64,{img_b64}'}},
                {'type':'text','text':(
                    'Esta e uma etiqueta de roupa/produto. '
                    'Le toda a informacao visivel: marca, nome do produto, preco, referencia/codigo. '
                    'Responde APENAS em JSON sem markdown: '
                    '{"marca":"MARCA","produto":"NOME_PRODUTO","preco":NUMERO_OU_NULL,'
                    '"referencia":"REF_OU_NULL","tipo":"roupa/calcado/acessorio/maquilagem/outro"} '
                    'Se nao for etiqueta: {"erro":"nao_etiqueta"}'
                )}
            ]}])
        txt = re.sub(r'```json|```', '', resp.choices[0].message.content.strip()).strip()
        try: dados = json.loads(txt)
        except: dados = {}
        if 'erro' in dados: return False

        desc  = dados.get('produto') or 'Item'
        marca = dados.get('marca')
        preco = dados.get('preco')
        ref   = dados.get('referencia')
        tipo  = dados.get('tipo', 'outro')
        if ref and str(ref) not in ['null','None','']: desc = f"{desc} ({ref})"
        cat, cat_e = detetar_cat_wishlist(desc + ' ' + tipo)
        est = detetar_estacao(desc)
        lojas = []; link_f = qr_url; preco_online = None

        # Segue QR redirect para obter URL real e nome
        if qr_url:
            try:
                import requests as req
                r_redirect = req.get(qr_url, allow_redirects=True, timeout=10,
                    headers={'User-Agent': 'Mozilla/5.0'})
                url_final = r_redirect.url
                link_f = url_final if url_final != qr_url else qr_url
                nome_from_url = extrair_nome_produto_url(link_f)
                if nome_from_url and len(nome_from_url) > 3:
                    desc = nome_from_url
            except Exception as e:
                log.error(f"qr redirect: {e}")
                link_f = qr_url

        # Categoria por defeito para marcas de roupa conhecidas
        MARCAS_ROUPA = {'zara','pull&bear','pullandbear','bershka','stradivarius','hm','h&m',
                        'mango','shein','primark','lefties','springfield','nike','adidas','jd','snipes'}
        if cat == 'outros' and marca and marca.lower().replace('&','').replace(' ','') in {m.replace('&','').replace(' ','') for m in MARCAS_ROUPA}:
            cat = 'roupa'; cat_e = '👗'

        # Verifica duplicados
        if wishlist_duplicado(usuario.id, link=link_f, referencia=ref):
            enviar_mensagem(phone_raw, "⚠️ Este artigo já está na wishlist!\nDiz 'wishlist' para ver a lista."); return True

        preco_f = preco

        # Valida o nome — se não for válido, pergunta ao utilizador
        if not nome_produto_valido(desc):
            phone_n = phone_raw.replace('@lid','').replace('@c.us','').split('@')[0]
            set_estado(phone_n, 'wishlist_tipo_pendente', {
                'marca': marca, 'preco': preco_f, 'link': link_f,
                'cat': cat, 'est': est, 'ref': ref or ''
            })
            marca_txt = f" ({marca})" if marca else ""
            preco_txt = f" — {preco_f:.2f}€" if preco_f else ""
            msg = (f"🏷️{marca_txt}{preco_txt}\n"
                   f"Não consegui identificar o artigo. O que é?\n\n"
                   f"1 T-Shirt   2 Polo   3 Camisa\n"
                   f"4 Sweatshirt   5 Casaco   6 Calças\n"
                   f"7 Calções   8 Jeans   9 Calçado   0 Outro")
            enviar_mensagem(phone_raw, msg); return True

        try:
            db.session.execute(text(
                "INSERT INTO wishlist (usuario_id,descricao,preco,link,marca,categoria,estacao) VALUES (:u,:d,:p,:l,:m,:c,:e)"),
                {'u':usuario.id,'d':desc,'p':preco_f,'l':link_f,'m':marca,'c':cat,'e':est})
            db.session.commit()
        except Exception as e:
            log.error(f"wishlist etiqueta bd: {e}"); db.session.rollback()

        preco_txt = f" — {preco_f:.2f}€" if preco_f else ""
        marca_txt = f" ({marca})" if marca else ""
        qr_txt = " 📱 QR lido!" if qr_url else ""
        msg = f"🛍️ Guardado!{qr_txt}\n{desc}{marca_txt}{preco_txt}"
        if link_f: msg += f"\n🔗 {link_f}"
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
    # Remove por número: "remove wishlist 1"
    m_num = re.search(r'(?:remove|apaga|tira).*wishlist.*?(\d+)|wishlist.*?(\d+).*(?:remove|apaga)', t)
    if m_num:
        num = int(m_num.group(1) or m_num.group(2))
        try:
            rows = db.session.execute(text(
                "SELECT id, descricao FROM wishlist WHERE usuario_id=:u AND comprado=FALSE ORDER BY criado_em DESC"),
                {'u':usuario.id}).fetchall()
            if 1 <= num <= len(rows):
                item_id, item_desc = rows[num-1]
                db.session.execute(text("DELETE FROM wishlist WHERE id=:id"), {'id':item_id})
                db.session.commit()
                enviar_mensagem(phone_raw, f"🗑️ '{item_desc}' removido!")
            else:
                enviar_mensagem(phone_raw, f"Não encontrei o item {num} 🤔 Diz 'wishlist' para ver a lista.")
        except Exception as e:
            log.error(f"wishlist remove num: {e}"); enviar_mensagem(phone_raw, "Erro 😕")
        return

    palavras = [w for w in re.findall(r'[a-zà-ú]+', t) if w not in {'remove','apaga','da','wishlist','o','a','tira'}]
    if not palavras:
        enviar_mensagem(phone_raw, "O que queres remover? Ex: 'remove wishlist 1' ou 'remove da wishlist o vestido'"); return
    chave = palavras[0]
    try:
        r = db.session.execute(text(
            "DELETE FROM wishlist WHERE usuario_id=:u AND LOWER(descricao) LIKE :c RETURNING descricao"),
            {'u':usuario.id,'c':f'%{chave}%'}).fetchone()
        db.session.commit()
        if r: enviar_mensagem(phone_raw, f"🗑️ '{r[0]}' removido!")
        else: enviar_mensagem(phone_raw, f"Não encontrei '{chave}' 🤔")
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
        piadas = [
            f"😏 Vamos ver se {pessoa} paga ou se vais ter de cobrar...",
            f"🍀 Esperemos que {pessoa} tenha dinheiro!",
            f"👀 {pessoa} ficou a dever-te {valor_cada:.2f}€. Boa sorte!",
            f"😂 Já foste, {pessoa} nunca vai pagar mas tá registado!",
        ]
        import random
        enviar_mensagem(phone_raw,
            f"✂️ {desc}: {valor:.2f}€\n"
            f"💸 {pessoa} deve-te {valor_cada:.2f}€\n"
            f"{random.choice(piadas)}")
    except Exception as e:
        log.error(f"splitting: {e}"); enviar_mensagem(phone_raw, "Erro 😕")

def processar_dividas(phone_raw, usuario, texto):
    """Regista dívidas — 'devo X ao Y' ou 'Y deve-me X'"""
    t = texto.lower()
    valor = extrair_valor(texto)
    if valor == 0:
        enviar_mensagem(phone_raw, "Qual é o valor? Ex: 'devo 20€ ao João'"); return

    # Deteta direção da dívida
    eu_devo = any(p in t for p in ['devo','tenho que pagar','preciso pagar'])
    m_pessoa = re.search(r'(?:ao|à|a|ao|para o|para a)\s+([A-Za-zÀ-ú]+)', texto, re.IGNORECASE)
    if not m_pessoa:
        m_pessoa = re.search(r'([A-Za-zÀ-ú]+)\s+deve', texto, re.IGNORECASE)
        eu_devo = False

    pessoa = m_pessoa.group(1).capitalize() if m_pessoa else 'Alguem'

    stop = {'devo','deve','me','ao','à','a','euros','euro','pagar','tenho','preciso','para'}
    palavras = [w for w in texto.split() if not re.match(r'[0-9€,.]',w) and len(w)>2 and w.lower() not in stop]
    desc = ' '.join(palavras[:2]).capitalize() if palavras else 'Dívida'

    try:
        if eu_devo:
            # Regista como splitting onde eu devo
            db.session.execute(text("INSERT INTO splitting (usuario_id,descricao,valor_total,valor_cada,pessoa,pago) VALUES (:u,:d,:vt,:vc,:p,FALSE)"),
                {'u':usuario.id,'d':f'[EU DEVO] {desc}','vt':valor,'vc':valor,'p':pessoa})
            db.session.commit()
            enviar_mensagem(phone_raw, f"📝 Anotado! Deves {valor:.2f}€ ao {pessoa}\nNão te esqueças de pagar! 😬")
        else:
            db.session.execute(text("INSERT INTO splitting (usuario_id,descricao,valor_total,valor_cada,pessoa,pago) VALUES (:u,:d,:vt,:vc,:p,FALSE)"),
                {'u':usuario.id,'d':desc,'vt':valor,'vc':valor,'p':pessoa})
            db.session.commit()
            enviar_mensagem(phone_raw, f"📝 Anotado! {pessoa} deve-te {valor:.2f}€\n😏 Vamos ver quando aparece com o dinheiro...")
    except Exception as e:
        log.error(f"dividas: {e}"); enviar_mensagem(phone_raw, "Erro 😕")

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

# ─── MODO CASAL ───────────────────────────────────────────────
def enviar_resumo_casal(phone_raw, usuario):
    parceiro_phone = get_parceiro_phone(usuario.phone)
    parceiro = Usuario.query.filter_by(phone=parceiro_phone).first() if parceiro_phone else None
    meu_nome = NOMES_CASAL.get(usuario.phone, 'Tu')
    par_nome = NOMES_CASAL.get(parceiro_phone, 'Parceiro')
    mes = agora().month; ano = agora().year

    def gastos_mes_u(u):
        return db.session.query(db.func.sum(Despesa.valor)).filter(
            Despesa.usuario_id==u.id, db.extract('month',Despesa.data)==mes,
            db.extract('year',Despesa.data)==ano,
            ~Despesa.descricao.like('[conjunta]%')).scalar() or 0

    meus_gastos = gastos_mes_u(usuario)
    disp_m, p_m = calcular_disponivel(usuario)
    minha_poupa = p_m.get('poupanca', 0)

    conj_total = 0
    for u in ([usuario, parceiro] if parceiro else [usuario]):
        conj_total += db.session.query(db.func.sum(Despesa.valor)).filter(
            Despesa.usuario_id==u.id, db.extract('month',Despesa.data)==mes,
            db.extract('year',Despesa.data)==ano,
            Despesa.descricao.like('[conjunta]%')).scalar() or 0

    msg = f"💑 Resumo do Casal — {['Jan','Fev','Mar','Abr','Mai','Jun','Jul','Ago','Set','Out','Nov','Dez'][mes-1]}\n\n"
    msg += f"👤 {meu_nome}\n  💸 Gastos: {meus_gastos:.0f}€  💚 Poupança: {minha_poupa:.0f}€\n"

    if parceiro:
        par_gastos = gastos_mes_u(parceiro)
        disp_p, p_p = calcular_disponivel(parceiro)
        par_poupa = p_p.get('poupanca', 0)
        msg += f"\n👤 {par_nome}\n  💸 Gastos: {par_gastos:.0f}€  💚 Poupança: {par_poupa:.0f}€\n"
        msg += f"\n💑 Conjunta: {conj_total:.0f}€ de 100€\n"
        msg += f"💎 Poupança total do casal: {minha_poupa+par_poupa:.0f}€\n"
        if minha_poupa > par_poupa: msg += f"\n🏆 {meu_nome} está a poupar mais!"
        elif par_poupa > minha_poupa: msg += f"\n🏆 {par_nome} está a poupar mais!"
        else: msg += f"\n🤝 Empate na poupança!"

    try:
        objs = db.session.execute(text(
            "SELECT descricao, valor_objetivo, valor_atual FROM objetivos_poupanca WHERE usuario_id=:id AND descricao LIKE '[casal]%' AND concluido=FALSE"),
            {'id':usuario.id}).fetchall()
        if objs:
            msg += "\n\n🎯 Objetivos do casal:\n"
            for o in objs:
                nome_obj = o[0].replace('[casal] ','')
                pct = round(o[2]/o[1]*100) if o[1] else 0
                barra = '█'*int(pct/10) + '░'*(10-int(pct/10))
                msg += f"  {nome_obj}: {barra} {pct}%\n  {o[2]:.0f}€ / {o[1]:.0f}€\n"
    except Exception: pass

    enviar_mensagem(phone_raw, msg)

def enviar_comparacao_casal(phone_raw, usuario):
    parceiro_phone = get_parceiro_phone(usuario.phone)
    parceiro = Usuario.query.filter_by(phone=parceiro_phone).first() if parceiro_phone else None
    if not parceiro: enviar_mensagem(phone_raw, "Ainda não tens parceiro ligado 😅"); return

    meu_nome = NOMES_CASAL.get(usuario.phone, 'Tu')
    par_nome = NOMES_CASAL.get(parceiro_phone, 'Parceiro')
    mes = agora().month; ano = agora().year

    def g(u): return db.session.query(db.func.sum(Despesa.valor)).filter(
        Despesa.usuario_id==u.id, db.extract('month',Despesa.data)==mes,
        db.extract('year',Despesa.data)==ano, ~Despesa.descricao.like('[conjunta]%')).scalar() or 0

    meus = g(usuario); deles = g(parceiro); diff = abs(meus-deles)
    msg = f"⚔️ Batalha do mês!\n\n"
    msg += f"{'🏆' if meus<=deles else '😅'} {meu_nome}: {meus:.0f}€\n"
    msg += f"{'🏆' if deles<=meus else '😅'} {par_nome}: {deles:.0f}€\n\n"
    if meus < deles: msg += f"Estás a ganhar! {par_nome} gastou {diff:.0f}€ a mais 👑"
    elif deles < meus: msg += f"{par_nome} está a ganhar! Gastaste {diff:.0f}€ a mais 😬"
    else: msg += f"Empate perfeito! 🤝"
    enviar_mensagem(phone_raw, msg)

def processar_objetivo_casal(phone_raw, usuario, texto):
    parceiro_phone = get_parceiro_phone(usuario.phone)
    parceiro = Usuario.query.filter_by(phone=parceiro_phone).first() if parceiro_phone else None
    meu_nome = NOMES_CASAL.get(usuario.phone, 'Tu')
    par_nome = NOMES_CASAL.get(parceiro_phone, 'Parceiro')

    m = re.search(r'(\d+(?:[.,]\d+)?)', texto)
    valor = float(m.group(1).replace(',','.')) if m else None
    m_desc = re.search(r'para\s+(.+?)(?:\s+em\s+|\s+até|\s*$)', texto, re.IGNORECASE)
    desc = m_desc.group(1).strip()[:50] if m_desc else 'objetivo conjunto'

    if not valor: enviar_mensagem(phone_raw, "Diz o valor! Ex: 'objetivo casal 1000€ para férias'"); return

    desc_casal = f"[casal] {desc}"
    try:
        for u in ([usuario, parceiro] if parceiro else [usuario]):
            db.session.execute(text(
                "INSERT INTO objetivos_poupanca (usuario_id,descricao,valor_objetivo,valor_atual) VALUES (:u,:d,:v,0)"),
                {'u':u.id,'d':desc_casal,'v':valor})
        db.session.commit()
    except Exception as e:
        log.error(f"obj casal: {e}"); db.session.rollback()

    enviar_mensagem(phone_raw, f"💑 Objetivo conjunto criado!\n🎯 {desc.capitalize()}: {valor:.0f}€\n{'👫 '+meu_nome+' + '+par_nome+' a trabalhar para isso! 💪' if parceiro else ''}")
    if parceiro:
        notificar_parceiro(usuario.phone, f"💑 {meu_nome} criou um objetivo conjunto!\n🎯 {desc.capitalize()}: {valor:.0f}€\nVamos a isso! 💪")

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
    stop = {'quero','poupar','para','objetivo','meta','de','poupanca','poupança','euros','euro'}
    palavras = [w for w in texto.split() if not re.match(r'[0-9€,.]',w) and len(w)>2 and w.lower() not in stop]
    desc = ' '.join(palavras[:3]).capitalize() if palavras else 'Objetivo'

    # Pergunta quando quer atingir
    phone = phone_raw.replace('@lid','').replace('@c.us','').split('@')[0]
    set_estado(phone, 'objetivo_data', {'valor': valor, 'desc': desc})
    enviar_mensagem(phone_raw,
        f"🎯 Boa! {desc}: {valor:.0f}€\n\n📅 Quando queres atingir este objetivo?\nEx: 'dezembro', 'março', 'daqui a 3 meses'")

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
def filtrar_resposta(txt, phone=None):
    """Filtra respostas da IA conforme o perfil do utilizador."""
    perfil = get_perfil(phone or '')
    if perfil['genero'] == 'F':
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
        perfil = get_perfil(usuario.phone or '')

        if perfil['genero'] == 'M':
            sys = f"""És o Zé das Finanças, assistente financeiro criado pelo tuga27 para o {perfil['nome']}.
ESTILO: português europeu informal, direto e brincalhão entre amigos.
Usa: {perfil['expressoes']}. Trata no masculino sempre.
Max 2 linhas + 1 emoji. NUNCA inventes preços de gasolina.
CONTEXTO: Modo {m['nome']} | {disp:.0f}€ disponível | Salário: {usuario.salario_liquido or 'não registado'}€
SABER: BK=Burger King, Mac=McDonald's, conti=Continente, PD=Pingo Doce, JD=JD Sports"""
        else:
            sys = f"""És o Zé das Finanças, assistente financeiro criado pelo tuga27 para a {perfil['nome']}.
REGRAS: fala SEMPRE no feminino. PROIBIDO: {perfil['proibido']}.
Usa: {perfil['expressoes']}. Português europeu informal e fofo. Max 2 linhas + 1 emoji.
NUNCA inventes preços de gasolina.
CONTEXTO: Modo {m['nome']} | {disp:.0f}€ disponível | Salário: {usuario.salario_liquido or 'não registado'}€
SABER: BK=Burger King, Mac=McDonald's, conti=Continente, PD=Pingo Doce, JD=JD Sports"""

        resp = Groq(api_key=GROQ_API_KEY).chat.completions.create(
            model='llama-3.3-70b-versatile',
            messages=[{'role':'system','content':sys},{'role':'user','content':texto}],
            max_tokens=150)
        return filtrar_resposta(resp.choices[0].message.content, usuario.phone)
    except Exception as e:
        log.error(f'IA: {e}'); return "Não percebi 🤔 Diz 'ajuda'!"

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
                    if pct >= 70:
                        enviar_mensagem(f"{u.phone}@lid", f"⚠️ A meio do mês e já usaste {pct:.0f}% do orçamento! Vai com calma 💪")
                    elif pct >= 50:
                        enviar_mensagem(f"{u.phone}@lid", f"📊 Meio do mês — usaste {pct:.0f}% do orçamento. No bom caminho! 👍")

                    # Aviso padrão combustível
                    mes = hoje.month; ano = hoje.year
                    mes_ant = mes-1 if mes>1 else 12; ano_ant = ano if mes>1 else ano-1
                    gas_atual = gastos_cat_mes(u, 'combustivel', mes, ano)
                    gas_ant   = gastos_cat_mes(u, 'combustivel', mes_ant, ano_ant)
                    if gas_ant > 0 and gas_atual > gas_ant * 1.2:
                        diferenca = gas_atual - gas_ant
                        enviar_mensagem(f"{u.phone}@lid",
                            f"⛽ Este mês já gastaste {diferenca:.0f}€ a mais em combustível que o mês passado!\n"
                            f"Este mês: {gas_atual:.0f}€ | Mês passado: {gas_ant:.0f}€\n"
                            f"Andaste mais ou os preços subiram? 🤔")

def aviso_uma_semana_salario():
    with app.app_context():
        hoje = agora()
        dia_pag = dia_pagamento_mes(hoje.year, hoje.month)
        dias_falta = (dia_pag.date() - hoje.date()).days
        if dias_falta == 7 and hoje.hour == 10:
            for u in Usuario.query.all():
                if u.phone and u.salario_liquido:
                    disp, _ = calcular_disponivel(u)
                    por_dia = round(disp/7, 2) if disp > 0 else 0
                    if por_dia > 0:
                        enviar_mensagem(f"{u.phone}@lid",
                            f"⏰ Última semana antes do salário!\n"
                            f"💳 Tens {disp:.2f}€ — dá {por_dia:.2f}€/dia até sexta 💪")
                    else:
                        enviar_mensagem(f"{u.phone}@lid",
                            f"😅 Última semana e o orçamento já está no limite!\nForça, falta pouco! 💪")

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
scheduler.add_job(aviso_uma_semana_salario,   'cron', hour=10, minute=0)
scheduler.add_job(aviso_fim_mes_wishlist,     'cron', hour=11, minute=0)
scheduler.add_job(resumo_semanal,             'cron', hour=9,  minute=30, day_of_week='mon')
scheduler.add_job(verificar_despesas_futuras, 'cron', hour=8,  minute=0)
scheduler.add_job(verificar_aniversarios,     'cron', hour=9,  minute=0)
scheduler.add_job(wrapped_anual,              'cron', hour=20, minute=0)
scheduler.start()
log.info("Ze das Financas v7 iniciado")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
