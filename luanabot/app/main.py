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
BASE_COMBUSTIVEL = 50

# Modos de poupança — % do que sobra depois de fixos e fundo
MODOS_POUPANCA = {
    'maximo':      {'gastar_pct': 0.20, 'poupar_pct': 0.80, 'emoji': '💎', 'nome': 'Maxima',
                    'desc': 'Modo monge 🧘 Poupes o maximo, gastas so o essencial. Para quem quer chegar a algum lado rapido.'},
    'equilibrado': {'gastar_pct': 0.30, 'poupar_pct': 0.70, 'emoji': '⚖️', 'nome': 'Equilibrado',
                    'desc': 'O meio termo perfeito. Poupas bem e ainda tens margem para viver a vida.'},
    'relaxado':    {'gastar_pct': 0.45, 'poupar_pct': 0.55, 'emoji': '😎', 'nome': 'Relaxado',
                    'desc': 'Vives a vida mas ainda poupas. Sem stress, sem culpa.'},
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

# ─── ABREVIAÇÕES ─────────────────────────────────────────────
LOJAS = {
    'bk':'fastfood','burger king':'fastfood','mac':'fastfood','mc':'fastfood',
    'mcd':'fastfood','mcdonald':'fastfood',"mcdonald's":'fastfood','mcdonalds':'fastfood',
    'kfc':'fastfood','sbx':'fastfood','starbucks':'fastfood','sub':'fastfood','subway':'fastfood',
    'telepizza':'fastfood','dominos':'fastfood',
    'zen':'restaurante','zen-sushi':'restaurante','zen sushi':'restaurante','sushi':'restaurante',
    'alcochete':'restaurante','cafe':'restaurante','café':'restaurante',
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
    'ps':'PlayStation','xb':'Xbox','wrt':'Worten','rp':'Radio Popular',
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
    'restaurantes':'restaurante','sushi':'restaurante','pizza':'restaurante',
    'kebab':'restaurante','jantar':'restaurante',
    'roupas':'roupa','sapatilhas':'roupa','tenis':'roupa','sapatos':'roupa','sneakers':'roupa',
    'tech':'tecnologia','eletronica':'tecnologia','gaming':'tecnologia',
    'gasolina':'combustivel','gasoleo':'combustivel','posto':'combustivel',
    'agua':'gota','bebida':'gota','bebidas':'gota',
    'farmacia':'saude','medico':'saude','dentista':'saude',
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

# ─── BD: TABELAS EXTRA ───────────────────────────────────────
def criar_tabelas():
    sqls = [
        """CREATE TABLE IF NOT EXISTS aprendizagem (
            chave VARCHAR(100) PRIMARY KEY, categoria VARCHAR(50) NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS estado_utilizador (
            phone VARCHAR(50) PRIMARY KEY, estado VARCHAR(100),
            dados TEXT, atualizado TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS badges (
            id SERIAL PRIMARY KEY, usuario_id INTEGER,
            badge VARCHAR(100), obtido_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS pessoas_gastos (
            id SERIAL PRIMARY KEY, usuario_id INTEGER,
            despesa_id INTEGER, pessoa VARCHAR(100))""",
        """CREATE TABLE IF NOT EXISTS reserva_emergencia (
            usuario_id INTEGER PRIMARY KEY, saldo FLOAT DEFAULT 0,
            atualizado TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS modo_poupanca (
            usuario_id INTEGER PRIMARY KEY, modo VARCHAR(20) DEFAULT 'equilibrado')""",
        """CREATE TABLE IF NOT EXISTS wishlist (
            id SERIAL PRIMARY KEY, usuario_id INTEGER,
            descricao VARCHAR(200), preco FLOAT,
            link VARCHAR(500), foto_url VARCHAR(500),
            marca VARCHAR(100), categoria VARCHAR(50),
            estacao VARCHAR(20), comprado BOOLEAN DEFAULT FALSE,
            criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS km_combustivel (
            id SERIAL PRIMARY KEY, usuario_id INTEGER,
            km INTEGER, litros FLOAT, valor FLOAT,
            consumo_l100 FLOAT, custo_km FLOAT,
            data TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS splitting (
            id SERIAL PRIMARY KEY, usuario_id INTEGER,
            descricao VARCHAR(200), valor_total FLOAT,
            valor_cada FLOAT, pessoa VARCHAR(100),
            pago BOOLEAN DEFAULT FALSE,
            criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS aniversarios (
            id SERIAL PRIMARY KEY, usuario_id INTEGER,
            nome VARCHAR(100), data_aniv DATE)""",
    ]
    for sql in sqls:
        try:
            db.session.execute(text(sql)); db.session.commit()
        except Exception as e:
            log.warning(f"tabela: {e}"); db.session.rollback()

# ─── HELPERS BD ──────────────────────────────────────────────
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
        return r[0] if r else 0.0
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
    fixos = {
        'carro': 350, 'ordem': 20, 'conjunta': 50,
        'unhas': 50 if mes <= 9 else 25,
        'combustivel': BASE_COMBUSTIVEL,
    }
    # Adiciona despesas futuras deste mês aos fixos
    if despesas_futuras_valor > 0:
        fixos['despesas_mes'] = despesas_futuras_valor
    total_fixos = sum(fixos.values())
    fundo = round(salario * FUNDO_PCT, 2)
    sobra = salario - total_fixos - fundo
    sobra = max(sobra, 0)
    m = MODOS_POUPANCA.get(modo, MODOS_POUPANCA[MODO_DEFAULT])
    gastar   = round(sobra * m['gastar_pct'], 2)
    poupanca = round(sobra * m['poupar_pct'], 2)
    return {
        **fixos, 'total_fixos': total_fixos, 'salario': salario,
        'fundo': fundo, 'sobra': sobra,
        'gastar': gastar, 'poupanca': poupanca,
        'modo': modo, 'subsidio': mes in [6, 11],
    }

def calcular_disponivel(usuario):
    mes = agora().month; ano = agora().year
    modo = get_modo(usuario.id)
    futuras_mes = db.session.query(db.func.sum(DespesaFutura.valor_reserva_mensal)).filter(
        DespesaFutura.usuario_id==usuario.id, DespesaFutura.pago==False).scalar() or 0
    p = calcular_plano(usuario.salario_liquido or 0, modo, futuras_mes)
    gastos = db.session.query(db.func.sum(Despesa.valor)).filter(
        Despesa.usuario_id==usuario.id,
        db.extract('month',Despesa.data)==mes, db.extract('year',Despesa.data)==ano,
        ~Despesa.descricao.like('[conjunta]%'),
        ~Despesa.descricao.like('[reserva]%'),
    ).scalar() or 0
    return p['gastar'] - gastos, p

def gastos_categoria_mes(usuario, categoria, mes, ano):
    return db.session.query(db.func.sum(Despesa.valor)).filter(
        Despesa.usuario_id==usuario.id, Despesa.categoria==categoria,
        db.extract('month',Despesa.data)==mes, db.extract('year',Despesa.data)==ano
    ).scalar() or 0

# ─── BADGES ──────────────────────────────────────────────────
BADGES = {
    'primeiro_gasto':  ('🎉', 'Primeira Despesa!', 'Registaste o primeiro gasto'),
    'mes_dentro':      ('🏆', 'Mes Perfeito', 'Dentro do orcamento o mes todo'),
    'poupadora':       ('💎', 'Poupadora', 'Poupaste mais de 200€ num mes'),
    'mestre_poupanca': ('👑', 'Mestre da Poupanca', '3 meses seguidos dentro do orcamento'),
    'sem_fastfood':    ('🥗', 'Clean Eater', '2 semanas sem fast food'),
    'meta_gasolina':   ('⛽', 'Combustivel Ok', 'Ficaste abaixo dos 50€ em gasolina'),
    'reserva_100':     ('🛡️', 'Reserva Solida', 'Reserva de emergencia acima de 100€'),
    'reserva_500':     ('🏰', 'Fortaleza', 'Reserva de emergencia acima de 500€'),
}

def verificar_badges(usuario, phone_raw):
    try:
        badges_ok = {r[0] for r in db.session.execute(
            text("SELECT badge FROM badges WHERE usuario_id=:id"), {'id':usuario.id}).fetchall()}
        novos = []
        mes=agora().month; ano=agora().year
        total_gastos = db.session.query(db.func.count(Despesa.id)).filter_by(usuario_id=usuario.id).scalar() or 0
        if total_gastos == 1 and 'primeiro_gasto' not in badges_ok:
            novos.append('primeiro_gasto')
        reserva = get_reserva(usuario.id)
        if reserva >= 100 and 'reserva_100' not in badges_ok: novos.append('reserva_100')
        if reserva >= 500 and 'reserva_500' not in badges_ok: novos.append('reserva_500')
        disp, p = calcular_disponivel(usuario)
        if usuario.salario_liquido and p['poupanca'] >= 200 and 'poupadora' not in badges_ok:
            novos.append('poupadora')
        gas = gastos_categoria_mes(usuario,'combustivel',mes,ano)
        if gas > 0 and gas <= BASE_COMBUSTIVEL and agora().day >= 20 and 'meta_gasolina' not in badges_ok:
            novos.append('meta_gasolina')
        for badge in novos:
            db.session.execute(text("INSERT INTO badges (usuario_id,badge) VALUES (:id,:b)"),
                               {'id':usuario.id,'b':badge})
            db.session.commit()
            e, nome, desc = BADGES[badge]
            enviar_mensagem(phone_raw, f"🎖️ CONQUISTA!\n{e} {nome}\n{desc}\n\nEstas a mandar! 🙌")
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
            mime = media.get('mimetype',''); url = media.get('url','')
            if 'audio' in mime or 'ogg' in mime:
                transcrito = transcrever_audio(url)
                if transcrito:
                    enviar_mensagem(phone_raw, f'🎤 Percebi: "{transcrito}"'); texto = transcrito
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
                        # Tenta ler como etiqueta de roupa
                        u_temp = Usuario.query.filter_by(phone=phone).first()
                        if u_temp and ler_etiqueta_wishlist(phone_raw, u_temp, url, mime):
                            return jsonify({'status':'ok'})
                        enviar_mensagem(phone_raw, f'📸 Li: {resultado}'); texto = resultado
                else:
                    # Tenta km (odómetro)
                    u_temp = Usuario.query.filter_by(phone=phone).first()
                    if u_temp and ler_foto_km(phone_raw, u_temp, url, mime):
                        return jsonify({'status':'ok'})
                    # Tenta etiqueta
                    if u_temp and ler_etiqueta_wishlist(phone_raw, u_temp, url, mime):
                        return jsonify({'status':'ok'})
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
    return jsonify({'status':'ok','bot':'Ze das Financas v6'})

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
            model='whisper-large-v3',
            file=(os.path.basename(fname), open(fname,'rb').read()),
            language='pt',
            prompt='Gastos em euros. Lojas: Continente, BK, McDonald, Galp, Zara.')
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
                {'type':'text','text':'Le este documento. Se recibo salario: "X,XX euros SALARIO". Se talao compra: "X,XX euros LOJA". Ex: "1327,92 euros SALARIO" ou "25,50 euros Continente". Se nao deres: erro'}
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

# ─── PROCESSAR TEXTO ─────────────────────────────────────────
def processar_texto(phone_raw, phone, texto):
    usuario = Usuario.query.filter_by(phone=phone).first()
    if not usuario:
        usuario = Usuario(phone=phone, nome='Luana')
        db.session.add(usuario); db.session.commit()

    t = texto.lower().strip()

    # ── ESTADOS ──
    estado, dados_estado = get_estado(phone)

    if estado == 'escolher_modo':
        if any(p in t for p in ['1','maximo','máximo','monge','poupar maximo','poupar ao maximo']):
            set_modo(usuario.id, 'maximo'); limpar_estado(phone)
            enviar_mensagem(phone_raw, "💎 Modo Maxima ativado! Vamos a isso, modo monge ON 🧘\nManda o salario quando receberes!")
        elif any(p in t for p in ['2','equilibrado','meio','meio termo']):
            set_modo(usuario.id, 'equilibrado'); limpar_estado(phone)
            enviar_mensagem(phone_raw, "⚖️ Modo Equilibrado ativado! O melhor dos dois mundos 😊\nManda o salario quando receberes!")
        elif any(p in t for p in ['3','relaxado','relax','sem stress']):
            set_modo(usuario.id, 'relaxado'); limpar_estado(phone)
            enviar_mensagem(phone_raw, "😎 Modo Relaxado ativado! Vives a vida mas poupas sempre alguma coisa 👌\nManda o salario quando receberes!")
        else:
            enviar_mensagem(phone_raw, "Escolhe:\n1 - Maxima\n2 - Equilibrado\n3 - Relaxado")
        return

    if estado == 'confirmar_salario':
        if any(p in t for p in ['sim','yes','correto','certo','exato','é isso','e isso']):
            valor = dados_estado.get('valor', 0); limpar_estado(phone)
            processar_receita(phone_raw, usuario, f"recebi {valor}")
        elif tem_numero(texto):
            limpar_estado(phone); processar_receita(phone_raw, usuario, texto)
        else:
            limpar_estado(phone); enviar_mensagem(phone_raw, "Ok, diz tu: recebi X euros 💰")
        return

    if estado == 'aguardar_recibo':
        if any(p in t for p in ['sim','yes','quero','manda','envia']):
            limpar_estado(phone); enviar_mensagem(phone_raw, "Manda o PDF ou foto do recibo 📄")
        elif any(p in t for p in ['nao','não','valor','digo']) or tem_numero(texto):
            limpar_estado(phone)
            if tem_numero(texto): processar_receita(phone_raw, usuario, texto)
            else: enviar_mensagem(phone_raw, "Ok, diz: recebi X euros 💰")
        return

    # ── MUDAR MODO ──
    if any(p in t for p in ['muda modo','mudar modo','modo maximo','modo equilibrado','modo relaxado','alterar modo','trocar modo']):
        if 'maximo' in t or 'máximo' in t:
            set_modo(usuario.id, 'maximo')
            enviar_mensagem(phone_raw, "💎 Modo Maxima ativado! Modo monge ON 🧘"); return
        elif 'equilibrado' in t:
            set_modo(usuario.id, 'equilibrado')
            enviar_mensagem(phone_raw, "⚖️ Modo Equilibrado ativado!"); return
        elif 'relaxado' in t:
            set_modo(usuario.id, 'relaxado')
            enviar_mensagem(phone_raw, "😎 Modo Relaxado ativado!"); return
        else:
            mostrar_modos(phone_raw); return

    # ── RESERVA ──
    if any(p in t for p in ['gastei da reserva','usei da reserva','tirei da reserva','gastei da emergencia']):
        processar_gasto_reserva(phone_raw, usuario, texto); return
    if any(p in t for p in ['reserva','emergencia','quanto tenho na reserva','quanto na reserva']):
        if any(p in t for p in ['quanto','tenho','ver','saldo']):
            r = get_reserva(usuario.id)
            enviar_mensagem(phone_raw, f"🛡️ Reserva de emergencia: {r:.2f}€\n\nPara usar: 'gastei 30 da reserva'"); return

    # ── ANIVERSÁRIOS ──
    if any(p in t for p in ['aniversario','aniversário','faz anos']):
        processar_aniversario(phone_raw, usuario, texto); return

    # ── APRENDER ──
    m = re.search(r'aprende que (.+?) (?:é|e|sao|são) (?:da categoria |categoria )?(\w+)', t)
    if m:
        chave = m.group(1).strip().strip('"\''); cat = normalizar_categoria(m.group(2))
        if cat in CATEGORIAS_VALIDAS:
            enviar_mensagem(phone_raw, f"🧠 Aprendido! '{chave}' = {cat.capitalize()} p/ sempre 😎") if guardar_aprendida(chave, cat) else enviar_mensagem(phone_raw, "Ops 😕")
        else:
            enviar_mensagem(phone_raw, f"Nao conheço essa categoria 🤔\nUsa: {', '.join(CATEGORIAS_VALIDAS)}")
        return

    # ── CORRIGIR ──
    m2 = re.search(r'(?:corrige|corrigir|muda|mudar|afinal|isso é|isso e|o ultimo|o último) (?:para |o )*(\w+)', t)
    if m2 and any(p in t for p in ['corrige','corrigir','afinal','isso é','isso e','ultimo','último']):
        cat = normalizar_categoria(m2.group(1))
        if cat in CATEGORIAS_VALIDAS:
            corrigir_ultimo(phone_raw, usuario, cat); return

    # ── CANCELAR DESPESA FUTURA ──
    if any(p in t for p in ['afinal nao','afinal não','cancela','cancelo','remove a despesa','apaga despesa']):
        processar_despesa_futura(phone_raw, usuario, texto); return

    # ── CRIADOR ──
    if any(p in t for p in ['quem criou','quem te fez','quem te criou','criador','quem te programou']):
        enviar_mensagem(phone_raw, "Fui criado pelo tuga27 🚀\nO mesmo genio por tras do Zeflix (plataforma de streaming de filmes e series) e agora do teu gestor financeiro 😎"); return

    if any(p in t for p in ['ajuda','help','/start','comandos']):
        enviar_ajuda(phone_raw); return

    if t in ['ola','olá','oi','boas','hey','hello'] or 'bom dia' in t or 'boa tarde' in t or 'boa noite' in t:
        enviar_boas_vindas(phone_raw, usuario, phone); return

    if 'estou teso' in t or 'tou teso' in t or 'sem dinheiro' in t or 'liso' in t:
        modo_teso(phone_raw, usuario); return

    if any(p in t for p in ['gasolina mais barata','posto mais barato','gasolina barata','valor gasolina','preco gasolina','preço gasolina']) or \
       (any(p in t for p in ['barreiro','moita','seixal','almada','montijo','palmela']) and any(p in t for p in ['gasolina','combustivel','posto','mais barata','mais barato','preco','preço','valor','barata','barato'])):
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

    if any(p in t for p in ['score','conquistas','badges']):
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

    if any(p in t for p in ['quanto gastei com','gastei com']):
        resumo_por_pessoa(phone_raw, usuario, texto); return

    # WISHLIST
    if any(p in t for p in ['wishlist','lista de desejos','quero comprar','quero isto','gostei disto','ver wishlist']):
        processar_wishlist(phone_raw, usuario, texto); return

    if any(p in t for p in ['comprei o','comprei a','ja comprei','já comprei']) and not eh_gasto(texto):
        marcar_wishlist_comprado(phone_raw, usuario, texto); return

    if any(p in t for p in ['remove da wishlist','apaga da wishlist','remove o','apaga o']) and 'wishlist' in t:
        remover_wishlist(phone_raw, usuario, texto); return

    # SPLITTING
    if any(p in t for p in ['dividi','dividir','a meias','split','partilhei']) and tem_numero(texto):
        processar_splitting(phone_raw, usuario, texto); return

    if any(p in t for p in ['splits','divididos','o que devo','o que me devem']):
        ver_splits(phone_raw, usuario); return

    # MODO DISCRETO
    if any(p in t for p in ['limpa conversa','apaga mensagens','modo discreto','limpar chat']):
        modo_discreto(phone_raw); return

    if tem_numero(texto) and eh_gasto(texto):
        processar_despesa(phone_raw, usuario, texto); return

    enviar_mensagem(phone_raw, perguntar_ia(texto, usuario))

# ─── GASTO RESERVA ───────────────────────────────────────────
def processar_gasto_reserva(phone_raw, usuario, texto):
    valor = extrair_valor(texto)
    if valor == 0:
        enviar_mensagem(phone_raw, "Quanto gastaste da reserva? Ex: gastei 30 da reserva"); return
    reserva_atual = get_reserva(usuario.id)
    if valor > reserva_atual:
        enviar_mensagem(phone_raw, f"⚠️ So tens {reserva_atual:.2f}€ na reserva! Tens a certeza?")
        return
    nova_reserva = reserva_atual - valor
    set_reserva(usuario.id, nova_reserva)
    despesa = Despesa(usuario_id=usuario.id, valor=valor, categoria='outros',
                      descricao=f'[reserva] {texto[:80]}', data=agora().replace(tzinfo=None))
    db.session.add(despesa); db.session.commit()
    enviar_mensagem(phone_raw, f"🛡️ Reserva usada: {valor:.2f}€\nReserva atual: {nova_reserva:.2f}€\n\nEspero que tenhas resolvido o que precisavas! 💪")

# ─── ANIVERSÁRIOS ────────────────────────────────────────────
def processar_aniversario(phone_raw, usuario, texto):
    t = texto.lower()

    # Ver lista
    if any(p in t for p in ['ver','lista','quais','mostrar','aniversarios','aniversários']):
        try:
            rows = db.session.execute(text(
                "SELECT nome, data_aniv FROM aniversarios WHERE usuario_id=:id ORDER BY EXTRACT(month FROM data_aniv), EXTRACT(day FROM data_aniv)"),
                {'id': usuario.id}).fetchall()
            if not rows:
                enviar_mensagem(phone_raw, "Ainda nao tens aniversarios guardados 🎂\nAdiciona: 'aniversario da Ana dia 15 marco'"); return
            meses = ['Jan','Fev','Mar','Abr','Mai','Jun','Jul','Ago','Set','Out','Nov','Dez']
            hoje = agora()
            msg = "🎂 Aniversarios:\n\n"
            mes_atual = None
            for r in rows:
                if r[1].month != mes_atual:
                    mes_atual = r[1].month
                    msg += f"── {meses[r[1].month-1]} ──\n"
                dias_falta = (r[1].replace(year=hoje.year) - hoje.date()).days
                if dias_falta < 0: dias_falta += 365
                alerta = " 🔥" if dias_falta <= 5 else (" ⚠️" if dias_falta <= 14 else "")
                msg += f"• {r[0]} — dia {r[1].day}{alerta}\n"
            enviar_mensagem(phone_raw, msg)
        except Exception as e:
            log.error(f"anivs: {e}"); enviar_mensagem(phone_raw, "Erro 😕")
        return

    # Adicionar — varios formatos:
    # "aniversario da Ana dia 15 marco"
    # "aniversario Ana 15/3"
    # "Ana faz anos dia 15 de marco"
    meses_map = {
        'janeiro':1,'fevereiro':2,'marco':3,'março':3,'abril':4,'maio':5,'junho':6,
        'julho':7,'agosto':8,'setembro':9,'outubro':10,'novembro':11,'dezembro':12,
        'jan':1,'fev':2,'mar':3,'abr':4,'mai':5,'jun':6,'jul':7,'ago':8,'set':9,'out':10,'nov':11,'dez':12
    }

    # Tenta formato DD/MM ou DD-MM
    m_data = re.search(r'(\d{1,2})[/\-](\d{1,2})', texto)
    # Tenta formato "dia X de/em mes"
    m_dia_mes = re.search(r'dia (\d{1,2})(?:\s+de)?\s+([a-záàâãéêíóôõúç]+)', t)
    # Tenta formato "X de mes"
    m_x_mes = re.search(r'(\d{1,2})\s+(?:de\s+)?([a-záàâãéêíóôõúç]+)', t)

    dia = mes_num = None

    if m_data:
        dia = int(m_data.group(1)); mes_num = int(m_data.group(2))
    elif m_dia_mes:
        dia = int(m_dia_mes.group(1)); mes_num = meses_map.get(m_dia_mes.group(2))
    elif m_x_mes:
        dia = int(m_x_mes.group(1)); mes_num = meses_map.get(m_x_mes.group(2))

    # Extrai nome
    stop = {'aniversario','aniversário','faz','anos','dia','de','do','da','em','o','a','os','as','para','e'}
    palavras_nome = [w for w in re.findall(r'[A-Za-zÀ-ú]+', texto)
                     if w.lower() not in stop and not w.lower() in meses_map and len(w) > 1]
    # Remove numeros escritos
    nome = palavras_nome[0].capitalize() if palavras_nome else None

    if nome and dia and mes_num and 1 <= dia <= 31 and 1 <= mes_num <= 12:
        try:
            data = f"2000-{mes_num:02d}-{min(dia,28):02d}"
            db.session.execute(text(
                "INSERT INTO aniversarios (usuario_id,nome,data_aniv) VALUES (:u,:n,:d) ON CONFLICT DO NOTHING"),
                {'u': usuario.id, 'n': nome, 'd': data})
            db.session.commit()
            meses_pt = ['Jan','Fev','Mar','Abr','Mai','Jun','Jul','Ago','Set','Out','Nov','Dez']
            enviar_mensagem(phone_raw, f"🎂 Anotado! {nome} faz anos a {dia} de {meses_pt[mes_num-1]}\nVou avisar-te antes! 🎉")
        except Exception as e:
            log.error(f"aniv add: {e}"); enviar_mensagem(phone_raw, "Erro ao guardar 😕")
    else:
        enviar_mensagem(phone_raw, "Nao percebi bem 🤔 Tenta assim:\n• 'aniversario da Ana dia 15 marco'\n• 'aniversario Ana 15/3'\n• 'aniversarios' para ver a lista")

# ─── PROCESSAR DESPESA ───────────────────────────────────────
def processar_despesa(phone_raw, usuario, texto):
    valor = extrair_valor(texto)
    if valor == 0:
        enviar_mensagem(phone_raw, "Nao percebi o valor 🤔 Diz tipo: 25 conti"); return

    categoria, emoji, nome_loja = categorizar(texto)
    na_conjunta = 'conjunta' in texto.lower()

    pessoa = None
    m_pessoa = re.search(r'com (?:a |o |as |os )?([A-Za-zÀ-ú]+)', texto, re.IGNORECASE)
    if m_pessoa and m_pessoa.group(1).lower() not in ['conjunta','ruben','a','o']:
        pessoa = m_pessoa.group(1).capitalize()

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
    total_cat     = gastos_categoria_mes(usuario, categoria, mes, ano)
    total_cat_ant = gastos_categoria_mes(usuario, categoria, mes_ant, ano_ant)
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
        if total_cat > BASE_COMBUSTIVEL*1.5: extra = f'\n⛽ {total_cat:.0f}€ em gasolina, bem acima dos {BASE_COMBUSTIVEL}€ base!'
        elif total_cat > BASE_COMBUSTIVEL: extra = f'\n⛽ Passaste a base de {BASE_COMBUSTIVEL}€ em gasolina'
    elif agora().weekday() in [4,5] and agora().hour>=19 and categoria in ['restaurante','fastfood']:
        extra = '\n🍻 Fim de semana a noite, la vem o costume!'
    elif total_cat_ant>0 and total_cat>total_cat_ant*1.3:
        extra = f'\n⚠️ Ja gastaste mais em {categoria} que o mes passado todo!'
    elif total_cat_ant>0 and total_cat<total_cat_ant*0.6:
        extra = f'\n✅ Muito menos em {categoria} que o mes passado. Bora!'

    aviso = ''
    if pct_usado >= 100: aviso = f'\n\n🔴 Passaste o orcamento! {abs(disp):.0f}€ a mais.'
    elif pct_usado >= 80: aviso = f'\n\n🔔 Ja usaste {pct_usado:.0f}% do orcamento!'

    conjunta_txt = ' (conjunta 💑)' if na_conjunta else ''
    pessoa_txt   = f' (com {pessoa})' if pessoa else ''
    modo = get_modo(usuario.id)
    m_info = MODOS_POUPANCA[modo]
    msg = (f"{emoji} Bora! {nome_loja} {valor:.2f}€{conjunta_txt}{pessoa_txt}\n"
           f"{categoria.capitalize()}: {total_cat:.2f}€ este mes{extra}{aviso}\n"
           f"💚 Disponivel: {disp:.2f}€ | {m_info['emoji']} Modo {m_info['nome']}")
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

    msg  = f"💰 Boa, recebeste {salario:.2f}€! {m['emoji']} Modo {m['nome']}\n\n📋 Plano:\n"
    msg += f"🏠 Fixos: {p['total_fixos']:.0f}€\n"
    msg += f"   🚗 {p['carro']:.0f} | 💼 {p['ordem']:.0f} | 💅 {p['unhas']:.0f} | 💑 {p['conjunta']:.0f} | ⛽ {p['combustivel']:.0f}"
    if total_fut > 0:
        msg += f" | 📅 {total_fut:.0f} (despesas mes)"
        for d in futuras: msg += f"\n   {d.descricao}: {d.valor_reserva_mensal:.0f}€"
    msg += f"\n🛡️ Fundo emergencia: {p['fundo']:.2f}€ (Revolut!)\n"
    msg += f"💳 Para gastar: {p['gastar']:.0f}€\n"
    msg += f"💎 Poupanca: {p['poupanca']:.0f}€"
    if p['subsidio']: msg += "\n\n🌴 Mes de subsidio! Mais margem 😉"

    # Mes do aniversário dela — novembro
    if agora().month == 11:
        msg += "\n\n🎂 Este mes e o teu aniversario!! Ja separei 100€ so para ti — compra algo que gostes muito! 🎁"
    enviar_mensagem(phone_raw, msg)

    # Verifica se ha poupanca anterior nao usada para adicionar a reserva
    reserva_atual = get_reserva(usuario.id)
    if reserva_atual > 0:
        enviar_mensagem(phone_raw, f"🛡️ Reserva de emergencia: {reserva_atual:.2f}€ — continua a crescer! 💪")

    # Resumo mes anterior
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
        # Verifica se sobrou dinheiro para reserva
        verificar_sobra_mes(phone_raw, usuario, mes_ant, ano_ant)
    else:
        enviar_mensagem(phone_raw, "💡 Primeiro mes! A partir de agora vou guardar tudo 💪")

    phone = phone_raw.replace('@lid','').replace('@c.us','').split('@')[0]
    set_estado(phone, 'fecho_feito', {'mes':agora().month,'ano':agora().year})

    # Aviso de aniversários este mês
    try:
        mes_atual = agora().month
        nomes_mes = ['Janeiro','Fevereiro','Marco','Abril','Maio','Junho','Julho','Agosto','Setembro','Outubro','Novembro','Dezembro']
        rows = db.session.execute(text(
            "SELECT nome, data_aniv FROM aniversarios WHERE usuario_id=:id AND EXTRACT(month FROM data_aniv)=:m ORDER BY EXTRACT(day FROM data_aniv)"),
            {'id': usuario.id, 'mes': mes_atual, 'm': mes_atual}).fetchall()
        if rows:
            msg_aniv = f"🎂 Este mes ({nomes_mes[mes_atual-1]}) tens aniversarios:\n"
            for r in rows:
                dias_falta = r[1].day - agora().day
                if dias_falta < 0:
                    msg_aniv += f"• {r[0]} — dia {r[1].day} (ja passou este mes)\n"
                elif dias_falta == 0:
                    msg_aniv += f"• {r[0]} — HOJE! 🎉\n"
                elif dias_falta <= 5:
                    msg_aniv += f"• {r[0]} — dia {r[1].day} (daqui a {dias_falta} dias! ⚠️)\n"
                else:
                    msg_aniv += f"• {r[0]} — dia {r[1].day}\n"
            msg_aniv += "\nQueres ver a lista completa? Diz 'aniversarios' 🎁"
            enviar_mensagem(phone_raw, msg_aniv)
    except Exception as e:
        log.error(f"anivs plano: {e}")

def verificar_sobra_mes(phone_raw, usuario, mes, ano):
    """No fim do mes verifica se sobrou dinheiro da poupanca prevista."""
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

    sobrou_gastar = p['gastar'] - gastos_mes
    if sobrou_gastar > 5:
        nova_reserva = get_reserva(usuario.id) + sobrou_gastar
        set_reserva(usuario.id, nova_reserva)
        enviar_mensagem(phone_raw,
            f"🎉 Sobraram {sobrou_gastar:.2f}€ do orcamento do mes passado!\n"
            f"Ja meti na tua reserva de emergencia 💪\n"
            f"🛡️ Reserva total: {nova_reserva:.2f}€")

# ─── QUANTO TENHO ────────────────────────────────────────────
def enviar_quanto_tenho(phone_raw, usuario):
    disp, p = calcular_disponivel(usuario)
    reserva = get_reserva(usuario.id)
    modo = get_modo(usuario.id)
    m = MODOS_POUPANCA[modo]
    if disp < 0:
        msg = f"😬 Passaste o orcamento em {abs(disp):.2f}€!\n🛡️ Reserva emergencia: {reserva:.2f}€"
    elif disp < 20:
        msg = f"💸 So tens {disp:.2f}€ para gastar. Aperta o cinto!\n🛡️ Reserva: {reserva:.2f}€ (nao mexas sem precisar!)"
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
        f"💑 Conjunta (jantares, cinema, lanches):\n💰 Tua parte: 50€\n🛒 Gastaste: {gasto:.2f}€\n💚 Resta: {max(resta,0):.2f}€ {estado_txt}\n\nPara marcar: 'jantar 30 na conjunta'")

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

    modo = get_modo(usuario.id)
    futuras = DespesaFutura.query.filter(DespesaFutura.usuario_id==usuario.id, DespesaFutura.pago==False).all()
    total_fut = sum(d.valor_reserva_mensal for d in futuras)
    p = calcular_plano(receita or 0, modo, total_fut)
    disp = p['gastar'] - gp
    nomes = ['Janeiro','Fevereiro','Marco','Abril','Maio','Junho','Julho','Agosto','Setembro','Outubro','Novembro','Dezembro']

    msg = f"📊 {nomes[mes-1]}\n\n💰 Receita: {receita:.0f}€\n🛒 Gastos: {gp:.2f}€\n💑 Conjunta: {gc:.2f}€\n💚 Disponivel: {disp:.2f}€\n💎 Poupanca prevista: {p['poupanca']:.0f}€\n\n📈 Categorias:"
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

# ─── SCORE ───────────────────────────────────────────────────
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
        badges_lista = db.session.execute(
            text("SELECT badge FROM badges WHERE usuario_id=:id ORDER BY obtido_em DESC"), {'id':usuario.id}).fetchall()
        badges_txt = ('\n\n🎖️ Conquistas:\n' + '\n'.join(f"{BADGES[r[0]][0]} {BADGES[r[0]][1]}" for r in badges_lista if r[0] in BADGES)) if badges_lista else ''
    except Exception:
        badges_txt = ''
    modo = get_modo(usuario.id)
    m = MODOS_POUPANCA[modo]
    enviar_mensagem(phone_raw, f"⭐ Score: {score}/10 | {m['emoji']} Modo {m['nome']}\n{txt}\n🛡️ Reserva: {reserva:.2f}€{badges_txt}")

# ─── MODO TESO ───────────────────────────────────────────────
def modo_teso(phone_raw, usuario):
    disp, _ = calcular_disponivel(usuario)
    reserva = get_reserva(usuario.id)
    dias = dias_para_salario()
    msg = (f"😅 Modo teso ativado!\n\n💚 Para gastar: {disp:.2f}€\n🛡️ Reserva: {reserva:.2f}€ (so em emergencias!)\n📅 ~{dias} dias p/ o salario\n\n"
           f"Dicas:\n🍳 Cozinha em casa\n🚶 Anda a pe\n🛒 So o essencial\n☕ Cafe de maquina\n💪 Consegues!")
    enviar_mensagem(phone_raw, msg)

# ─── CORRIGIR ────────────────────────────────────────────────
def corrigir_ultimo(phone_raw, usuario, nova_cat):
    ultima = Despesa.query.filter_by(usuario_id=usuario.id).order_by(Despesa.id.desc()).first()
    if not ultima:
        enviar_mensagem(phone_raw, "Nao tenho nenhum gasto p/ corrigir 🤔"); return
    cat_antiga = ultima.categoria; ultima.categoria = nova_cat; db.session.commit()
    desc = ultima.descricao.replace('[conjunta] ','').replace('[reserva] ','').lower()
    palavras = [w for w in re.findall(r"[a-zà-ú&']+", desc)
                if len(w)>1 and w not in ['gastei','paguei','comprei','almocei','euros','euro','no','na','em']]
    aprendido = ''
    if palavras:
        chave = palavras[-1]
        if guardar_aprendida(chave, nova_cat):
            aprendido = f"\n🧠 Aprendi: '{chave}' = {nova_cat.capitalize()} p/ sempre!"
    enviar_mensagem(phone_raw, f"{EMOJI_CAT.get(nova_cat,'💳')} Corrigido! {cat_antiga.capitalize()} → {nova_cat.capitalize()}{aprendido}")

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
            SELECT d.valor, d.categoria, d.descricao, d.data
            FROM despesas d JOIN pessoas_gastos pg ON d.id=pg.despesa_id
            WHERE pg.usuario_id=:uid AND LOWER(pg.pessoa)=:p ORDER BY d.data DESC LIMIT 20
        """), {'uid':usuario.id,'p':nome.lower()}).fetchall()
        if not rows:
            enviar_mensagem(phone_raw, f"Nao encontrei gastos com {nome} 🤔"); return
        total = sum(r[0] for r in rows)
        msg = f"💸 Gastos com {nome}:\n"
        for r in rows[:5]: msg += f"• {r[0]:.0f}€ — {r[2][:25]}\n"
        if len(rows)>5: msg += f"... e mais {len(rows)-5}\n"
        msg += f"\n💰 Total: {total:.2f}€"
        enviar_mensagem(phone_raw, msg)
    except Exception as e:
        log.error(f"resumo pessoa: {e}"); enviar_mensagem(phone_raw, "Erro 😕")

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
        maps_query = p['nome'].replace(' ', '+')
        maps_link = f"https://maps.google.com/?q={maps_query}"
        msg += f"{medalhas[i]} {p['preco']:.3f}€/L — {p['nome'][:28]}{marca}\n📍 {maps_link}\n\n"
    msg += "💡 Dados DGEG, hoje!"
    enviar_mensagem(phone_raw, msg)

# ─── DESPESA FUTURA ──────────────────────────────────────────
def processar_despesa_futura(phone_raw, usuario, texto):
    t = texto.lower()
    if any(p in t for p in ['afinal','cancela','cancelo','remove','apaga','nao tenho','não tenho']):
        futuras = DespesaFutura.query.filter(DespesaFutura.usuario_id==usuario.id, DespesaFutura.pago==False).all()
        palavras_chave = [w for w in re.findall(r'[a-zà-ú]+', t)
                         if w not in ['afinal','nao','não','tenho','cancela','cancelo','remove','apaga','que','vem','mes','mês','o','a']]
        if palavras_chave:
            chave = palavras_chave[0]
            removidas = [f for f in futuras if chave in f.descricao.lower()]
            if removidas:
                for f in removidas: db.session.delete(f)
                db.session.commit()
                enviar_mensagem(phone_raw, f"🗑️ Removido! '{removidas[0].descricao}' apagado 👍")
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
                    and w.lower() not in ['mes','mês','que','vem','tenho','este','esse','proximo','próximo','afinal']]
        desc = ' '.join(palavras[:2]).capitalize() if palavras else 'Despesa futura'

    meses = 2 if ('2 meses' in t or 'dois meses' in t) else (3 if '3 meses' in t else 1)
    dia_match = re.search(r'dia (\d{1,2})', t)
    dia = int(dia_match.group(1)) if dia_match else None

    hoje = agora().replace(tzinfo=None)
    if dia:
        mes_alvo = hoje.month+meses; ano_alvo = hoje.year
        while mes_alvo>12: mes_alvo-=12; ano_alvo+=1
        try: data_prev = hoje.replace(year=ano_alvo, month=mes_alvo, day=min(dia,28))
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

# ─── SIMULAR ─────────────────────────────────────────────────
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
    elif pct <= 100: resp = f"🟠 Tecnicamente sim mas ficas quase a zero ({disp-valor:.0f}€). Cuidado!"
    else:            resp = f"🔴 Nao da. Faltam {valor-disp:.0f}€. Deixa p/ o mes que vem 😬"
    enviar_mensagem(phone_raw, resp)

# ─── BOAS VINDAS / AJUDA / MODOS ─────────────────────────────
def mostrar_modos(phone_raw):
    msg = "Escolhe o teu modo de poupanca:\n\n"
    for i, (k, m) in enumerate(MODOS_POUPANCA.items(), 1):
        msg += f"{i}. {m['emoji']} {m['nome']}\n{m['desc']}\n\n"
    msg += "Responde 1, 2 ou 3 — podes mudar a qualquer momento 😊"
    enviar_mensagem(phone_raw, msg)

def enviar_boas_vindas(phone_raw, usuario=None, phone=None):
    modo = get_modo(usuario.id) if usuario else MODO_DEFAULT
    tem_salario = usuario and usuario.salario_liquido
    dias = dias_para_salario()

    if not tem_salario:
        if phone:
            set_estado(phone, 'escolher_modo', {})
        msg = (f"Ola Luana! 👋 Eu sou o Ze das Financas!\n"
               f"Fui criado pelo tuga27 especialmente para ti 💸\n\n"
               f"A minha missao? Ajudar-te a poupar, controlar os teus gastos "
               f"e nunca mais ficares a zeros antes do salario 😅\n\n"
               f"Antes de comecarmos, diz-me como queres gerir o teu dinheiro:\n\n"
               f"1. 💎 Poupanca Maxima\nPoupes o maximo, gastas so o essencial. Modo monge 🧘\n\n"
               f"2. ⚖️ Equilibrado\nPoupas bem mas ainda tens margem para viver a vida 😊\n\n"
               f"3. 😎 Relaxado\nVives a vida mas ainda poupas alguma coisa. Sem stress.\n\n"
               f"Podes mudar a qualquer momento! Escolhe 1, 2 ou 3 👇\n\n"
               f"(Daqui a {dias} dia{'s' if dias!=1 else ''} vamos juntos com o salario 🚀)")
    else:
        disp, p = calcular_disponivel(usuario)
        m = MODOS_POUPANCA[modo]
        msg = (f"Ola de volta! 👋 {m['emoji']}\n"
               f"Tens {disp:.0f}€ para gastar | Poupanca: {p['poupanca']:.0f}€\n"
               f"🛡️ Reserva: {get_reserva(usuario.id):.2f}€\n\n"
               f"Sugestoes de melhorias? Manda! Estou sempre a evoluir 🚀\n"
               f"Diz 'ajuda' se precisares 😎")
    enviar_mensagem(phone_raw, msg)

def enviar_ajuda(phone_raw):
    enviar_mensagem(phone_raw, """😎 Ze das Financas — o que sei:

💸 Gastos:
• 15 bk | 25 conti | 50 galp
• jantar 30 na conjunta
• gastei 30 da reserva
• foto talao | audio | PDF recibo

🛍️ Wishlist:
• [foto etiqueta] → guarda automatico
• quero sapatilhas nike 89€
• wishlist → ver tudo
• comprei o vestido
• remove da wishlist o vestido

✂️ Splitting:
• dividi 60€ jantar com o Ruben
• splits → ver pendentes

📊 Consultas:
• resumo | plano | quanto tenho
• quanto tenho na conjunta
• score | resumo anterior

🎂 Aniversarios:
• aniversario da Ana dia 15 marco

🎯 Planear:
• posso comprar X?
• mes que vem dentista 40€ dia 15
• afinal nao tenho dentista

⛽ Gasolina mais barata no barreiro
🆘 Estou teso
🔒 Limpa conversa (modo discreto)
🔄 Muda modo (maximo/equilibrado/relaxado)
🧠 Aprende que X e roupa | corrige para roupa

💡 Sugestoes? Manda sempre! 🚀""")


# ─── IA ──────────────────────────────────────────────────────
def filtrar_resposta(texto):
    """Filtra respostas da IA para garantir linguagem feminina correta."""
    # Palavras masculinas a substituir
    substituicoes = [
        (r'\bbrother\b', 'querida'),
        (r'\birmao\b', 'querida'),
        (r'\birmão\b', 'querida'),
        (r'\bmano\b', 'linda'),
        (r'\bchefe\b', 'querida'),
        (r'\bbro\b', 'querida'),
        (r'\bamigo\b', 'querida'),
        (r'\brapaz\b', 'rapariga'),
        (r'\bcara\b', 'querida'),
        (r'\bparceiro\b', 'parceira'),
        (r'\bcampeao\b', 'campea'),
        (r'\bcampeão\b', 'campeã'),
        (r'\bparabens cara\b', 'parabens querida'),
    ]
    resultado = texto
    for padrao, substituto in substituicoes:
        resultado = re.sub(padrao, substituto, resultado, flags=re.IGNORECASE)
    return resultado

def perguntar_ia(texto, usuario):
    try:
        from groq import Groq
        disp, _ = calcular_disponivel(usuario)
        modo = get_modo(usuario.id)
        m = MODOS_POUPANCA[modo]
        sys = f"""Es o Ze das Financas, assistente financeiro portugues criado pelo tuga27 para a Luana.

REGRAS ABSOLUTAS — NAO PODES QUEBRAR NENHUMA:
1. Fala SEMPRE no feminino: "gastaste", "tens", "podes", "estás", "foste"
2. PROIBIDO usar: brother, irmao, irmão, mano, chefe, bro, cara, rapaz, amigo, parceiro
3. Usa: querida, linda, bora, fixe, top, ena, boa
4. Portugues europeu informal e fofo
5. Max 2 linhas + 1 emoji
6. NUNCA inventes precos de gasolina — diz sempre "usa 'gasolina mais barata no barreiro'"
7. Se nao souberes responder diz "Nao sei querida 🤔 Diz 'ajuda' para veres o que sei!"

CONTEXTO:
Modo poupanca: {m['nome']} | Disponivel: {disp:.0f}€ | Salario: {usuario.salario_liquido or 'nao registado'}€

ABREVIATURAS: BK=Burger King, Mac=McDonald's, conti=Continente, PD=Pingo Doce, JD=JD Sports, FL=Foot Locker"""

        resp = Groq(api_key=GROQ_API_KEY).chat.completions.create(
            model='llama-3.3-70b-versatile',
            messages=[{'role':'system','content':sys},{'role':'user','content':texto}],
            max_tokens=150)
        resposta = resp.choices[0].message.content
        return filtrar_resposta(resposta)
    except Exception as e:
        log.error(f'IA: {e}'); return "Nao percebi 🤔 Diz 'ajuda'!"

# ─── WISHLIST ────────────────────────────────────────────────
WISHLIST_CATS = {
    'roupa':       ('👗', ['vestido','casaco','camisola','blusa','calcas','calças','saia','top','hoodie','sweater','jaqueta','blusao','blazer','fato','conjunto']),
    'calcado':     ('👟', ['sapatilhas','sapatos','botas','botins','chinelas','sandalias','tenis','sneakers']),
    'acessorios':  ('👜', ['mala','carteira','cinto','chapeu','óculos','oculos','boné','bone','cachecol','luvas','colar','brincos','pulseira','anel']),
    'maquilagem':  ('💄', ['batom','base','blush','sombra','mascara','rimmel','primer','contorno','bronzer','highlighter','lip','eyeshadow','foundation','concealer','perfume','creme','soro','sérum','serum','hidratante']),
    'casa':        ('🏠', ['vela','quadro','espelho','almofada','planta','caneca','taca','copo','decoracao','ikea','organizer']),
    'tecnologia':  ('📱', ['iphone','android','earbuds','auscultadores','carregador','capa','tablet','smartwatch']),
    'outros':      ('🛒', []),
}

ESTACOES = {
    'verao':    ['verao','verão','praia','bikini','shorts','sandalia','leve','fresco','manga curta'],
    'inverno':  ['inverno','casaco','blusao','lã','la','quente','grossa','forro','boots','botas'],
    'primavera':['primavera','floral','flores','colorido','leve','pastel'],
    'outono':   ['outono','outonal','castanho','bordeaux','burgundy','oversize'],
}

def detetar_categoria_wishlist(texto):
    t = texto.lower()
    for cat, (emoji, palavras) in WISHLIST_CATS.items():
        if any(p in t for p in palavras):
            return cat, emoji
    return 'outros', '🛒'

def detetar_estacao_wishlist(texto):
    t = texto.lower()
    for estacao, palavras in ESTACOES.items():
        if any(p in t for p in palavras):
            return estacao
    return None

def comparar_precos_tavily(desc, marca=None):
    """Pesquisa o produto em várias lojas e compara preços."""
    try:
        import requests as req
        query = f"{marca + ' ' if marca else ''}{desc} comprar preco Portugal"
        r = req.post("https://api.tavily.com/search",
            json={
                'api_key': TAVILY_API_KEY,
                'query': query,
                'search_depth': 'advanced',
                'max_results': 5,
                'include_answer': False,
            }, timeout=20)
        if r.status_code != 200:
            log.error(f"Tavily: {r.status_code}"); return []
        results = r.json().get('results', [])
        lojas = []
        for res in results:
            url     = res.get('url','')
            titulo  = res.get('title','')
            conteudo= res.get('content','')
            # Extrai nome da loja do domínio
            import urllib.parse
            dominio = urllib.parse.urlparse(url).netloc.replace('www.','').split('.')[0].capitalize()
            # Extrai preço
            preco_match = re.search(r'(\d{1,3}[.,]\d{2})\s*€|€\s*(\d{1,3}[.,]\d{2})', conteudo)
            if preco_match:
                p_str = preco_match.group(1) or preco_match.group(2)
                try:
                    preco = float(p_str.replace(',','.'))
                    if 0 < preco < 10000:
                        lojas.append({'loja': dominio, 'preco': preco, 'url': url, 'titulo': titulo[:50]})
                except: pass
        lojas.sort(key=lambda x: x['preco'])
        return lojas[:4]
    except Exception as e:
        log.error(f"comparar precos: {e}"); return []

def processar_wishlist(phone_raw, usuario, texto):
    t = texto.lower()

    # Ver lista — pode filtrar por categoria ou estação
    if any(p in t for p in ['ver','lista','mostrar','wishlist','o que tenho','desejos']) and not any(p in t for p in ['quero','gostei','adiciona']):
        filtro_cat = None
        filtro_estacao = None
        for cat in WISHLIST_CATS:
            if cat in t: filtro_cat = cat; break
        for est in ESTACOES:
            if est in t: filtro_estacao = est; break
        try:
            query_sql = "SELECT id, descricao, preco, link, marca, categoria, estacao FROM wishlist WHERE usuario_id=:id AND comprado=FALSE"
            params = {'id': usuario.id}
            if filtro_cat:
                query_sql += " AND categoria=:cat"; params['cat'] = filtro_cat
            if filtro_estacao:
                query_sql += " AND estacao=:est"; params['est'] = filtro_estacao
            query_sql += " ORDER BY criado_em DESC"
            rows = db.session.execute(text(query_sql), params).fetchall()
            if not rows:
                filtro_txt = f" de {filtro_cat or filtro_estacao}" if (filtro_cat or filtro_estacao) else ""
                enviar_mensagem(phone_raw, f"A tua wishlist{filtro_txt} esta vazia 🛍️\nManda foto de etiqueta, link ou diz 'quero [produto]'!")
                return
            total = sum(r[2] for r in rows if r[2])
            titulo = f"🛍️ Wishlist"
            if filtro_cat: titulo += f" — {WISHLIST_CATS[filtro_cat][0]} {filtro_cat.capitalize()}"
            if filtro_estacao: titulo += f" — {filtro_estacao.capitalize()}"
            msg = f"{titulo} ({len(rows)} items"
            msg += f" | {total:.0f}€ total):\n\n" if total > 0 else "):\n\n"
            for i, r in enumerate(rows, 1):
                preco_txt   = f" — {r[2]:.2f}€" if r[2] else ""
                marca_txt   = f" ({r[4]})" if r[4] else ""
                cat_emoji   = WISHLIST_CATS.get(r[5], ('🛒',))[0] if r[5] else '🛒'
                estacao_txt = f" [{r[6]}]" if r[6] else ""
                link_txt    = f"\n   🔗 {r[3]}" if r[3] else ""
                msg += f"{i}. {cat_emoji} {r[1]}{marca_txt}{preco_txt}{estacao_txt}{link_txt}\n"
            msg += "\n'comprei o [nome]' para marcar | 'wishlist roupa' para filtrar"
            enviar_mensagem(phone_raw, msg)
        except Exception as e:
            log.error(f"wishlist ver: {e}"); enviar_mensagem(phone_raw, "Erro 😕")
        return

    # Manda link direto → analisa e compara preços
    link_match = re.search(r'https?://\S+', texto)
    if link_match:
        link = link_match.group(0)
        enviar_mensagem(phone_raw, "🔍 A analisar o produto e a comparar precos...")
        lojas = comparar_precos_tavily(texto.replace(link,'').strip() or 'produto', None)
        # Extrai info do link via Tavily
        try:
            import requests as req
            r2 = req.post("https://api.tavily.com/search",
                json={'api_key': TAVILY_API_KEY, 'query': link, 'max_results': 1}, timeout=15)
            titulo_prod = ''
            if r2.status_code == 200:
                res = r2.json().get('results', [{}])[0]
                titulo_prod = res.get('title','Produto')[:60]
        except:
            titulo_prod = 'Produto'

        cat, cat_emoji = detetar_categoria_wishlist(titulo_prod + ' ' + texto)
        estacao = detetar_estacao_wishlist(titulo_prod + ' ' + texto)
        preco_link = lojas[0]['preco'] if lojas else None

        db.session.execute(text(
            "INSERT INTO wishlist (usuario_id,descricao,preco,link,categoria,estacao) VALUES (:u,:d,:p,:l,:c,:e)"),
            {'u': usuario.id, 'd': titulo_prod or 'Produto', 'p': preco_link, 'l': link, 'c': cat, 'e': estacao})
        db.session.commit()

        msg = f"🛍️ Guardado na wishlist!\n{cat_emoji} {titulo_prod}\n"
        if lojas:
            msg += f"\n💰 Precos encontrados:\n"
            for i, l in enumerate(lojas):
                medalha = ['🥇','🥈','🥉','4️⃣'][i]
                msg += f"{medalha} {l['loja']}: {l['preco']:.2f}€\n   {l['url'][:45]}\n"
            if len(lojas) > 1:
                mais_barata = lojas[0]
                msg += f"\n✅ Mais barato: {mais_barata['loja']} a {mais_barata['preco']:.2f}€!"
        else:
            msg += "Nao encontrei comparacao de precos 😕"
        enviar_mensagem(phone_raw, msg)
        return

    # Adicionar por texto: "quero sapatilhas nike 89€"
    valor = extrair_valor(texto)
    stop_words = {'quero','isto','gostei','disto','comprar','uma','um','umas','uns','adorei','vi','e','de','da','do'}
    palavras = [w for w in texto.split() if not re.match(r'[0-9€,.]',w) and len(w)>2 and w.lower() not in stop_words]
    desc = ' '.join(palavras[:4]).capitalize() if palavras else 'Item'

    marca = None
    marcas_conhecidas = ['zara','nike','adidas','hm','h&m','bershka','stradivarius','pull','shein','mango','primark','jd','foot locker','snipes','mango','lefties','subdued']
    for m in marcas_conhecidas:
        if m in t: marca = m.capitalize(); break

    cat, cat_emoji = detetar_categoria_wishlist(texto)
    estacao = detetar_estacao_wishlist(texto)

    link_final = None
    preco_final = valor if valor > 0 else None
    lojas = []

    if TAVILY_API_KEY and len(desc) > 3:
        enviar_mensagem(phone_raw, f"🔍 A procurar '{desc}' e a comparar precos...")
        lojas = comparar_precos_tavily(desc, marca)
        if lojas:
            link_final = lojas[0]['url']
            if not preco_final: preco_final = lojas[0]['preco']

    db.session.execute(text(
        "INSERT INTO wishlist (usuario_id,descricao,preco,link,marca,categoria,estacao) VALUES (:u,:d,:p,:l,:m,:c,:e)"),
        {'u': usuario.id, 'd': desc, 'p': preco_final, 'l': link_final, 'm': marca, 'c': cat, 'e': estacao})
    db.session.commit()

    preco_txt = f" — {preco_final:.2f}€" if preco_final else ""
    estacao_txt = f" [{estacao}]" if estacao else ""
    msg = f"🛍️ {cat_emoji} Adicionado!\n{desc}{preco_txt}{estacao_txt}\n"
    if lojas:
        msg += f"\n💰 Precos encontrados:\n"
        for i, l in enumerate(lojas[:3]):
            medalha = ['🥇','🥈','🥉'][i]
            msg += f"{medalha} {l['loja']}: {l['preco']:.2f}€\n"
        if len(lojas) > 1:
            msg += f"\n✅ Mais barato: {lojas[0]['loja']} ({lojas[0]['preco']:.2f}€)"
    msg += "\n\nDiz 'wishlist' para ver tudo 😊"
    enviar_mensagem(phone_raw, msg)

def buscar_produto_tavily(marca, produto, referencia=None):
    """Pesquisa produto simples — usado na etiqueta."""
    try:
        import requests as req
        query = f"{marca} {produto}"
        if referencia and referencia != 'null': query += f" {referencia}"
        query += " comprar"
        r = req.post("https://api.tavily.com/search",
            json={'api_key': TAVILY_API_KEY, 'query': query, 'search_depth': 'basic', 'max_results': 3},
            timeout=15)
        if r.status_code != 200: return None, None
        results = r.json().get('results', [])
        for res in results:
            conteudo = res.get('content','')
            preco_match = re.search(r'(\d{1,3}[.,]\d{2})\s*€|€\s*(\d{1,3}[.,]\d{2})', conteudo)
            if preco_match:
                p_str = preco_match.group(1) or preco_match.group(2)
                try:
                    preco = float(p_str.replace(',','.'))
                    if 0 < preco < 10000:
                        return res.get('url'), preco
                except: pass
        return results[0].get('url') if results else None, None
    except Exception as e:
        log.error(f"Tavily: {e}"); return None, None

def ler_etiqueta_wishlist(phone_raw, usuario, url, mimetype):
    """Lê foto de etiqueta, pesquisa online e compara preços."""
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
        try:
            dados = json.loads(txt)
        except:
            return False
        if 'erro' in dados: return False

        desc   = dados.get('produto','Item')
        marca  = dados.get('marca')
        preco  = dados.get('preco')
        ref    = dados.get('referencia')
        tipo   = dados.get('tipo','outro')
        if ref and ref not in ['null','None',None]: desc = f"{desc} ({ref})"

        cat, cat_emoji = detetar_categoria_wishlist(desc + ' ' + tipo)
        estacao = detetar_estacao_wishlist(desc)

        lojas = []
        link_encontrado = None
        preco_online = None
        if TAVILY_API_KEY and marca:
            enviar_mensagem(phone_raw, f"🔍 A procurar '{desc}' e a comparar precos...")
            lojas = comparar_precos_tavily(dados.get('produto',''), marca)
            if lojas:
                link_encontrado = lojas[0]['url']
                preco_online = lojas[0]['preco']

        preco_final = preco or preco_online

        db.session.execute(text(
            "INSERT INTO wishlist (usuario_id,descricao,preco,link,marca,categoria,estacao) VALUES (:u,:d,:p,:l,:m,:c,:e)"),
            {'u': usuario.id, 'd': desc, 'p': preco_final, 'l': link_encontrado, 'm': marca, 'c': cat, 'e': estacao})
        db.session.commit()

        preco_txt   = f" — {preco_final:.2f}€" if preco_final else ""
        marca_txt   = f" ({marca})" if marca else ""
        estacao_txt = f" [{estacao}]" if estacao else ""
        msg = f"🛍️ {cat_emoji} Guardado!\n{desc}{marca_txt}{preco_txt}{estacao_txt}\n"
        if lojas:
            msg += f"\n💰 Precos online:\n"
            for i, l in enumerate(lojas[:3]):
                medalha = ['🥇','🥈','🥉'][i]
                msg += f"{medalha} {l['loja']}: {l['preco']:.2f}€\n"
            if len(lojas) > 1:
                msg += f"\n✅ Mais barato: {lojas[0]['loja']} ({lojas[0]['preco']:.2f}€)!"
        else:
            msg += "Nao encontrei precos online 😕"
        msg += "\n\nDiz 'wishlist' para ver tudo 😊"
        enviar_mensagem(phone_raw, msg)
        return True
    except Exception as e:
        log.error(f"etiqueta: {e}", exc_info=True); return False

def marcar_wishlist_comprado(phone_raw, usuario, texto):
    t = texto.lower()
    palavras = [w for w in re.findall(r'[a-zà-ú]+', t) if w not in ['comprei','ja','já','o','a','os','as']]
    if not palavras:
        enviar_mensagem(phone_raw, "O que compraste? Ex: 'comprei o vestido'"); return
    chave = palavras[0]
    try:
        r = db.session.execute(text(
            "UPDATE wishlist SET comprado=TRUE WHERE usuario_id=:u AND LOWER(descricao) LIKE :c AND comprado=FALSE RETURNING descricao,preco"),
            {'u': usuario.id, 'c': f'%{chave}%'}).fetchone()
        db.session.commit()
        if r:
            enviar_mensagem(phone_raw, f"✅ '{r[0]}' marcado como comprado! 🎉\nVai registar o gasto? Diz quanto pagaste!")
        else:
            enviar_mensagem(phone_raw, f"Nao encontrei '{chave}' na wishlist 🤔\nDiz 'wishlist' para veres o que tens.")
    except Exception as e:
        log.error(f"wishlist comprado: {e}"); enviar_mensagem(phone_raw, "Erro 😕")

def remover_wishlist(phone_raw, usuario, texto):
    t = texto.lower()
    palavras = [w for w in re.findall(r'[a-zà-ú]+', t) if w not in ['remove','apaga','da','wishlist','o','a']]
    if not palavras:
        enviar_mensagem(phone_raw, "O que queres remover? Ex: 'remove da wishlist o vestido'"); return
    chave = palavras[0]
    try:
        r = db.session.execute(text(
            "DELETE FROM wishlist WHERE usuario_id=:u AND LOWER(descricao) LIKE :c RETURNING descricao"),
            {'u': usuario.id, 'c': f'%{chave}%'}).fetchone()
        db.session.commit()
        if r:
            enviar_mensagem(phone_raw, f"🗑️ '{r[0]}' removido da wishlist!")
        else:
            enviar_mensagem(phone_raw, f"Nao encontrei '{chave}' 🤔 Diz 'wishlist' para veres o que tens.")
    except Exception as e:
        log.error(f"wishlist remove: {e}"); enviar_mensagem(phone_raw, "Erro 😕")



# ─── FOTO KM (odómetro após abastecer) ──────────────────────
def ler_foto_km(phone_raw, usuario, url, mimetype):
    """Lê foto do odómetro e regista km para calcular consumo."""
    try:
        from groq import Groq
        c = baixar_media(url)
        if not c: return False
        mt = 'image/png' if 'png' in mimetype else 'image/jpeg'
        img = base64.b64encode(c).decode()
        resp = Groq(api_key=GROQ_API_KEY).chat.completions.create(
            model='meta-llama/llama-4-scout-17b-16e-instruct', max_tokens=80,
            messages=[{'role':'user','content':[
                {'type':'image_url','image_url':{'url':f'data:{mt};base64,{img}'}},
                {'type':'text','text':'Le o odometro/conta-km deste carro. Responde APENAS em JSON: {"km":NUMERO_INTEIRO}. Se nao for odometro: {"erro":"nao_odometro"}'}
            ]}])
        txt = re.sub(r'```json|```','', resp.choices[0].message.content.strip()).strip()
        try:
            dados = json.loads(txt)
        except:
            return False
        if 'erro' in dados: return False
        km = dados.get('km')
        if not km: return False

        # Busca ultimo registo de km
        ultimo = db.session.execute(text(
            "SELECT km, valor FROM km_combustivel WHERE usuario_id=:u ORDER BY data DESC LIMIT 1"),
            {'u': usuario.id}).fetchone()

        # Busca ultimo abastecimento para calcular litros/valor
        ultimo_gasto_gas = db.session.query(Despesa).filter(
            Despesa.usuario_id==usuario.id, Despesa.categoria=='combustivel'
        ).order_by(Despesa.id.desc()).first()

        valor_gas = ultimo_gasto_gas.valor if ultimo_gasto_gas else 0
        # Estima litros (gasolina 95 ~1.9€/L em media)
        litros_est = round(valor_gas / 1.9, 1) if valor_gas > 0 else None

        consumo = None
        custo_km = None
        msg_consumo = ''

        if ultimo and km > ultimo[0]:
            km_percorridos = km - ultimo[0]
            if litros_est and km_percorridos > 0:
                consumo = round(litros_est / km_percorridos * 100, 1)
                custo_km = round(valor_gas / km_percorridos * 100, 2) if valor_gas > 0 else None
                msg_consumo = f"\n\n📊 Desde o ultimo abastecimento:\n🛣️ {km_percorridos} km percorridos\n⛽ Consumo: {consumo}L/100km\n💶 Custo: {custo_km:.2f}€/100km" if custo_km else f"\n\n🛣️ {km_percorridos} km desde o ultimo abastecimento"

        db.session.execute(text(
            "INSERT INTO km_combustivel (usuario_id,km,litros,valor,consumo_l100,custo_km) VALUES (:u,:k,:l,:v,:c,:ck)"),
            {'u': usuario.id, 'k': km, 'l': litros_est, 'v': valor_gas, 'c': consumo, 'ck': custo_km})
        db.session.commit()

        enviar_mensagem(phone_raw, f"🚗 Km registado: {km:,} km{msg_consumo}\n\nProximo abastecimento manda foto do odometro de novo! 📸")
        return True
    except Exception as e:
        log.error(f"foto km: {e}", exc_info=True); return False


def processar_splitting(phone_raw, usuario, texto):
    valor = extrair_valor(texto)
    if valor == 0:
        enviar_mensagem(phone_raw, "Quanto foi o total? Ex: dividi 60€ jantar com o Ruben"); return

    t = texto.lower()
    # Extrai pessoa
    m_pessoa = re.search(r'com (?:o |a |os |as )?([A-Za-zÀ-ú]+)', texto, re.IGNORECASE)
    pessoa = m_pessoa.group(1).capitalize() if m_pessoa else 'Alguém'

    # Extrai descrição
    palavras = [w for w in texto.split() if not re.match(r'[0-9€,.]', w) and len(w) > 2
                and w.lower() not in ['dividi','dividir','meias','split','partilhei','com','o','a']]
    desc = ' '.join(palavras[:3]).capitalize() if palavras else 'Gasto partilhado'

    valor_cada = round(valor / 2, 2)

    try:
        db.session.execute(text(
            "INSERT INTO splitting (usuario_id,descricao,valor_total,valor_cada,pessoa) VALUES (:u,:d,:vt,:vc,:p)"),
            {'u': usuario.id, 'd': desc, 'vt': valor, 'vc': valor_cada, 'p': pessoa})
        db.session.commit()
        enviar_mensagem(phone_raw,
            f"✂️ Split registado!\n{desc}: {valor:.2f}€ total\n"
            f"A tua parte: {valor_cada:.2f}€\n"
            f"{pessoa} fica-te a dever: {valor_cada:.2f}€\n\n"
            f"Quando {pessoa} pagar diz: '{pessoa} pagou'")
    except Exception as e:
        log.error(f"splitting: {e}"); enviar_mensagem(phone_raw, "Erro 😕")

def ver_splits(phone_raw, usuario):
    try:
        rows = db.session.execute(text(
            "SELECT descricao, valor_cada, pessoa, criado_em FROM splitting WHERE usuario_id=:u AND pago=FALSE ORDER BY criado_em DESC"),
            {'u': usuario.id}).fetchall()
        if not rows:
            enviar_mensagem(phone_raw, "Nao tens splits pendentes 😊"); return
        total_pendente = sum(r[1] for r in rows)
        msg = f"✂️ Splits pendentes:\n\n"
        for r in rows:
            msg += f"• {r[0]} — {r[1]:.2f}€ ({r[2]})\n"
        msg += f"\n💰 Total a receber: {total_pendente:.2f}€"
        enviar_mensagem(phone_raw, msg)
    except Exception as e:
        log.error(f"splits: {e}"); enviar_mensagem(phone_raw, "Erro 😕")

# ─── MODO DISCRETO ───────────────────────────────────────────
def modo_discreto(phone_raw):
    try:
        import requests as req
        # Busca mensagens recentes
        r = req.get(f"{WAHA_URL}/api/default/messages",
                    headers={'X-Api-Key': WAHA_API_KEY},
                    params={'chatId': phone_raw, 'limit': 30},
                    timeout=15)
        if r.status_code != 200:
            enviar_mensagem(phone_raw, "Nao consigo apagar mensagens agora 😕"); return
        msgs = r.json()
        apagadas = 0
        for msg in msgs:
            msg_id = msg.get('id','')
            if msg_id:
                req.delete(f"{WAHA_URL}/api/default/messages/{msg_id}",
                           headers={'X-Api-Key': WAHA_API_KEY}, timeout=10)
                apagadas += 1
        enviar_mensagem(phone_raw, f"🔒 Modo discreto! Apaguei {apagadas} mensagens 👍")
    except Exception as e:
        log.error(f"discreto: {e}")
        enviar_mensagem(phone_raw, "Nao consegui apagar 😕 O WAHA pode nao suportar esta funcao.")

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
                    enviar_mensagem(f"{u.phone}@lid", "💰 Hoje e dia de salario! Manda o recibo ou diz o valor 🚀")

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
                if estado=='fecho_feito' and dados.get('mes')==hoje.month and dados.get('ano')==hoje.year: continue
                enviar_mensagem(f"{u.phone}@lid",
                    f"📅 Novo mes financeiro!\nDiz 'resumo anterior' p/ veres como correu {nomes[mes_ant-1]} 📊")

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
                        enviar_mensagem(f"{u.phone}@lid",
                            f"⚠️ A meio do mes e ja usaste {pct:.0f}% do orcamento!\nVai com calma nos proximos dias 💪")

def verificar_aniversarios():
    with app.app_context():
        hoje = agora()
        try:
            rows = db.session.execute(text("""
                SELECT u.phone, a.nome, a.data_aniv
                FROM aniversarios a JOIN users u ON a.usuario_id=u.id
                WHERE EXTRACT(month FROM a.data_aniv)=:m AND EXTRACT(day FROM a.data_aniv) IN (:d, :d5, :d1)
            """), {'m':hoje.month,'d':hoje.day,'d5':hoje.day+5,'d1':hoje.day+1}).fetchall()
            for r in rows:
                phone, nome, data = r
                dias_falta = (data.replace(year=hoje.year) - hoje.date()).days
                if dias_falta == 5:
                    enviar_mensagem(f"{phone}@lid", f"🎂 Daqui a 5 dias e o aniversario de {nome}! Ja pensaste no presente? 🎁")
                elif dias_falta == 1:
                    enviar_mensagem(f"{phone}@lid", f"🎂 AMANHA e o aniversario de {nome}! Nao te esqueças! 🎉")
                elif dias_falta == 0:
                    enviar_mensagem(f"{phone}@lid", f"🎂🎉 HOJE e o aniversario de {nome}! Ja desejaste parabens? 💕")
        except Exception as e:
            log.error(f"anivs scheduler: {e}")

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
            ano = hoje.year
            for u in Usuario.query.all():
                if not u.phone or not u.salario_liquido: continue
                total_gastos = db.session.query(db.func.sum(Despesa.valor)).filter(
                    Despesa.usuario_id==u.id,
                    db.extract('year',Despesa.data)==ano).scalar() or 0
                total_receita = db.session.query(db.func.sum(Receita.valor)).filter(
                    Receita.usuario_id==u.id,
                    db.extract('year',Receita.data)==ano).scalar() or 0
                top_cat = db.session.query(Despesa.categoria, db.func.sum(Despesa.valor)).filter(
                    Despesa.usuario_id==u.id,
                    db.extract('year',Despesa.data)==ano).group_by(Despesa.categoria).order_by(db.func.sum(Despesa.valor).desc()).first()
                poupanca_total = total_receita - total_gastos
                reserva = get_reserva(u.id)
                msg = (f"🎊 O teu {ano} em numeros!\n\n"
                       f"💰 Recebeste: {total_receita:.0f}€\n"
                       f"🛒 Gastaste: {total_gastos:.0f}€\n"
                       f"💎 Poupaste: {poupanca_total:.0f}€\n"
                       f"🛡️ Reserva: {reserva:.2f}€\n")
                if top_cat:
                    msg += f"🏆 Maior gasto: {top_cat[0].capitalize()} ({top_cat[1]:.0f}€)\n"
                msg += f"\nFeliz {ano+1}! Vamos a mais um ano a bombar! 🚀🎉"
                enviar_mensagem(f"{u.phone}@lid", msg)

# ─── ARRANQUE ────────────────────────────────────────────────
with app.app_context():
    try: db.create_all()
    except Exception as e: log.warning(f"db: {e}")
    criar_tabelas()

scheduler.add_job(lembrete_recibo,            'cron', hour=11, minute=0)
scheduler.add_job(lembrete_salario,           'cron', hour=9,  minute=0)
scheduler.add_job(fecho_mes,                  'cron', hour=10, minute=0)
scheduler.add_job(aviso_meio_mes,             'cron', hour=10, minute=0)
scheduler.add_job(resumo_semanal,             'cron', hour=9,  minute=30, day_of_week='mon')
scheduler.add_job(verificar_despesas_futuras, 'cron', hour=8,  minute=0)
scheduler.add_job(verificar_aniversarios,     'cron', hour=9,  minute=0)
scheduler.add_job(wrapped_anual,              'cron', hour=20, minute=0)
scheduler.start()
log.info("Ze das Financas v6 iniciado")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
