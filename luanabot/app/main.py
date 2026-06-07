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

GASTAR_NORMAL   = 200
GASTAR_SUBSIDIO = 400
FUNDO_PCT       = 0.05
BASE_COMBUSTIVEL = 50

scheduler = BackgroundScheduler()

def agora():
    return datetime.now(TZ) if TZ else datetime.now()

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
    'starbucks':'Starbucks','sub':'Subway','subway':'Subway',
    'zen':'Zen Sushi','sushi':'Sushi','foot':'Foot Locker','fl':'Foot Locker',
    'jd':'JD Sports','snipes':'Snipes','zara':'Zara','z':'Zara','hm':'H&M',
    'nike':'Nike','nk':'Nike','adidas':'Adidas','ads':'Adidas','adi':'Adidas',
    'apl':'Apple','sam':'Samsung','smg':'Samsung','ps':'PlayStation','psn':'PlayStation',
    'xb':'Xbox','xbx':'Xbox','wrt':'Worten','worten':'Worten','rp':'Radio Popular',
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

# ─── APRENDIZAGEM ────────────────────────────────────────────
def criar_tabela_aprendizagem():
    try:
        db.session.execute(text("""
            CREATE TABLE IF NOT EXISTS aprendizagem (
                chave VARCHAR(100) PRIMARY KEY, categoria VARCHAR(50) NOT NULL
            )
        """))
        db.session.execute(text("""
            CREATE TABLE IF NOT EXISTS estado_utilizador (
                phone VARCHAR(50) PRIMARY KEY,
                estado VARCHAR(100),
                dados TEXT,
                atualizado TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))
        db.session.commit()
    except Exception as e:
        log.warning(f"criar tabelas: {e}"); db.session.rollback()

def carregar_aprendidas():
    try:
        rows = db.session.execute(text("SELECT chave, categoria FROM aprendizagem")).fetchall()
        return {r[0]: r[1] for r in rows}
    except Exception:
        db.session.rollback(); return {}

def guardar_aprendida(chave, categoria):
    try:
        db.session.execute(
            text("INSERT INTO aprendizagem (chave, categoria) VALUES (:c,:cat) ON CONFLICT (chave) DO UPDATE SET categoria=:cat"),
            {'c': chave.lower().strip(), 'cat': categoria}
        )
        db.session.commit(); return True
    except Exception as e:
        log.error(f"guardar aprendida: {e}"); db.session.rollback(); return False

def get_estado(phone):
    try:
        r = db.session.execute(text("SELECT estado, dados FROM estado_utilizador WHERE phone=:p"), {'p': phone}).fetchone()
        return (r[0], json.loads(r[1]) if r[1] else {}) if r else (None, {})
    except Exception:
        db.session.rollback(); return (None, {})

def set_estado(phone, estado, dados=None):
    try:
        db.session.execute(
            text("INSERT INTO estado_utilizador (phone,estado,dados,atualizado) VALUES (:p,:e,:d,NOW()) ON CONFLICT (phone) DO UPDATE SET estado=:e, dados=:d, atualizado=NOW()"),
            {'p': phone, 'e': estado, 'd': json.dumps(dados or {})}
        )
        db.session.commit()
    except Exception as e:
        log.error(f"set estado: {e}"); db.session.rollback()

def limpar_estado(phone):
    set_estado(phone, None, {})

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
                    enviar_mensagem(phone_raw, "Nao consegui perceber o audio 😕 Tenta escrever!")
                    return jsonify({'status':'ok'})
            elif 'image' in mime:
                resultado = ler_foto_talao(url, mime)
                if resultado:
                    enviar_mensagem(phone_raw, f'📸 Li: {resultado}')
                    texto = resultado
                else:
                    enviar_mensagem(phone_raw, "Nao consegui ler a imagem 😕 Escreve o valor!")
                    return jsonify({'status':'ok'})
            elif 'pdf' in mime or 'application' in mime:
                resultado = ler_pdf_salario(url)
                if resultado:
                    enviar_mensagem(phone_raw, f'📄 Vi no recibo: {resultado:.2f} euros — e esse o teu salario?')
                    set_estado(phone, 'confirmar_salario', {'valor': resultado})
                    return jsonify({'status':'ok'})
                else:
                    enviar_mensagem(phone_raw, "Nao consegui ler o PDF 😕 Diz o valor: recebi X euros")
                    return jsonify({'status':'ok'})

        if not texto: return jsonify({'status':'ok'})
        log.info(f"Mensagem de {phone}: {texto}")
        with app.app_context():
            processar_texto(phone_raw, phone, texto)
    except Exception as e:
        log.error(f'Erro webhook: {e}', exc_info=True)
    return jsonify({'status':'ok'})

@app.route('/', methods=['GET'])
def health():
    return jsonify({'status':'ok','bot':'Ze das Financas'})

# ─── MEDIA ───────────────────────────────────────────────────
def baixar_media(url):
    import requests as req
    from urllib.parse import urlparse
    if not url: return None
    if 'localhost' in url or '127.0.0.1' in url:
        parsed = urlparse(url)
        url = WAHA_URL.rstrip('/') + parsed.path + (('?'+parsed.query) if parsed.query else '')
    log.info(f'Download media: {url}')
    try:
        r = req.get(url, headers={'X-Api-Key': WAHA_API_KEY}, timeout=30)
        log.info(f'Media status: {r.status_code}, {len(r.content)} bytes')
        return r.content if r.status_code == 200 and r.content else None
    except Exception as e:
        log.error(f'Erro download: {e}'); return None

def transcrever_audio(url):
    try:
        from groq import Groq
        c = baixar_media(url)
        if not c: return ''
        with tempfile.NamedTemporaryFile(suffix='.ogg', delete=False) as f:
            f.write(c); fname = f.name
        client = Groq(api_key=GROQ_API_KEY)
        with open(fname,'rb') as af:
            t = client.audio.transcriptions.create(
                model='whisper-large-v3', file=(os.path.basename(fname), af.read()),
                language='pt', prompt='Gastos em euros. Lojas: Continente, BK, McDonalds, Galp, Zara.')
        try: os.unlink(fname)
        except: pass
        log.info(f'Audio: {t.text}'); return t.text.strip()
    except Exception as e:
        log.error(f'Erro audio: {e}', exc_info=True); return ''

def ler_foto_talao(url, mimetype='image/jpeg'):
    try:
        from groq import Groq
        c = baixar_media(url)
        if not c: return ''
        mt = 'image/png' if 'png' in mimetype else ('image/webp' if 'webp' in mimetype else 'image/jpeg')
        img = base64.b64encode(c).decode()
        client = Groq(api_key=GROQ_API_KEY)
        resp = client.chat.completions.create(
            model='meta-llama/llama-4-scout-17b-16e-instruct', max_tokens=80,
            messages=[{'role':'user','content':[
                {'type':'image_url','image_url':{'url':f'data:{mt};base64,{img}'}},
                {'type':'text','text':'Le este talao. Responde APENAS: "X euros LOJA". Ex: "25.50 euros Continente". Se nao deres, responde: erro'}
            ]}])
        txt = resp.choices[0].message.content.strip()
        log.info(f'Foto: {txt}')
        return '' if 'erro' in txt.lower() else txt
    except Exception as e:
        log.error(f'Erro foto: {e}', exc_info=True); return ''

def ler_pdf_salario(url):
    try:
        import requests as req
        from urllib.parse import urlparse
        if 'localhost' in url or '127.0.0.1' in url:
            parsed = urlparse(url)
            url = WAHA_URL.rstrip('/') + parsed.path
        r = req.get(url, headers={'X-Api-Key': WAHA_API_KEY}, timeout=30)
        if r.status_code != 200: return None
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as f:
            f.write(r.content); fname = f.name
        valor = extrair_salario_pdf(fname)
        try: os.unlink(fname)
        except: pass
        return valor
    except Exception as e:
        log.error(f'Erro pdf: {e}'); return None

# ─── PROCESSAR TEXTO ─────────────────────────────────────────
def processar_texto(phone_raw, phone, texto):
    usuario = Usuario.query.filter_by(phone=phone).first()
    if not usuario:
        usuario = Usuario(phone=phone, nome='Luana')
        db.session.add(usuario); db.session.commit()

    t = texto.lower().strip()

    # ESTADO — confirmar salário do recibo
    estado, dados_estado = get_estado(phone)
    if estado == 'confirmar_salario':
        if any(p in t for p in ['sim','yes','correto','certo','exato','é isso','e isso']):
            valor = dados_estado.get('valor', 0)
            limpar_estado(phone)
            processar_receita(phone_raw, usuario, f"recebi {valor}")
            return
        elif tem_numero(texto):
            limpar_estado(phone)
            processar_receita(phone_raw, usuario, texto)
            return
        else:
            limpar_estado(phone)
            enviar_mensagem(phone_raw, "Ok, diz-me tu: recebi X euros 💰")
            return

    if estado == 'aguardar_recibo':
        if any(p in t for p in ['sim','yes','quero','manda','envia']):
            limpar_estado(phone)
            enviar_mensagem(phone_raw, "Manda o PDF ou foto do recibo e eu trato do resto! 📄")
            return
        elif any(p in t for p in ['nao','não','valor','digo']) or tem_numero(texto):
            limpar_estado(phone)
            if tem_numero(texto):
                processar_receita(phone_raw, usuario, texto)
            else:
                enviar_mensagem(phone_raw, "Ok, diz o valor: recebi X euros 💰")
            return

    # APRENDER
    m = re.search(r'aprende que (.+?) (?:é|e|sao|são) (?:da categoria |categoria )?(\w+)', t)
    if m:
        chave = m.group(1).strip().strip('"\'')
        cat = normalizar_categoria(m.group(2))
        if cat in CATEGORIAS_VALIDAS:
            if guardar_aprendida(chave, cat):
                enviar_mensagem(phone_raw, f"🧠 Aprendido! '{chave}' = {cat.capitalize()} p/ sempre 😎")
            else:
                enviar_mensagem(phone_raw, "Ops, nao consegui guardar 😕")
        else:
            cats = ', '.join(CATEGORIAS_VALIDAS)
            enviar_mensagem(phone_raw, f"Hmm, nao conheço essa categoria 🤔\nUsa: {cats}")
        return

    # CORRIGIR
    m2 = re.search(r'(?:corrige|corrigir|muda|mudar|afinal|isso é|isso e|o ultimo|o último) (?:para |o )*(\w+)', t)
    if m2 and any(p in t for p in ['corrige','corrigir','muda','mudar','afinal','isso é','isso e','ultimo','último']):
        cat = normalizar_categoria(m2.group(1))
        if cat in CATEGORIAS_VALIDAS:
            corrigir_ultimo(phone_raw, usuario, cat); return

    # CRIADOR
    if any(p in t for p in ['quem criou','quem te fez','quem te criou','criador','quem te programou']):
        enviar_mensagem(phone_raw, "Fui criado pelo tuga27 🚀\nO mesmo genio por tras do Zeflix (plataforma de streaming de filmes e series) e agora tambem do teu gestor financeiro pessoal 😎")
        return

    if any(p in t for p in ['ajuda','help','/start','comandos']):
        enviar_ajuda(phone_raw); return

    if t in ['ola','olá','oi','boas','hey'] or 'bom dia' in t or 'boa tarde' in t or 'boa noite' in t:
        enviar_boas_vindas(phone_raw); return

    if 'estou teso' in t or 'tou teso' in t or 'sem dinheiro' in t or 'liso' in t:
        modo_teso(phone_raw, usuario); return

    if any(p in t for p in ['gasolina mais barata','posto mais barato','gasolina barata']):
        gasolina_barata(phone_raw, t); return

    if 'conjunta' in t and any(p in t for p in ['quanto','tenho','sobra','resta']):
        enviar_conjunta(phone_raw, usuario); return

    if any(p in t for p in ['quanto tenho','quanto me resta','quanto sobra','saldo']):
        enviar_quanto_tenho(phone_raw, usuario); return

    if any(p in t for p in ['resumo anterior','mes passado','mes anterior']):
        mes_ant = agora().month - 1 if agora().month > 1 else 12
        ano_ant = agora().year if agora().month > 1 else agora().year - 1
        enviar_resumo(phone_raw, usuario, mes_ant, ano_ant); return

    if any(p in t for p in ['resumo','como estou','quanto gastei','situacao','situação']):
        enviar_resumo(phone_raw, usuario); return

    if any(p in t for p in ['plano','transferencia','transferência','distribuicao']):
        enviar_plano_mes(phone_raw, usuario); return

    if 'score' in t or 'nota' in t or 'pontuacao' in t:
        enviar_score(phone_raw, usuario); return

    if any(p in t for p in ['poupar para','quero poupar','objetivo','objectivo']):
        enviar_mensagem(phone_raw, processar_mensagem_ia(texto, usuario, 'objetivo')); return

    if any(p in t for p in ['mes que vem','mês que vem','proximo mes','próximo mês']) and tem_numero(texto):
        processar_despesa_futura(phone_raw, usuario, texto); return

    if any(p in t for p in ['dentista','seguro','inspecao','inspeção']) and 'mes' in t and tem_numero(texto):
        processar_despesa_futura(phone_raw, usuario, texto); return

    if any(p in t for p in ['posso comprar','posso gastar','vale a pena','consigo comprar']):
        simular_compra(phone_raw, usuario, texto); return

    if any(p in t for p in ['recebi','ordenado','salario','salário','recibo','vencimento']) and tem_numero(texto):
        processar_receita(phone_raw, usuario, texto); return

    if tem_numero(texto) and eh_gasto(texto):
        processar_despesa(phone_raw, usuario, texto); return

    enviar_mensagem(phone_raw, perguntar_ia(texto, usuario))

def tem_numero(texto):
    return bool(re.search(r'[0-9]+[.,]?[0-9]*', texto))

def extrair_valor(texto):
    m = re.findall(r'[0-9]+[.,]?[0-9]*', texto)
    return float(m[0].replace(',','.')) if m else 0

def eh_gasto(texto):
    t = texto.lower()
    verbos = ['gastei','paguei','comprei','almocei','jantei','custou','meti','abasteci','lanchei']
    if any(v in t for v in verbos): return True
    if '€' in t or 'euro' in t: return True
    cat, _, _ = categorizar(texto)
    return cat != 'outros'

# ─── CÁLCULOS ────────────────────────────────────────────────
def calcular_plano(salario):
    mes = agora().month
    fixo_carro=350; fixo_ordem=20; fixo_conjunta=50
    fixo_unhas = 50 if mes <= 9 else 25
    fixo_combustivel = BASE_COMBUSTIVEL
    total_fixos = fixo_carro + fixo_ordem + fixo_conjunta + fixo_unhas + fixo_combustivel
    fundo  = round(salario * FUNDO_PCT, 2)
    gastar = GASTAR_SUBSIDIO if mes in [6,11] else GASTAR_NORMAL
    poupanca = round(salario - total_fixos - fundo - gastar, 2)
    return {'salario':salario,'fixos':total_fixos,'carro':fixo_carro,'ordem':fixo_ordem,
            'unhas':fixo_unhas,'conjunta':fixo_conjunta,'combustivel':fixo_combustivel,
            'fundo':fundo,'gastar':gastar,'poupanca':poupanca,'subsidio':mes in [6,11]}

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

# ─── PROCESSAR DESPESA ───────────────────────────────────────
def processar_despesa(phone_raw, usuario, texto):
    valor = extrair_valor(texto)
    if valor == 0:
        enviar_mensagem(phone_raw, "Nao percebi o valor 🤔 Diz tipo: gastei 25 no continente"); return

    categoria, emoji, nome_loja = categorizar(texto)
    na_conjunta = 'conjunta' in texto.lower()

    despesa = Despesa(usuario_id=usuario.id, valor=valor, categoria=categoria,
                      descricao=('[conjunta] ' if na_conjunta else '')+texto[:90],
                      data=agora().replace(tzinfo=None))
    db.session.add(despesa); db.session.commit()

    mes=agora().month; ano=agora().year
    mes_ant = mes-1 if mes>1 else 12
    ano_ant = ano if mes>1 else ano-1

    total_cat     = gastos_categoria_mes(usuario, categoria, mes, ano)
    total_cat_ant = gastos_categoria_mes(usuario, categoria, mes_ant, ano_ant)
    disp = calcular_disponivel(usuario)
    gastar = GASTAR_SUBSIDIO if mes in [6,11] else GASTAR_NORMAL
    pct_usado = ((gastar - disp) / gastar * 100) if gastar > 0 else 0

    # Comentário personalidade
    extra = ''
    inicio_semana = agora().replace(tzinfo=None) - timedelta(days=agora().weekday())
    vezes_semana = db.session.query(db.func.count(Despesa.id)).filter(
        Despesa.usuario_id==usuario.id, Despesa.categoria==categoria,
        Despesa.data>=inicio_semana
    ).scalar() or 0

    if categoria == 'fastfood' and vezes_semana >= 3:
        extra = f'\n😏 Ja e a {vezes_semana}a vez de fast food esta semana!'
    elif categoria == 'gota' and total_cat > 30:
        extra = f'\n🧃 {total_cat:.0f} euros em bebidas este mes... abranda!'
    elif categoria == 'combustivel':
        if total_cat > BASE_COMBUSTIVEL * 1.5:
            extra = f'\n⛽ Ja gastaste {total_cat:.0f} euros em gasolina, acima da base de {BASE_COMBUSTIVEL}€!'
        elif total_cat > BASE_COMBUSTIVEL:
            extra = f'\n⛽ Passaste a base de {BASE_COMBUSTIVEL}€ em gasolina este mes'
    elif agora().weekday() in [4,5] and agora().hour >= 19 and categoria in ['restaurante','fastfood']:
        extra = '\n🍻 Fim de semana a noite, la vem o gasto!'
    elif total_cat_ant > 0 and total_cat > total_cat_ant * 1.3:
        extra = f'\n⚠️ Ja gastaste mais em {categoria} que o mes passado todo!'
    elif total_cat_ant > 0 and total_cat < total_cat_ant * 0.7:
        extra = f'\n✅ Menos em {categoria} que o mes passado. Boa!'

    # Aviso 80%
    aviso_80 = ''
    if pct_usado >= 80 and pct_usado < 100:
        aviso_80 = f'\n\n🔔 Ja usaste {pct_usado:.0f}% do orcamento do mes!'
    elif pct_usado >= 100:
        aviso_80 = f'\n\n🔴 Passaste o orcamento! Gastaste {abs(disp):.0f}€ a mais.'

    # Gasto estranho
    gasto_estranho = ''
    if total_cat_ant > 0 and valor > total_cat_ant * 0.8:
        gasto_estranho = f'\n💡 Isso e muito de uma vez para {categoria}. Tudo bem?'

    conjunta_txt = ' (conjunta 💑)' if na_conjunta else ''
    msg = (f"{emoji} Bora! {nome_loja} {valor:.2f}€{conjunta_txt}\n"
           f"{categoria.capitalize()}: {total_cat:.2f}€ este mes{extra}{gasto_estranho}{aviso_80}\n"
           f"💚 Para gastar: {disp:.2f}€ ({100-pct_usado:.0f}% livre)")
    enviar_mensagem(phone_raw, msg)

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
    p = calcular_plano(salario)
    futuras = DespesaFutura.query.filter(DespesaFutura.usuario_id==usuario.id, DespesaFutura.pago==False).all()
    total_fut = sum(d.valor_reserva_mensal for d in futuras)
    poupanca_final = p['poupanca'] - total_fut

    msg = f"💰 Boa, recebeste {salario:.2f}€!\n\n📋 Plano do mes:\n"
    msg += f"🏠 Fixos: {p['fixos']:.0f}€\n"
    msg += f"   🚗 Carro {p['carro']:.0f} | 💼 Ordem {p['ordem']:.0f} | 💅 Unhas {p['unhas']:.0f} | 💑 Conjunta {p['conjunta']:.0f} | ⛽ Gasolina {p['combustivel']:.0f}\n"
    msg += f"🛡️ Fundo emergencia: {p['fundo']:.2f}€ (Revolut, nao mexas!)\n"
    msg += f"💳 Para gastar: {p['gastar']:.0f}€\n"
    if total_fut > 0:
        msg += f"📅 Reservas futuras: {total_fut:.2f}€\n"
        for d in futuras: msg += f"   {d.descricao}: {d.valor_reserva_mensal:.0f}€\n"
    msg += f"💎 Poupanca: {poupanca_final:.2f}€ 🔥"
    if p['subsidio']: msg += "\n\n🌴 Mes de subsidio! Meti mais margem. Aproveita com juizo 😉"
    enviar_mensagem(phone_raw, msg)

    # Pergunta se quer ver resumo do mes passado
    mes_ant = agora().month - 1 if agora().month > 1 else 12
    nomes = ['Jan','Fev','Mar','Abr','Mai','Jun','Jul','Ago','Set','Out','Nov','Dez']
    enviar_mensagem(phone_raw, f"Queres ver o resumo de {nomes[mes_ant-1]}? Diz 'resumo anterior' 📊")

# ─── QUANTO TENHO ────────────────────────────────────────────
def enviar_quanto_tenho(phone_raw, usuario):
    disp = calcular_disponivel(usuario)
    p = calcular_plano(usuario.salario_liquido or 0)
    if disp < 0:
        msg = f"😬 Passaste o orcamento em {abs(disp):.2f}€!\nA partir daqui e do fundo ou poupanca. Cuidado!"
    elif disp < 30:
        msg = f"💸 So tens {disp:.2f}€ para gastar. Aperta o cinto!"
    else:
        msg = f"💚 Tens {disp:.2f}€ para gastar 😎\n🛡️ Fundo: {p['fundo']:.0f}€ | 💎 Poupanca: {p['poupanca']:.0f}€"
    enviar_mensagem(phone_raw, msg)

def enviar_conjunta(phone_raw, usuario):
    mes=agora().month; ano=agora().year
    gasto = db.session.query(db.func.sum(Despesa.valor)).filter(
        Despesa.usuario_id==usuario.id,
        db.extract('month',Despesa.data)==mes, db.extract('year',Despesa.data)==ano,
        Despesa.descricao.like('[conjunta]%')
    ).scalar() or 0
    resta = 50 - gasto
    estado = "✅ Dentro do orcamento!" if resta >= 0 else f"⚠️ Passaste {abs(resta):.0f}€ da tua parte!"
    msg = f"💑 Conjunta este mes:\n💰 Tua parte: 50€\n🛒 Ja gastaste: {gasto:.2f}€\n💚 Resta: {max(resta,0):.2f}€\n{estado}\n\nPara marcar: 'jantar 30 na conjunta'"
    enviar_mensagem(phone_raw, msg)

# ─── RESUMO ──────────────────────────────────────────────────
def enviar_resumo(phone_raw, usuario, mes_override=None, ano_override=None):
    mes = mes_override or agora().month
    ano = ano_override or agora().year
    receita = db.session.query(db.func.sum(Receita.valor)).filter(
        Receita.usuario_id==usuario.id,
        db.extract('month',Receita.data)==mes, db.extract('year',Receita.data)==ano
    ).scalar() or usuario.salario_liquido or 0

    gastos_pessoais = db.session.query(db.func.sum(Despesa.valor)).filter(
        Despesa.usuario_id==usuario.id,
        db.extract('month',Despesa.data)==mes, db.extract('year',Despesa.data)==ano,
        ~Despesa.descricao.like('[conjunta]%')
    ).scalar() or 0

    gastos_conjunta = db.session.query(db.func.sum(Despesa.valor)).filter(
        Despesa.usuario_id==usuario.id,
        db.extract('month',Despesa.data)==mes, db.extract('year',Despesa.data)==ano,
        Despesa.descricao.like('[conjunta]%')
    ).scalar() or 0

    por_cat = db.session.query(Despesa.categoria, db.func.sum(Despesa.valor)).filter(
        Despesa.usuario_id==usuario.id,
        db.extract('month',Despesa.data)==mes, db.extract('year',Despesa.data)==ano
    ).group_by(Despesa.categoria).all()

    gastar = GASTAR_SUBSIDIO if mes in [6,11] else GASTAR_NORMAL
    disp = gastar - gastos_pessoais
    nomes = ['Janeiro','Fevereiro','Marco','Abril','Maio','Junho','Julho','Agosto','Setembro','Outubro','Novembro','Dezembro']

    msg = f"📊 {nomes[mes-1]}\n\n💰 Receita: {receita:.0f}€\n🛒 Gastos: {gastos_pessoais:.2f}€\n💑 Conjunta: {gastos_conjunta:.2f}€\n💚 Disponivel: {disp:.2f}€\n\n📈 Categorias:"
    for cat, total in sorted(por_cat, key=lambda x:-x[1]):
        msg += f"\n{EMOJI_CAT.get(cat,'💳')} {cat.capitalize()}: {total:.2f}€"

    dia = agora().day
    if dia > 3 and gastos_pessoais > 0 and not mes_override:
        ritmo = gastos_pessoais / dia * 30
        msg += f"\n\n🔮 Ao ritmo atual: ~{ritmo:.0f}€ no fim do mes"

    # Sugestao de poupanca
    if por_cat:
        top = sorted(por_cat, key=lambda x:-x[1])
        cat_top, val_top = top[0]
        if val_top > 50:
            poupanca_ano = round((val_top * 0.3) * 12)
            msg += f"\n\n💡 Se reduzisses {cat_top} em 30%, poupas ~{poupanca_ano}€/ano"

    enviar_mensagem(phone_raw, msg)

def enviar_plano_mes(phone_raw, usuario):
    if not usuario.salario_liquido:
        enviar_mensagem(phone_raw, "Ainda nao sei o teu salario 🤔 Diz: recebi 1300 euros"); return
    enviar_plano_salario(phone_raw, usuario, usuario.salario_liquido)

# ─── SCORE ───────────────────────────────────────────────────
def enviar_score(phone_raw, usuario):
    disp = calcular_disponivel(usuario)
    gastar = GASTAR_SUBSIDIO if agora().month in [6,11] else GASTAR_NORMAL
    pct = (gastar - disp) / gastar * 100 if gastar > 0 else 0
    if pct < 50: score, txt = 9, "Mestre da poupanca! 🏆"
    elif pct < 75: score, txt = 7, "Vais bem, continua! 👍"
    elif pct < 100: score, txt = 5, "Cuidado com os gastos 😬"
    else: score, txt = 2, "Passaste o orcamento 🔴"
    enviar_mensagem(phone_raw, f"⭐ Score: {score}/10\n{txt}")

# ─── MODO TESO ───────────────────────────────────────────────
def modo_teso(phone_raw, usuario):
    disp = calcular_disponivel(usuario)
    dias = max(0, 21 - agora().day) % 30
    msg = (f"😅 Modo teso ativado!\n\n💚 Tens {disp:.2f}€\n📅 ~{dias} dias p/ o salario\n\n"
           f"Dicas:\n🍳 Cozinha em casa, sem take-away\n🚶 Anda a pe quando deres\n"
           f"🛒 So o essencial\n🚫 Evita o {EMOJI_CAT['fastfood']} por uns dias\n💪 Consegues!")
    enviar_mensagem(phone_raw, msg)

# ─── CORRIGIR ────────────────────────────────────────────────
def corrigir_ultimo(phone_raw, usuario, nova_cat):
    ultima = Despesa.query.filter_by(usuario_id=usuario.id).order_by(Despesa.id.desc()).first()
    if not ultima:
        enviar_mensagem(phone_raw, "Nao tenho nenhum gasto p/ corrigir 🤔"); return
    cat_antiga = ultima.categoria
    ultima.categoria = nova_cat; db.session.commit()
    desc = ultima.descricao.replace('[conjunta] ','').lower()
    palavras = [w for w in re.findall(r"[a-zà-ú&']+", desc) if len(w)>1 and w not in ['gastei','paguei','comprei','almocei','euros','euro','no','na','em']]
    aprendido = ''
    if palavras:
        chave = palavras[-1]
        if guardar_aprendida(chave, nova_cat):
            aprendido = f"\n🧠 E aprendi: '{chave}' = {nova_cat.capitalize()} p/ sempre!"
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
                    if preco > 0:
                        todos.append({'nome':p.get('Nome','?'),'marca':p.get('Marca',''),'preco':preco})
                except: pass
        except Exception as e:
            log.error(f'DGEG {idm}: {e}')
    return sorted(todos, key=lambda x:x['preco'])

def gasolina_barata(phone_raw, texto):
    ids=[]; nomes=[]
    for chave, idm in MUNICIPIOS_DGEG.items():
        if chave in texto:
            ids.append(idm); nomes.append(chave.capitalize())
    if not ids: ids=[223,225]; nomes=['Barreiro','Moita']
    postos = buscar_postos_dgeg(ids)
    if not postos:
        enviar_mensagem(phone_raw, "⛽ Nao consegui buscar agora 😕\nhttps://precoscombustiveis.dgeg.gov.pt"); return
    zona = ' e '.join(nomes)
    msg = f"⛽ Gasolina 95 em {zona}:\n\n"
    for i, p in enumerate(postos[:5]):
        marca = f" ({p['marca']})" if p['marca'] else ''
        msg += f"{'🥇🥈🥉4️⃣5️⃣'[i*2:i*2+2]} {p['preco']:.3f}€/L — {p['nome'][:28]}{marca}\n"
    msg += "\n💡 Dados DGEG, hoje!"
    enviar_mensagem(phone_raw, msg)

# ─── DESPESA FUTURA ──────────────────────────────────────────
def processar_despesa_futura(phone_raw, usuario, texto):
    valor = extrair_valor(texto)
    if valor == 0:
        enviar_mensagem(phone_raw, "Quanto vai custar? Ex: mes que vem dentista 40€"); return
    t = texto.lower()
    if 'dentista' in t: desc='Dentista'
    elif 'seguro' in t: desc='Seguro'
    elif 'inspe' in t: desc='Inspecao'
    elif 'renda' in t: desc='Renda'
    else:
        palavras = [w for w in texto.split() if not re.match(r'[0-9€,.]',w) and len(w)>3 and w.lower() not in ['mes','mês','que','vem','tenho']]
        desc = ' '.join(palavras[:2]).capitalize() if palavras else 'Despesa futura'
    meses = 2 if ('2 meses' in t or 'dois meses' in t) else (3 if '3 meses' in t else 1)
    reserva = round(valor/meses, 2)
    db.session.add(DespesaFutura(usuario_id=usuario.id, descricao=desc, valor_total=valor,
        valor_reserva_mensal=reserva, meses=meses, data_prevista=agora().replace(tzinfo=None)+timedelta(days=30*meses)))
    db.session.commit()
    enviar_mensagem(phone_raw, f"📅 Anotado! {desc}: {valor:.0f}€ em {meses} mes{'es' if meses>1 else ''}\nGuardo {reserva:.0f}€/mes p/ isso 👍")

# ─── SIMULAR ─────────────────────────────────────────────────
def simular_compra(phone_raw, usuario, texto):
    valor = extrair_valor(texto)
    disp  = calcular_disponivel(usuario)
    if valor == 0:
        enviar_mensagem(phone_raw, f"💚 Tens {disp:.2f}€ para gastar este mes."); return
    if disp <= 0:
        enviar_mensagem(phone_raw, f"🔴 Nem penses! Ja nao tens orcamento ({disp:.2f}€) 😅"); return
    pct = valor/disp*100
    if pct <= 30:   resp = f"✅ Vai nessa! {valor:.0f}€ e so {pct:.0f}% do disponivel. Ficas com {disp-valor:.0f}€ 🛍️"
    elif pct <= 60: resp = f"🟡 Da, mas pesa. Ficas com {disp-valor:.0f}€. Precisas mesmo?"
    elif pct <= 100: resp = f"🟠 Tecnicamente sim mas ficas quase a zero ({disp-valor:.0f}€). Cuidado!"
    else:            resp = f"🔴 Nao da. Faltam {valor-disp:.0f}€. Deixa p/ o mes que vem 😬"
    enviar_mensagem(phone_raw, resp)

# ─── BOAS VINDAS / AJUDA ─────────────────────────────────────
def enviar_boas_vindas(phone_raw):
    enviar_mensagem(phone_raw, "Ola! 👋 Sou o Ze das Financas 💰\n\nManda os gastos que eu trato:\n• 15 bk / 25 conti / 50 galp\n• foto ou audio do gasto\n• PDF do recibo\n\nDiz 'ajuda' p/ tudo o que sei 😎")

def enviar_ajuda(phone_raw):
    enviar_mensagem(phone_raw, """😎 O Ze das Financas sabe:

💸 Gastos: 15 bk | 25 conti | foto | audio | PDF
📊 Consultas: resumo | plano | quanto tenho | score
💰 Salario: recebi 1300 euros
💑 Conjunta: jantar 30 na conjunta | quanto tenho na conjunta
🎯 Planear: posso comprar X? | mes que vem dentista 40€
⛽ Gasolina: gasolina mais barata no barreiro
🆘 Modo teso: estou teso
🧠 Aprender: aprende que X e roupa | corrige para roupa

Bora poupar! 🚀""")

# ─── IA FALLBACK ─────────────────────────────────────────────
def perguntar_ia(texto, usuario):
    try:
        from groq import Groq
        disp = calcular_disponivel(usuario)
        sys = f"""Es o Ze das Financas, assistente financeiro portugues criado pelo tuga27.
Falas portugues europeu informal, curto e com piada.
Sabes: BK=Burger King, Mac=McDonald's, conti=Continente, PD=Pingo Doce, galp/bp=gasolina, JD=JD Sports.
Saldo disponivel: {disp:.0f}€. Salario: {usuario.salario_liquido or '?'}€.
Responde em max 2 linhas, 1 emoji."""
        resp = Groq(api_key=GROQ_API_KEY).chat.completions.create(
            model='llama-3.3-70b-versatile',
            messages=[{'role':'system','content':sys},{'role':'user','content':texto}],
            max_tokens=150)
        return resp.choices[0].message.content
    except Exception as e:
        log.error(f'IA: {e}'); return "Nao percebi 🤔 Diz 'ajuda' p/ veres o que sei!"

def dia_pagamento_mes(ano, mes):
    """Dia 21, recuando para dia util se fim de semana."""
    d = datetime(ano, mes, 21)
    if d.weekday() == 5: d -= timedelta(days=1)
    elif d.weekday() == 6: d -= timedelta(days=2)
    return d

def dia_recibo_mes(ano, mes):
    """1 dia util antes do dia de pagamento."""
    pag = dia_pagamento_mes(ano, mes)
    d = pag - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d

# ─── LEMBRETES / SCHEDULER ───────────────────────────────────
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
            mes_ant = hoje.month - 1 if hoje.month > 1 else 12
            nomes = ['Janeiro','Fevereiro','Marco','Abril','Maio','Junho','Julho','Agosto','Setembro','Outubro','Novembro','Dezembro']
            for u in Usuario.query.all():
                if u.phone:
                    enviar_mensagem(f"{u.phone}@lid",
                        f"📅 Novo mes financeiro começa hoje!\nO mes de {nomes[mes_ant-1]} ficou para tras.\nDiz 'resumo anterior' p/ veres como correu 📊")

def aviso_meio_mes():
    with app.app_context():
        hoje = agora()
        if hoje.day == 15 and hoje.hour == 10:
            for u in Usuario.query.all():
                if u.phone and u.salario_liquido:
                    disp = calcular_disponivel(u)
                    gastar = GASTAR_SUBSIDIO if hoje.month in [6,11] else GASTAR_NORMAL
                    pct = (gastar - disp) / gastar * 100 if gastar > 0 else 0
                    if pct > 70:
                        enviar_mensagem(f"{u.phone}@lid", f"⚠️ A meio do mes e ja usaste {pct:.0f}% do orcamento!\nAo ritmo atual podes passar o limite. Cuidado nos proximos dias! 💪")

def resumo_semanal():
    with app.app_context():
        if agora().weekday() == 0 and agora().hour == 9:
            for u in Usuario.query.all():
                if u.phone: enviar_resumo(f"{u.phone}@lid", u)

def verificar_despesas_futuras():
    with app.app_context():
        amanha = agora().replace(tzinfo=None) + timedelta(days=1)
        for d in DespesaFutura.query.filter(DespesaFutura.pago==False).all():
            if d.data_prevista and d.data_prevista.date() <= amanha.date():
                u = Usuario.query.get(d.usuario_id)
                if u and u.phone:
                    enviar_mensagem(f"{u.phone}@lid", f"⚠️ Lembrete: {d.descricao} — {d.valor_total:.2f}€ amanha!")

# ─── ARRANQUE ────────────────────────────────────────────────
with app.app_context():
    try: db.create_all()
    except Exception as e: log.warning(f"db.create_all: {e}")
    criar_tabela_aprendizagem()

scheduler.add_job(lembrete_recibo,          'cron', hour=11, minute=0)
scheduler.add_job(lembrete_salario,         'cron', hour=9,  minute=0)
scheduler.add_job(fecho_mes,                'cron', hour=20, minute=0)
scheduler.add_job(aviso_meio_mes,           'cron', hour=10, minute=0)
scheduler.add_job(resumo_semanal,           'cron', hour=9,  minute=30, day_of_week='mon')
scheduler.add_job(verificar_despesas_futuras,'cron', hour=8, minute=0)
scheduler.start()
log.info("Ze das Financas v4 iniciado")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
