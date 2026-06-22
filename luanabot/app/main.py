import os, json, logging, re, base64, tempfile
from datetime import datetime, timedelta
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Lisbon")
except Exception:
    try:
        import pytz
        TZ = pytz.timezone("Europe/Lisbon")
    except Exception:
        TZ = None
from flask import Flask, request, jsonify
from flask import after_this_request
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import text
from models import db, Usuario, Despesa, Receita, DespesaFutura, ObjetivoFinanceiro, FundoEmergencia
from whatsapp import enviar_mensagem as _enviar_mensagem_raw

# Correção automática de acentos nas mensagens (palavras PT comuns)
_ACENTOS_FIX = {
    'Situacao':'Situação','Disponivel':'Disponível','disponivel':'disponível',
    'Poupanca':'Poupança','poupanca':'poupança','financas':'finanças','Financas':'Finanças',
    'aniversario':'aniversário','Aniversario':'Aniversário','salario':'salário','Salario':'Salário',
    'proximo':'próximo','Proximo':'Próximo','maximo':'máximo','Maximo':'Máximo',
    'emergencia':'emergência','historico':'histórico','Historico':'Histórico',
    'credito':'crédito','Credito':'Crédito','sugestoes':'sugestões','Sugestoes':'Sugestões',
    'concluido':'concluído','proxima':'próxima','Atencao':'Atenção','atencao':'atenção',
    'Parabens':'Parabéns','parabens':'parabéns','recibo de vencimento':'recibo de vencimento',
    'Obrigado':'Obrigado','versao':'versão','accao':'ação','accoes':'ações',
}

def enviar_mensagem(phone, texto):
    """Wrapper que corrige acentos comuns antes de enviar."""
    if texto:
        for errado, certo in _ACENTOS_FIX.items():
            if errado in texto:
                texto = texto.replace(errado, certo)
    return _enviar_mensagem_raw(phone, texto)

from claude_ai import processar_mensagem_ia
from pdf_reader import extrair_salario_pdf

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

DASHBOARD_URL = "https://zedasfinancas.netlify.app"

app = Flask(__name__)

@app.after_request
def add_cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, X-Token'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    return response

@app.route('/qr', methods=['GET'])
def qr_facil():
    """Endpoint fixo para reobter o QR do WhatsApp rapidamente, sem curl/SQL.
    Abre isto no browser sempre que o WhatsApp desligar: /qr"""
    import requests as _r
    try:
        # Verificar estado atual
        r_status = _r.get(f"{WAHA_URL}/api/sessions/{WAHA_SESSION}",
                           headers={'X-Api-Key': WAHA_API_KEY}, timeout=10)
        status = r_status.json().get('status', '') if r_status.status_code == 200 else 'DESCONHECIDO'

        if status == 'WORKING':
            return ("<h2>✅ WhatsApp já está ligado!</h2>"
                    "<p>Não precisas de fazer nada. Se mesmo assim quiseres religar, "
                    "<a href='/qr?forcar=1'>clica aqui para forçar novo QR</a>.</p>")

        forcar = request.args.get('forcar', '')
        if status not in ('SCAN_QR_CODE', 'STARTING') or forcar:
            # Sessão morta ou a pedido — apagar e recriar
            _r.delete(f"{WAHA_URL}/api/sessions/{WAHA_SESSION}",
                      headers={'X-Api-Key': WAHA_API_KEY}, timeout=10)
            import time; time.sleep(2)
            _r.post(f"{WAHA_URL}/api/sessions",
                     headers={'X-Api-Key': WAHA_API_KEY, 'Content-Type': 'application/json'},
                     json={'name': WAHA_SESSION, 'start': True}, timeout=15)
            import time; time.sleep(6)

        r_img = _r.get(f"{WAHA_URL}/api/screenshot?session={WAHA_SESSION}",
                        headers={'X-Api-Key': WAHA_API_KEY}, timeout=15)
        if r_img.status_code == 200 and r_img.content[:8].startswith(b'\x89PNG\r\n\x1a\n'):
            import base64 as _b64
            img_b64 = _b64.b64encode(r_img.content).decode()
            return (f"<html><body style='text-align:center;font-family:sans-serif;padding:20px'>"
                    f"<h2>📱 Lê este QR no WhatsApp</h2>"
                    f"<p>Definições → Aparelhos ligados → Ligar aparelho</p>"
                    f"<img src='data:image/png;base64,{img_b64}' style='max-width:350px'><br>"
                    f"<small>Se não aparecer QR, <a href='/qr?forcar=1'>força novo</a> ou "
                    f"<a href='/qr'>atualiza a página</a> (expira em ~20s)</small>"
                    f"</body></html>")
        else:
            return (f"<h2>⏳ A preparar sessão...</h2>"
                    f"<p>Estado: {status}. <a href='/qr'>Atualiza a página</a> em 5 segundos.</p>")
    except Exception as e:
        log.error(f"qr_facil: {e}")
        return f"<h2>Erro: {e}</h2><p><a href='/qr?forcar=1'>Tentar forçar</a></p>"

@app.route('/api/despesa/editar', methods=['POST', 'OPTIONS'])
def api_despesa_editar():
    """Edita uma despesa por ID (descricao, valor, categoria)."""
    if request.method == 'OPTIONS':
        return '', 204
    data = request.get_json() or {}
    phone = data.get('phone', '')
    token = data.get('token', '')
    did = data.get('id')
    if not phone or token != phone[:8] + 'zef':
        return jsonify({'error': 'unauthorized'}), 401
    if not did:
        return jsonify({'error': 'id obrigatorio'}), 400
    try:
        usuario = Usuario.query.filter_by(phone=phone).first()
        if not usuario:
            return jsonify({'error': 'user not found'}), 404
        d = Despesa.query.filter_by(id=did, usuario_id=usuario.id).first()
        if not d:
            return jsonify({'error': 'despesa not found'}), 404
        if data.get('descricao') is not None:
            d.descricao = data['descricao']
        if data.get('valor') is not None:
            d.valor = float(data['valor'])
        if data.get('categoria') is not None:
            d.categoria = data['categoria']
        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        log.error(f"api_despesa_editar: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/despesa/apagar', methods=['POST', 'OPTIONS'])
def api_despesa_apagar():
    """Apaga uma despesa por ID."""
    if request.method == 'OPTIONS':
        return '', 204
    data = request.get_json() or {}
    phone = data.get('phone', '')
    token = data.get('token', '')
    did = data.get('id')
    if not phone or token != phone[:8] + 'zef':
        return jsonify({'error': 'unauthorized'}), 401
    if not did:
        return jsonify({'error': 'id obrigatorio'}), 400
    try:
        usuario = Usuario.query.filter_by(phone=phone).first()
        if not usuario:
            return jsonify({'error': 'user not found'}), 404
        d = Despesa.query.filter_by(id=did, usuario_id=usuario.id).first()
        if not d:
            return jsonify({'error': 'despesa not found'}), 404
        db.session.delete(d)
        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        log.error(f"api_despesa_apagar: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/reset', methods=['POST', 'OPTIONS'])
def api_reset():
    """Reset de dados financeiros do utilizador, mantendo a configuracao de gestao."""
    if request.method == 'OPTIONS':
        return '', 204
    data = request.get_json() or {}
    phone = data.get('phone', '')
    token = data.get('token', '')
    escopo = data.get('escopo', 'valores')  # 'valores' ou 'tudo'
    if not phone or token != phone[:8] + 'zef':
        return jsonify({'error': 'unauthorized'}), 401
    try:
        usuario = Usuario.query.filter_by(phone=phone).first()
        if not usuario:
            return jsonify({'error': 'user not found'}), 404
        uid = usuario.id
        # Tabelas por usuario_id
        tabelas_uid = ['saldos_contas', 'reserva_emergencia', 'dividas_pessoais',
                       'objetivos_poupanca', 'marcos_objetivo', 'recorrentes',
                       'pagamentos_agendados', 'conjunta_depositos', 'aportes_casal',
                       'assinaturas', 'metas_categoria', 'km_combustivel', 'splitting']
        if escopo == 'tudo':
            tabelas_uid += ['wishlist', 'objetivos_casal']
        for t in tabelas_uid:
            try:
                db.session.execute(text(f"DELETE FROM {t} WHERE usuario_id=:u"), {'u': uid})
            except Exception as e:
                log.warning(f"reset {t}: {e}")
        # Tabelas por phone
        for t in ['abastecimentos', 'picos']:
            try:
                db.session.execute(text(f"DELETE FROM {t} WHERE user_phone=:p"), {'p': phone})
            except Exception as e:
                log.warning(f"reset {t}: {e}")
        # Despesas e receitas (modelos ORM)
        try:
            Despesa.query.filter_by(usuario_id=uid).delete()
            Receita.query.filter_by(usuario_id=uid).delete()
            DespesaFutura.query.filter_by(usuario_id=uid).delete()
        except Exception as e:
            log.warning(f"reset despesas/receitas: {e}")
        db.session.commit()
        return jsonify({'ok': True, 'escopo': escopo})
    except Exception as e:
        db.session.rollback()
        log.error(f"api_reset: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/conta', methods=['POST', 'OPTIONS'])
def api_conta():
    """Cria ou atualiza o saldo de uma conta manualmente."""
    if request.method == 'OPTIONS':
        return '', 204
    data = request.get_json() or {}
    phone = data.get('phone', '')
    token = data.get('token', '')
    conta = (data.get('conta') or '').strip()
    valor = data.get('valor')
    if not phone or token != phone[:8] + 'zef':
        return jsonify({'error': 'unauthorized'}), 401
    if not conta or valor is None:
        return jsonify({'error': 'conta e valor obrigatorios'}), 400
    try:
        usuario = Usuario.query.filter_by(phone=phone).first()
        if not usuario:
            return jsonify({'error': 'user not found'}), 404
        db.session.execute(text(
            "INSERT INTO saldos_contas (usuario_id, conta, valor) VALUES (:u, :c, :v) "
            "ON CONFLICT (usuario_id, conta) DO UPDATE SET valor=:v, atualizado_em=NOW()"),
            {'u': usuario.id, 'c': conta, 'v': float(valor)})
        db.session.commit()
        return jsonify({'ok': True, 'conta': conta, 'valor': float(valor)})
    except Exception as e:
        db.session.rollback()
        log.error(f"api_conta: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/pico', methods=['POST', 'OPTIONS'])
def api_pico():
    """Adiciona horas extra (pico) manualmente."""
    if request.method == 'OPTIONS':
        return '', 204
    data = request.get_json() or {}
    phone = data.get('phone', '')
    token = data.get('token', '')
    horas = data.get('horas')
    datap = data.get('data', '')  # opcional YYYY-MM-DD
    if not phone or token != phone[:8] + 'zef':
        return jsonify({'error': 'unauthorized'}), 401
    if horas is None:
        return jsonify({'error': 'horas obrigatorio'}), 400
    try:
        if not datap:
            datap = agora().strftime('%Y-%m-%d')
        db.session.execute(text(
            "INSERT INTO picos (user_phone, data, extra) VALUES (:p, :d, :e)"),
            {'p': phone, 'd': datap, 'e': float(horas)})
        db.session.commit()
        return jsonify({'ok': True, 'horas': float(horas), 'data': datap})
    except Exception as e:
        db.session.rollback()
        log.error(f"api_pico: {e}")
        return jsonify({'error': str(e)}), 500

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
    """Compatibilidade — usa dia 21 com lógica day_before."""
    d = datetime(ano, mes, 21)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d

def dia_pagamento_usuario(usuario, ano=None, mes=None):
    """Calcula dia de pagamento conforme perfil do utilizador."""
    hoje = agora()
    ano = ano or hoje.year; mes = mes or hoje.month
    # Dias de pagamento por utilizador
    from sqlalchemy import text as _text
    dia_pagamento_db = getattr(usuario, 'dia_pagamento', None)
    pagamento_tipo_db = getattr(usuario, 'pagamento_tipo', None)
    if dia_pagamento_db is None and hasattr(usuario, 'id'):
        try:
            row = db.session.execute(_text(
                "SELECT dia_pagamento, pagamento_tipo FROM usuarios WHERE id=:id"),
                {'id': usuario.id}).fetchone()
            if row:
                dia_pagamento_db = row[0]; pagamento_tipo_db = row[1]
        except Exception:
            db.session.rollback()
    if hasattr(usuario, 'phone') and usuario.phone == PHONE_RUBEN:
        dia_base = dia_pagamento_db or 22
        tipo = pagamento_tipo_db or 'exact'
    elif hasattr(usuario, 'phone') and usuario.phone == PHONE_LUANA:
        dia_base = dia_pagamento_db or 21
        tipo = pagamento_tipo_db or 'day_before'
    else:
        dia_base = dia_pagamento_db or 21
        tipo = pagamento_tipo_db or 'day_before'
    try:
        d = datetime(ano, mes, dia_base)
    except ValueError:
        d = datetime(ano, mes, 21)
    if tipo == 'day_before':
        while d.weekday() >= 5:
            d -= timedelta(days=1)
    elif tipo == 'day_after':
        while d.weekday() >= 5:
            d += timedelta(days=1)
    # 'exact' = sem alteração
    return d

def dia_recibo_mes(ano, mes):
    pag = dia_pagamento_mes(ano, mes)
    d = pag - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d

def dias_para_salario(usuario=None):
    hoje = agora()
    if usuario:
        pag = dia_pagamento_usuario(usuario, hoje.year, hoje.month)
        if pag.date() <= hoje.date():
            mes_prox = hoje.month + 1 if hoje.month < 12 else 1
            ano_prox = hoje.year if hoje.month < 12 else hoje.year + 1
            pag = dia_pagamento_usuario(usuario, ano_prox, mes_prox)
    else:
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
    'cafe':'cafe','café':'cafe','cafézinho':'cafe','bica':'cafe','galao':'cafe','galão':'cafe','expresso':'cafe','cappuccino':'cafe','pastelaria':'cafe','kebab':'restaurante',
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
    'cinema':'lazer','nos':'lazer','uci':'lazer','cinemax':'lazer','forum':'lazer','norteshopping':'lazer','colombo':'lazer','dolce vita':'lazer','arrábida shopping':'lazer','amoreiras':'lazer','concerto':'lazer','bowling':'lazer','netflix':'subscricoes',
    'spotify':'subscricoes','disney':'subscricoes',
    # ─── Lojas expandidas (lista completa) ───
    'pizza hut':'fastfood',
    "domino's":'fastfood',
    'taco bell':'fastfood',
    'five guys':'fastfood',
    'pans':'fastfood',
    'pans & company':'fastfood',
    'burger ranch':'fastfood',
    'maccas':'fastfood',
    'kentucky':'fastfood',
    'glovo':'fastfood',
    'uber eats':'fastfood',
    'ubereats':'fastfood',
    'bolt food':'fastfood',
    'takeaway':'fastfood',
    'just eat':'fastfood',
    'h3':'restaurante',
    'honorato':'restaurante',
    'portugualia':'restaurante',
    'portuguália':'restaurante',
    'ramiro':'restaurante',
    'solar dos presuntos':'restaurante',
    'hard rock':'restaurante',
    'italian republic':'restaurante',
    'capricciosa':'restaurante',
    'sabor a lenha':'restaurante',
    'auchan':'supermercado',
    'intermarché':'supermercado',
    'minipreço':'supermercado',
    'froiz':'supermercado',
    'el corte':'supermercado',
    'pull&bear':'roupa',
    'pull bear':'roupa',
    'pb':'roupa',
    'strad':'roupa',
    'massimo':'roupa',
    'massimo dutti':'roupa',
    'c&a':'roupa',
    'kiabi':'roupa',
    'salsa':'roupa',
    'levis':'roupa',
    "levi's":'roupa',
    'tommy':'roupa',
    'tommy hilfiger':'roupa',
    'ralph lauren':'roupa',
    'lacoste':'roupa',
    'zh':'roupa',
    'tshirt':'roupa',
    't-shirt':'roupa',
    'camisa':'roupa',
    'polo':'roupa',
    'hoodie':'roupa',
    'sweat':'roupa',
    'crewneck':'roupa',
    'casaco':'roupa',
    'blusao':'roupa',
    'blusão':'roupa',
    'jeans':'roupa',
    'ganga':'roupa',
    'calcas':'roupa',
    'calças':'roupa',
    'calcoes':'roupa',
    'calções':'roupa',
    'sport zone':'roupa',
    'courir':'roupa',
    'nike store':'roupa',
    'adidas store':'roupa',
    'new balance':'roupa',
    'asics':'roupa',
    'sketchers':'roupa',
    'tenis':'roupa',
    'ténis':'roupa',
    'sneakers':'roupa',
    'sapatilhas':'roupa',
    'sapatos':'roupa',
    'primor':'pessoal',
    'notino':'pessoal',
    'douglas':'pessoal',
    'perfumes & companhia':'pessoal',
    'sephora':'pessoal',
    'wells beauty':'pessoal',
    'equivalenza':'pessoal',
    'druni':'pessoal',
    'perfume':'pessoal',
    'jysk':'casa',
    'homa':'casa',
    'hôma':'casa',
    'conforama':'casa',
    'maxmat':'casa',
    'bricomarche':'casa',
    'bricomarché':'casa',
    'casa shop':'casa',
    'sofa':'casa',
    'sofá':'casa',
    'cama':'casa',
    'colchao':'casa',
    'colchão':'casa',
    'comoda':'casa',
    'cómoda':'casa',
    'secretaria':'casa',
    'secretária':'casa',
    'candeeiro':'casa',
    'espelho':'casa',
    'tapete':'casa',
    'moldura':'casa',
    'pcdiga':'tecnologia',
    'switch technology':'tecnologia',
    'globaldata':'tecnologia',
    'chip7':'tecnologia',
    'mediamarkt':'tecnologia',
    'media markt':'tecnologia',
    'amazon':'tecnologia',
    'monitor':'tecnologia',
    'teclado':'tecnologia',
    'rato':'tecnologia',
    'gpu':'tecnologia',
    'grafica':'tecnologia',
    'gráfica':'tecnologia',
    'processador':'tecnologia',
    'ram':'tecnologia',
    'ssd':'tecnologia',
    'iphone':'tecnologia',
    'ipad':'tecnologia',
    'macbook':'tecnologia',
    'airpods':'tecnologia',
    'apple watch':'tecnologia',
    'playstation store':'tecnologia',
    'ps store':'tecnologia',
    'steam':'tecnologia',
    'epic games':'tecnologia',
    'nintendo':'tecnologia',
    'xbox store':'tecnologia',
    'instant gaming':'tecnologia',
    'cdkeys':'tecnologia',
    'ps5':'tecnologia',
    'ps5 pro':'tecnologia',
    'switch':'tecnologia',
    'alves bandeira':'combustivel',
    'norauto':'carro',
    'midas':'carro',
    'feu vert':'carro',
    'roady':'carro',
    'glassdrive':'carro',
    'carglass':'carro',
    'estacionamento':'carro',
    'parque':'carro',
    'pneus':'carro',
    'revisao':'carro',
    'revisão':'carro',
    'inspecao':'carro',
    'inspeção':'carro',
    'farmacia portuguesa':'saude',
    'cuf':'saude',
    'lusiadas':'saude',
    'lusíadas':'saude',
    'trofa':'saude',
    'hospital da luz':'saude',
    'medicapilar':'saude',
    'tiendanimal':'animais',
    'kiwoko':'animais',
    'pet outlet':'animais',
    'pet city':'animais',
    'mr wonderful':'presentes',
    'odisseias':'presentes',
    'smartbox':'presentes',
    'uber':'carro',
    'bolt':'carro',
    'freenow':'carro',
    'cp':'viagem',
    'fertagus':'viagem',
    'carris':'viagem',
    'metro':'viagem',
    'easyjet':'viagem',
    'edreams':'viagem',
    'skyscanner':'viagem',
    'hotels.com':'viagem',
    'a1':'casa',
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
    'fastfood':'🍔','restaurante':'🍽️','cafe':'☕','roupa':'👗','tecnologia':'📱',
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


def extrair_nome_gasto(texto):
    """Extrai um nome limpo do gasto, sem verbos nem números."""
    t = texto.lower()
    stop = {'gastei','paguei','comprei','almocei','jantei','custou','lanchei','meti','torrei',
            'dei','bebi','tomei','fui','no','na','em','de','da','do','num','numa','uns','umas',
            'uma','um','euros','euro','eur','ontem','hoje','anteontem','€','ao','aos','as','o','a',
            'comer','beber','foram','para','resto','mas','e','com'}
    # Remover datas relativas e dias da semana
    for dia in ['segunda','terca','terça','quarta','quinta','sexta','sabado','sábado','domingo']:
        t = t.replace(dia, '')
    lugares = {'almada','forum','dolce','cascais','lisboa','porto','braga','setubal',
               'faro','coimbra','aveiro','leiria','evora','viseu','guarda','beja',
               'viana','funchal','shopping','mall','center','centre'}
    tokens = [w for w in re.findall(r"[a-zà-ú]+", t)
              if w not in stop and w not in lugares and len(w) > 2]
    if tokens:
        return ' '.join(tokens[:2]).capitalize()
    return 'Gasto'

def categorizar_ia(texto):
    """Fallback IA para categorias desconhecidas — usa Groq Llama 3 8B."""
    try:
        from groq import Groq
        cats_str = ', '.join(CATEGORIAS_VALIDAS)
        resp = Groq(api_key=GROQ_API_KEY).chat.completions.create(
            model='llama-3.1-8b-instant',
            max_tokens=10,
            temperature=0,
            messages=[
                {'role':'system','content':f'Responde APENAS com uma palavra da lista: {cats_str}'},
                {'role':'user','content':f'Categoria financeira de: "{texto}"'}
            ])
        cat = resp.choices[0].message.content.strip().lower()
        # Valida que é uma categoria conhecida
        if cat in CATEGORIAS_VALIDAS:
            # Guarda a palavra-chave mais relevante para aprender
            stop = {'gastei','paguei','comprei','euros','euro','no','na','em','de','da','do','num','uma','uns','umas'}
            tokens = [w for w in re.findall(r"[a-zà-ú]+", texto.lower()) if len(w)>3 and w not in stop]
            if tokens:
                guardar_aprendida(tokens[0], cat)
            return cat, EMOJI_CAT.get(cat,'💳'), extrair_nome_gasto(texto)
    except Exception as e:
        log.error(f"categorizar_ia: {e}")
    return 'outros', '💳', 'Gasto'


FIXO_KEYWORDS = {
    'mae': ['mae','mãe'],
    'carro': ['carro','taigo','prestacao','prestação','seguro carro'],
    'credito1': [('credito','bpi'),('crédito','bpi')],
    'credito2': [('credito','revolut'),('crédito','revolut')],
    'combustivel': ['gasolina','combustivel','combustível','abasteci','galp','bp ','repsol','cepsa'],
    'ordem': ['ordem'],
    'unhas': ['unhas','manicure'],
}

def _fixo_keyword_bate(texto_check, kws):
    """Verifica se as keywords batem — strings normais (substring) ou tuplos (todas as palavras presentes)."""
    for kw in kws:
        if isinstance(kw, tuple):
            if all(palavra in texto_check for palavra in kw):
                return True
        elif kw in texto_check:
            return True
    return False
FIXO_META_KEYS = {'total_fixos','salario','fundo','sobra','gastar','poupanca','modo','subsidio','despesas_mes'}

def verificar_fixos_completos(usuario, p):
    """Verifica se TODOS os fixos do mês já foram pagos. Devolve True só na 1ª vez que completar."""
    mes = agora().month; ano = agora().year
    chaves_fixos = [k for k in p.keys() if k not in FIXO_META_KEYS and p.get(k, 0) > 0]
    if not chaves_fixos:
        return False
    try:
        despesas_mes = db.session.execute(text(
            "SELECT descricao, categoria FROM despesas WHERE usuario_id=:u "
            "AND EXTRACT(month FROM data)=:m AND EXTRACT(year FROM data)=:a"),
            {'u': usuario.id, 'm': mes, 'a': ano}).fetchall()
    except Exception:
        return False
    pagos = set()
    for desc, cat in despesas_mes:
        texto_check = ((desc or '') + ' ' + (cat or '')).lower()
        for chave in chaves_fixos:
            if chave in pagos: continue
            kws = FIXO_KEYWORDS.get(chave, [chave])
            if _fixo_keyword_bate(texto_check, kws):
                pagos.add(chave)
    # Conjunta conta-se separadamente (tabela própria)
    if 'conjunta' in chaves_fixos and 'conjunta' not in pagos:
        try:
            tem_dep = db.session.execute(text(
                "SELECT COUNT(*) FROM conjunta_depositos WHERE usuario_id=:u "
                "AND EXTRACT(month FROM data)=:m AND EXTRACT(year FROM data)=:a"),
                {'u': usuario.id, 'm': mes, 'a': ano}).scalar()
            if tem_dep:
                pagos.add('conjunta')
        except Exception:
            pass
    if pagos != set(chaves_fixos):
        return False
    # Já completou — verificar se é a primeira vez a avisar este mês
    mes_chave = f"{ano}-{mes:02d}"
    try:
        ja_avisado = db.session.execute(text(
            "SELECT dados->>'fixos_completos_mes' FROM estado_utilizador WHERE phone=:p"),
            {'p': usuario.phone}).scalar()
        if ja_avisado == mes_chave:
            return False
        db.session.execute(text(
            "INSERT INTO estado_utilizador (phone, estado, dados, atualizado) "
            "VALUES (:p, 'normal', jsonb_build_object('fixos_completos_mes', :mc), NOW()) "
            "ON CONFLICT (phone) DO UPDATE SET dados = COALESCE(estado_utilizador.dados,'{}'::jsonb) || jsonb_build_object('fixos_completos_mes', :mc)"),
            {'p': usuario.phone, 'mc': mes_chave})
        db.session.commit()
    except Exception as e:
        log.error(f"verificar_fixos_completos marcar: {e}")
        db.session.rollback()
        return False
    return True

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
    # Fallback IA — só quando nada foi reconhecido
    return categorizar_ia(texto)

# ─── BD: TABELAS E HELPERS ───────────────────────────────────
def criar_tabelas():
    sqls = [
        "CREATE TABLE IF NOT EXISTS aprendizagem (chave VARCHAR(100) PRIMARY KEY, categoria VARCHAR(50) NOT NULL)",
        "CREATE TABLE IF NOT EXISTS estado_utilizador (phone VARCHAR(50) PRIMARY KEY, estado VARCHAR(100), dados TEXT, atualizado TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
        "CREATE TABLE IF NOT EXISTS badges (id SERIAL PRIMARY KEY, usuario_id INTEGER, badge VARCHAR(100), obtido_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
        "CREATE TABLE IF NOT EXISTS pessoas_gastos (id SERIAL PRIMARY KEY, usuario_id INTEGER, despesa_id INTEGER, pessoa VARCHAR(100))",
        "CREATE TABLE IF NOT EXISTS reserva_emergencia (usuario_id INTEGER PRIMARY KEY, saldo FLOAT DEFAULT 0, atualizado TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
        "CREATE TABLE IF NOT EXISTS modo_poupanca (usuario_id INTEGER PRIMARY KEY, modo VARCHAR(20) DEFAULT 'equilibrado')",
        "CREATE TABLE IF NOT EXISTS lembretes (id SERIAL PRIMARY KEY, usuario_id INTEGER NOT NULL, texto VARCHAR(300), quando TIMESTAMP, enviado BOOLEAN DEFAULT FALSE, criado_em TIMESTAMP DEFAULT NOW())",
        "CREATE TABLE IF NOT EXISTS saldos_contas (id SERIAL PRIMARY KEY, usuario_id INTEGER NOT NULL, conta VARCHAR(50), valor FLOAT, atualizado_em TIMESTAMP DEFAULT NOW(), UNIQUE(usuario_id, conta))",
        "CREATE TABLE IF NOT EXISTS pagamentos_agendados (id SERIAL PRIMARY KEY, usuario_id INTEGER, nome VARCHAR(100), valor FLOAT, dia_mes INTEGER, prestacoes_total INTEGER DEFAULT 1, prestacoes_pagas INTEGER DEFAULT 0, categoria VARCHAR(50) DEFAULT 'outros', variavel BOOLEAN DEFAULT FALSE, valor_medio FLOAT DEFAULT 0, ativo BOOLEAN DEFAULT TRUE, criado TIMESTAMP DEFAULT NOW())",
        "CREATE TABLE IF NOT EXISTS bancos_ligados (id SERIAL PRIMARY KEY, usuario_id INTEGER, banco VARCHAR(50), requisition_id VARCHAR(100), account_id VARCHAR(100), saldo FLOAT DEFAULT 0, atualizado TIMESTAMP, expira TIMESTAMP, ativo BOOLEAN DEFAULT TRUE)",
        "CREATE TABLE IF NOT EXISTS viagens (id SERIAL PRIMARY KEY, usuario_id INTEGER, nome VARCHAR(100), ativa BOOLEAN DEFAULT TRUE, inicio TIMESTAMP DEFAULT NOW(), fim TIMESTAMP)",
        "CREATE TABLE IF NOT EXISTS objetivos_casal (id SERIAL PRIMARY KEY, descricao VARCHAR(100), valor_objetivo FLOAT, criado_em TIMESTAMP DEFAULT NOW())",
        "CREATE TABLE IF NOT EXISTS aportes_casal (id SERIAL PRIMARY KEY, objetivo_id INTEGER NOT NULL, usuario_id INTEGER NOT NULL, valor FLOAT, data TIMESTAMP DEFAULT NOW())",
        "CREATE TABLE IF NOT EXISTS salarios_pendentes (id SERIAL PRIMARY KEY, usuario_id INTEGER NOT NULL, valor FLOAT, data_pagamento DATE, processado BOOLEAN DEFAULT FALSE, criado_em TIMESTAMP DEFAULT NOW())",
        "CREATE TABLE IF NOT EXISTS abastecimentos (id SERIAL PRIMARY KEY, user_phone VARCHAR(50), data TIMESTAMP DEFAULT NOW(), km_antes FLOAT, km_depois FLOAT, km_percorridos FLOAT, valor FLOAT, litros FLOAT, custo_por_km FLOAT, consumo_l100 FLOAT)",
        "CREATE TABLE IF NOT EXISTS dividas_pessoais (id SERIAL PRIMARY KEY, usuario_id INTEGER NOT NULL, credor VARCHAR(100), saldo FLOAT DEFAULT 0, parcela_mensal FLOAT DEFAULT 0, criado_em TIMESTAMP DEFAULT NOW(), UNIQUE(usuario_id, credor))",
        "CREATE TABLE IF NOT EXISTS abastecimentos (id SERIAL PRIMARY KEY, usuario_id INTEGER NOT NULL, data TIMESTAMP DEFAULT NOW(), km_antes FLOAT, km_depois FLOAT, valor FLOAT, litros FLOAT, km_ganhos FLOAT, custo_por_km FLOAT)",
        "CREATE TABLE IF NOT EXISTS recorrentes (id SERIAL PRIMARY KEY, usuario_id INTEGER NOT NULL, descricao VARCHAR(200), valor FLOAT, criado_em TIMESTAMP DEFAULT NOW(), UNIQUE(usuario_id, descricao))",
        "CREATE TABLE IF NOT EXISTS assinaturas (id SERIAL PRIMARY KEY, usuario_id INTEGER NOT NULL, nome VARCHAR(100), valor FLOAT, criado_em TIMESTAMP DEFAULT NOW())",
        "CREATE TABLE IF NOT EXISTS metas_categoria (id SERIAL PRIMARY KEY, usuario_id INTEGER NOT NULL, categoria VARCHAR(50), limite FLOAT, mes INTEGER, ano INTEGER, UNIQUE(usuario_id, categoria, mes, ano))",
        "CREATE TABLE IF NOT EXISTS conjunta_depositos (id SERIAL PRIMARY KEY, usuario_id INTEGER NOT NULL, valor FLOAT NOT NULL, descricao VARCHAR(200), data TIMESTAMP DEFAULT NOW())",
        "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS dia_pagamento INTEGER DEFAULT 21",
        "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS carro_nome VARCHAR(100) DEFAULT ''",
        "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS carro_consumo_l100 FLOAT DEFAULT 6.0",
        "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS pagamento_tipo VARCHAR(20) DEFAULT 'day_before'",
        "UPDATE usuarios SET dia_pagamento=22, carro_nome='Ibiza 6J', carro_consumo_l100=7.5, pagamento_tipo='day_before' WHERE phone='264909371768998'",
        "UPDATE usuarios SET dia_pagamento=21, carro_nome='Taigo', carro_consumo_l100=5.5, pagamento_tipo='day_before' WHERE phone='84516500680875'",
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
    # Garantir colunas críticas na tabela usuarios (re-tentar individualmente)
    colunas_extra = [
        "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS dia_pagamento INTEGER DEFAULT 21",
        "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS carro_nome VARCHAR(100) DEFAULT ''",
        "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS carro_consumo_l100 FLOAT DEFAULT 6.0",
        "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS pagamento_tipo VARCHAR(20) DEFAULT 'day_before'",
        "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS salario_liquido FLOAT DEFAULT 0",
        "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS ultimo_salario_mes VARCHAR(7) DEFAULT ''",
        "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS fixo_carro FLOAT",
        "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS fixo_ordem FLOAT",
        "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS fixo_unhas FLOAT",
        "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS fixo_conjunta FLOAT",
        "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS fixo_combustivel FLOAT",
        "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS fixo_mae FLOAT",
        "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS fixo_credito1 FLOAT",
        "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS fixo_credito2 FLOAT",
        "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS fixo_carro_fim_mes INTEGER DEFAULT 7",
        "ALTER TABLE objetivos_poupanca ADD COLUMN IF NOT EXISTS data_meta DATE",
        "ALTER TABLE objetivos_poupanca ADD COLUMN IF NOT EXISTS por_mes_sugerido FLOAT",
        "ALTER TABLE despesas ADD COLUMN IF NOT EXISTS tags VARCHAR(200)",
        "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS lembrar_nif BOOLEAN DEFAULT FALSE",
        # Semear valores atuais SO se ainda estiver NULL (nao sobrescreve edicoes futuras)
        "UPDATE usuarios SET fixo_mae=100, fixo_credito1=50, fixo_credito2=50, fixo_carro=200, "
        "fixo_conjunta=50, fixo_combustivel=50, fixo_carro_fim_mes=7 "
        "WHERE phone='264909371768998' AND fixo_mae IS NULL",
        "UPDATE usuarios SET fixo_carro=350, fixo_ordem=20, fixo_unhas=50, fixo_conjunta=50, "
        "fixo_combustivel=50 WHERE phone='84516500680875' AND fixo_carro IS NULL",
    ]
    for sql in colunas_extra:
        try:
            db.session.execute(text(sql)); db.session.commit()
            log.info(f"Coluna OK: {sql[:55]}")
        except Exception as e:
            log.warning(f"coluna: {e}"); db.session.rollback()

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
def get_saldo_divida(usuario_id, credor):
    """Busca saldo de uma dívida pessoal na BD."""
    try:
        r = db.session.execute(text(
            "SELECT saldo, parcela_mensal FROM dividas_pessoais WHERE usuario_id=:u AND LOWER(credor)=LOWER(:c)"),
            {'u': usuario_id, 'c': credor}).fetchone()
        return (float(r[0]), float(r[1])) if r else (0.0, 0.0)
    except Exception:
        db.session.rollback(); return (0.0, 0.0)

def set_saldo_divida(usuario_id, credor, saldo, parcela_mensal=500):
    """Atualiza ou cria saldo de dívida."""
    try:
        db.session.execute(text(
            "INSERT INTO dividas_pessoais (usuario_id, credor, saldo, parcela_mensal) "
            "VALUES (:u,:c,:s,:p) ON CONFLICT (usuario_id, credor) "
            "DO UPDATE SET saldo=:s, parcela_mensal=:p"),
            {'u': usuario_id, 'c': credor, 's': max(0, saldo), 'p': parcela_mensal})
        db.session.commit()
    except Exception as e:
        log.error(f"set_saldo_divida: {e}"); db.session.rollback()


FIXO_NOME_COLUNA = {
    'carro': 'fixo_carro', 'ordem': 'fixo_ordem', 'unhas': 'fixo_unhas',
    'conjunta': 'fixo_conjunta', 'combustivel': 'fixo_combustivel', 'gasolina': 'fixo_combustivel',
    'mae': 'fixo_mae', 'mãe': 'fixo_mae',
    'credito1': 'fixo_credito1', 'creditobpi': 'fixo_credito1', 'bpi': 'fixo_credito1',
    'credito2': 'fixo_credito2', 'creditorevolut': 'fixo_credito2', 'revolut': 'fixo_credito2',
}



def processar_consulta_tag(phone_raw, usuario, tag):
    """Mostra quanto foi gasto numa #tag especifica (ex: #aniversario)."""
    tag_norm = f"#{tag.lower()}"
    try:
        rows = db.session.execute(text(
            "SELECT descricao, valor, data FROM despesas WHERE usuario_id=:u "
            "AND tags ILIKE :tg ORDER BY data DESC"),
            {'u': usuario.id, 'tg': f'%{tag_norm}%'}).fetchall()
    except Exception as e:
        log.error(f"processar_consulta_tag: {e}")
        enviar_mensagem(phone_raw, "Erro 😕"); return

    if not rows:
        enviar_mensagem(phone_raw, f"Não encontrei nada com {tag_norm} 🤔"); return

    total = sum(r[1] for r in rows)
    msg = f"🔖 *{tag_norm}* — {len(rows)} gasto{'s' if len(rows)>1 else ''}\n"
    msg += f"💰 Total: *{total:.2f}€*\n\n"
    for desc_t, val_t, data_t in rows[:10]:
        desc_limpa = re.sub(r'#\w+', '', desc_t).strip()[:40]
        data_fmt = data_t.strftime('%d/%m') if data_t else ''
        msg += f"  • {desc_limpa} — {val_t:.2f}€ ({data_fmt})\n"
    if len(rows) > 10:
        msg += f"\n_+{len(rows)-10} mais antigos_"
    enviar_mensagem(phone_raw, msg)

def processar_dividir_conta(phone_raw, usuario, valor_str, pessoas_str):
    """Divide uma conta (ex: restaurante) por N pessoas e pergunta se regista a tua parte."""
    try:
        valor_total = float(valor_str.replace(',', '.'))
        n_pessoas = int(pessoas_str)
    except Exception:
        enviar_mensagem(phone_raw, "Não entendi os números 😕"); return
    if n_pessoas <= 0:
        enviar_mensagem(phone_raw, "Precisa de ser pelo menos 1 pessoa 😅"); return
    parte = valor_total / n_pessoas
    phone_clean = phone_raw.replace('@lid','').replace('@c.us','').split('@')[0]
    set_estado(phone_clean, 'confirmar_divisao_conta', {'parte': round(parte, 2), 'total': valor_total, 'n': n_pessoas})
    enviar_mensagem(phone_raw,
        f"🧾 *Conta de {valor_total:.2f}€ ÷ {n_pessoas} pessoas*\n\n"
        f"💰 Cada um paga: *{parte:.2f}€*\n\n"
        f"Queres registar a tua parte ({parte:.2f}€) como despesa? Diz *sim* e em quê "
        f"(ex: 'sim jantar') ou *não* se já trataste de outra forma 😊")

def processar_alterar_fixo(phone_raw, usuario, nome_fixo, valor_str):
    """Atualiza um valor fixo do plano mensal direto na BD, sem precisar de deploy."""
    nome_norm = nome_fixo.lower().replace(' ', '')
    coluna = FIXO_NOME_COLUNA.get(nome_norm)
    if not coluna:
        opcoes = ', '.join(sorted(set(FIXO_NOME_COLUNA.keys())))
        enviar_mensagem(phone_raw, f"Não conheço esse fixo 🤔\nOpções: {opcoes}")
        return
    try:
        valor = float(valor_str.replace(',', '.'))
    except Exception:
        enviar_mensagem(phone_raw, "Valor inválido 😕"); return
    try:
        db.session.execute(text(f"UPDATE usuarios SET {coluna}=:v WHERE id=:u"),
                            {'v': valor, 'u': usuario.id})
        db.session.commit()
        enviar_mensagem(phone_raw, f"✅ Fixo *{nome_fixo}* atualizado para {valor:.2f}€!\nJá conta assim no próximo plano 😊")
    except Exception as e:
        db.session.rollback()
        log.error(f"processar_alterar_fixo: {e}")
        enviar_mensagem(phone_raw, "Erro ao atualizar 😕")

def get_fixos_usuario(phone, mes, usuario_id=None):
    """Devolve os fixos mensais do utilizador, lidos da BD (editáveis sem deploy).
    Defaults mantidos como fallback caso a BD ainda não tenha valores."""
    row = None
    try:
        row = db.session.execute(text(
            "SELECT fixo_carro, fixo_ordem, fixo_unhas, fixo_conjunta, fixo_combustivel, "
            "fixo_mae, fixo_credito1, fixo_credito2, fixo_carro_fim_mes "
            "FROM usuarios WHERE phone=:p"), {'p': phone}).fetchone()
    except Exception as e:
        log.error(f"get_fixos_usuario DB: {e}")

    def v(idx, default):
        try:
            return float(row[idx]) if row and row[idx] is not None else default
        except Exception:
            return default

    if phone == PHONE_RUBEN:
        fim_mes_carro = int(row[8]) if row and row[8] is not None else 7
        fixos = {
            'mae':         v(5, 100),
            'credito1':    v(6, 50),
            'credito2':    v(7, 50),
            'carro':       v(0, 200) if mes <= fim_mes_carro else 0,
            'conjunta':    v(3, 50),
            'combustivel': v(4, 50),
        }
        # Dívida à Luana — usa saldo real da BD se disponível
        if usuario_id:
            try:
                saldo, parcela = get_saldo_divida(usuario_id, 'luana')
                if saldo <= 0:
                    r = db.session.execute(text(
                        "SELECT COUNT(*) FROM dividas_pessoais WHERE usuario_id=:u AND LOWER(credor)='luana'"),
                        {'u': usuario_id}).scalar()
                    if r == 0:
                        set_saldo_divida(usuario_id, 'luana', 1720, 500)
                        saldo, parcela = 1720.0, 500.0
                if saldo > 0:
                    fixos['divida_luana'] = min(parcela, saldo)
            except Exception as e:
                log.error(f"get_fixos divida: {e}")
                db.session.rollback()
                fixos['divida_luana'] = 500  # fallback
        return fixos
    else:  # Luana (default)
        return {
            'carro':       v(0, 350),
            'ordem':       v(1, 20),
            'unhas':       v(2, 50),
            'conjunta':    v(3, 50),
            'combustivel': v(4, BASE_COMBUSTIVEL),
        }

def calcular_plano(salario, modo='equilibrado', despesas_futuras_valor=0, phone=None, usuario_id=None):
    mes = agora().month
    fixos = get_fixos_usuario(phone, mes, usuario_id) if phone else get_fixos_usuario(PHONE_LUANA, mes)
    if despesas_futuras_valor > 0:
        fixos['despesas_mes'] = despesas_futuras_valor
    total_fixos = sum(fixos.values())
    fundo = round(salario * FUNDO_PCT, 2)
    sobra = max(salario - total_fixos - fundo, 0)
    m = MODOS_POUPANCA.get(modo, MODOS_POUPANCA[MODO_DEFAULT])
    gastar   = round(sobra * m['gastar_pct'], 2)
    poupanca = round(sobra * m['poupar_pct'], 2)
    meses_subsidio = [6, 11] if phone != PHONE_RUBEN else [6, 12]
    return {**fixos, 'total_fixos':total_fixos, 'salario':salario, 'fundo':fundo, 'sobra':sobra,
            'gastar':gastar, 'poupanca':poupanca, 'modo':modo, 'subsidio':mes in meses_subsidio}

def calcular_disponivel(usuario):
    mes=agora().month; ano=agora().year
    modo = get_modo(usuario.id)
    futuras = db.session.query(db.func.sum(DespesaFutura.valor_reserva_mensal)).filter(
        DespesaFutura.usuario_id==usuario.id, DespesaFutura.pago==False).scalar() or 0
    # Só conta o salário se já foi recebido ESTE mês (senão disponível = 0)
    mes_atual_str = f"{ano}-{mes:02d}"
    try:
        ultimo_mes_real_disp = db.session.execute(text(
            "SELECT ultimo_salario_mes FROM usuarios WHERE id=:u"), {'u': usuario.id}).scalar()
    except Exception:
        ultimo_mes_real_disp = None
    ja_recebeu = ultimo_mes_real_disp == mes_atual_str
    salario_efetivo = (usuario.salario_liquido or 0) if ja_recebeu else 0
    p = calcular_plano(salario_efetivo, modo, futuras, phone=usuario.phone, usuario_id=usuario.id)
    gastos_raw = db.session.query(db.func.sum(Despesa.valor)).filter(
        Despesa.usuario_id==usuario.id,
        db.extract('month',Despesa.data)==mes, db.extract('year',Despesa.data)==ano,
        ~Despesa.descricao.like('[conjunta]%'),
        ~Despesa.descricao.like('[reserva]%'),
    ).scalar() or 0
    # Fixos já deduzidos no plano — só conta o EXCESSO acima do orçamento
    # Ex: gasolina orçamento=50€, gastou 70€ → só conta 20€ no disponível
    fixos_usuario = p  # já tem os fixos calculados
    # Mapa: chave do fixo -> categoria de despesa correspondente
    FIXO_CATEGORIA = {'combustivel': 'combustivel', 'unhas': 'pessoal'}
    desconto_fixos = 0
    for chave_fixo, cat_desp in FIXO_CATEGORIA.items():
        orcamento = fixos_usuario.get(chave_fixo, 0)
        if orcamento <= 0:
            continue
        real = db.session.query(db.func.sum(Despesa.valor)).filter(
            Despesa.usuario_id==usuario.id,
            Despesa.categoria==cat_desp,
            db.extract('month',Despesa.data)==mes, db.extract('year',Despesa.data)==ano,
        ).scalar() or 0
        desconto_fixos += min(real, orcamento)
    gastos = gastos_raw - desconto_fixos
    # Dinheiro extra que entrou este mes ("meti X na conta") soma ao disponivel
    extras = db.session.query(db.func.sum(Receita.valor)).filter(
        Receita.usuario_id==usuario.id,
        Receita.descricao=='Extra',
        db.extract('month',Receita.data)==mes, db.extract('year',Receita.data)==ano,
    ).scalar() or 0
    return p['gastar'] + extras - gastos, p

def gastos_cat_mes(usuario, cat, mes, ano):
    return db.session.query(db.func.sum(Despesa.valor)).filter(
        Despesa.usuario_id==usuario.id, Despesa.categoria==cat,
        db.extract('month',Despesa.data)==mes, db.extract('year',Despesa.data)==ano
    ).scalar() or 0

def extrair_valor(texto):
    # 1. Tentar primeiro valores com € (mais fiável)
    m_euro = re.findall(r'(\d+[.,]\d{2})\s*(?:€|eur)', texto, re.IGNORECASE)
    if m_euro:
        for n in m_euro:
            try:
                v = float(n.replace(',','.'))
                if 0 < v < 100000: return v
            except: continue
    # 2. Tentar padrão X,XX ou X.XX (preço típico) excluindo números muito grandes
    m_preco = re.findall(r'(?<![\d.])(?:\d{1,6}[,.]\d{2})(?![\d])', texto)
    if m_preco:
        for n in m_preco:
            try:
                v = float(n.replace(',','.'))
                if 0 < v < 100000: return v
            except: continue
    # 3. Tentar inteiro com € (pizza 12€)
    m_int_euro = re.findall(r'(\d{1,6})\s*(?:€|eur)', texto, re.IGNORECASE)
    if m_int_euro:
        for n in m_int_euro:
            try:
                v = float(n)
                if 0 < v < 100000: return v
            except: continue
    # 4. Fallback: qualquer número razoável (ignorar códigos de barras > 8 dígitos)
    padrao = re.findall(r'\d[\d.,]*\d|\d+', texto)
    for n in padrao:
        if len(n.replace('.','').replace(',','')) > 8: continue
        try:
            if '.' in n and ',' in n: v = float(n.replace('.','').replace(',','.'))
            elif ',' in n: v = float(n.replace(',','.'))
            elif '.' in n:
                decimais = n[n.rfind('.')+1:]
                v = float(n.replace('.','')) if (len(decimais)==3 and n.replace('.','').isdigit()) else float(n)
            else: v = float(n)
            if 0 < v < 100000: return v
        except: continue
    return 0

def tem_numero(texto):
    return bool(re.search(r'[0-9]+', texto))

def categorizar_sem_ia(texto):
    """Categoriza usando apenas dicionários locais — sem chamada à IA."""
    t = texto.lower()
    aprendidas = carregar_aprendidas()
    for chave, cat in aprendidas.items():
        if chave in t:
            return cat
    tokens = re.findall(r"[a-zà-ú&']+", t)
    for chave, cat in LOJAS.items():
        if ' ' in chave and chave in t:
            return cat
    for tok in tokens:
        if tok in LOJAS:
            return LOJAS[tok]
    for chave, cat in LOJAS.items():
        if ' ' not in chave and len(chave) > 3 and chave in t:
            return cat
    return 'outros'

def eh_gasto(texto):
    t = texto.lower()
    verbos = ['gastei','paguei','comprei','almocei','jantei','custou','abasteci','lanchei','fui ao','fui à',
              'deixei','torrei','dei','fui no','fui na','comi no','comi na','bebi','tomei','larguei','arrotei',
              'voaram','sairam','saíram','desapareceram','estourei','rebentei','queimei','derreti','mandei',
              'foi-se','custou-me','ficou-me','atestei']
    # "meti" só é gasto se não for na conta
    if 'meti' in t and not any(p in t for p in ['na conta','no banco','no bpi','no revolut','na minha']):
        verbos.append('meti')
    if any(v in t for v in verbos): return True
    if '€' in t or ' euro' in t or 'euros' in t: return True
    # Usa só dicionário local — a IA fica para quando JÁ sabemos que é gasto
    return categorizar_sem_ia(texto) != 'outros'

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
                    enviar_mensagem(phone_raw, f'🎤 Percebi: "{transcrito}"')
                    # Se parece gasto, perguntar pessoal ou conjunta
                    if extrair_valor(transcrito) > 0:
                        set_estado(phone, 'confirmar_pessoal_conjunta', {'texto': transcrito})
                        enviar_mensagem(phone_raw, "É *pessoal* ou *conjunta*?")
                        return jsonify({'status':'ok'})
                    texto = transcrito
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
                        # Perguntar pessoal ou conjunta antes de registar
                        if resultado and extrair_valor(resultado) > 0:
                            set_estado(phone, 'confirmar_pessoal_conjunta', {'texto': resultado})
                            enviar_mensagem(phone_raw,
                                f"📸 Vi: _{resultado}_\n\n"
                                f"É *pessoal* ou *conjunta*?\n_Diz_ *ignora* _para cancelar_")
                            return jsonify({'status':'ok'})
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




@app.route('/api/webhook/recibo', methods=['POST'])
def webhook_recibo():
    """Chamado pelo Google Apps Script quando chega recibo de vencimento."""
    data = request.get_json(silent=True) or {}

    # Aceita chamadas sem token (do Google Apps Script legado) ou com token
    token = data.get('token','')
    phone = data.get('phone', PHONE_RUBEN)
    if token:
        expected = (phone[:8] + 'zef') if phone else ''
        if token != expected:
            return jsonify({'error':'unauthorized'}), 401

    salario = float(data.get('salario', 0))
    mensagem_gs = data.get('mensagem','')  # mensagem já formatada pelo Apps Script
    mes = data.get('mes','')

    # Dados de horas extras do Apps Script
    horas_50  = float(data.get('horas_50', 0))
    valor_50  = float(data.get('valor_50', 0))
    horas_75  = float(data.get('horas_75', 0))
    valor_75  = float(data.get('valor_75', 0))
    horas_dom = float(data.get('horas_domingos', 0))
    valor_dom = float(data.get('valor_domingos', 0))
    horas_fer = float(data.get('horas_feriados', 0))
    valor_fer = float(data.get('valor_feriados', 0))
    total_h   = float(data.get('total_horas', 0))
    total_v   = float(data.get('valor_total_horas', 0))

    try:
        usuario = Usuario.query.filter_by(phone=phone).first()
        if not usuario:
            return jsonify({'error':'user not found'}), 404

        phone_raw = phone + '@lid'

        if salario <= 0:
            enviar_mensagem(phone_raw,
                "📄 Chegou um recibo mas não consegui ler o valor.\n"
                "Envia-me o PDF no WhatsApp 📎")
            return jsonify({'status':'ok'})

        # Montar mensagem rica
        msg = f"📄 Chegou o teu recibo!\n\n"
        msg += f"💰 Líquido: *{salario:.2f}€*"
        if mes:
            msg += f" — {mes}"
        msg += "\n"

        if total_h > 0:
            msg += f"\n⏱️ Horas extra: {total_h:.1f}h → {total_v:.2f}€\n"
            if horas_75 > 0:  msg += f"   • {horas_75:.1f}h a 75% → {valor_75:.2f}€\n"
            if horas_50 > 0:  msg += f"   • {horas_50:.1f}h a 50% → {valor_50:.2f}€\n"
            if horas_fer > 0: msg += f"   • {horas_fer:.1f}h feriados → {valor_fer:.2f}€\n"
            if horas_dom > 0: msg += f"   • {horas_dom:.1f}h domingos → {valor_dom:.2f}€\n"

        # Pagamento = próximo dia útil após o recibo
        dp = proximo_dia_util(agora().date())
        guardar_salario_pendente(usuario.id, salario, dp)
        msg += f"\n📅 O dinheiro entra dia {dp.strftime('%d/%m')} — eu aviso e mando o plano! 😊"

        enviar_mensagem(phone_raw, msg)

        # Guardar o salário no perfil para o plano
        if not usuario.salario_liquido or abs(usuario.salario_liquido - salario) > 50:
            usuario.salario_liquido = salario
            db.session.commit()
            log.info(f"Salário atualizado para {phone}: {salario}€")

        log.info(f"Recibo processado para {phone}: {salario}€, extras: {total_h}h")
        return jsonify({'status':'ok','salario':salario,'mensagem_enviada':True})

    except Exception as e:
        log.error(f"webhook_recibo: {e}")
        return jsonify({'error':str(e)}), 500



@app.route('/api/debug', methods=['GET'])
def api_debug():
    """Diagnóstico — testa funções problemáticas e mostra erros reais."""
    token = request.args.get('token','')
    phone = request.args.get('phone', PHONE_RUBEN)
    if token != (phone[:8] + 'zef'):
        return jsonify({'error':'unauthorized'}), 401
    resultados = {}
    import traceback

    usuario = Usuario.query.filter_by(phone=phone).first()
    if not usuario:
        return jsonify({'error':'user not found'}), 404

    # Teste 1: tabelas existem?
    for tabela in ['abastecimentos','salarios_pendentes','lembretes','saldos_contas','metas_categoria','dividas_pessoais','picos']:
        try:
            n = db.session.execute(text(f"SELECT COUNT(*) FROM {tabela}")).scalar()
            resultados[f'tabela_{tabela}'] = f'OK ({n} linhas)'
        except Exception as e:
            db.session.rollback()
            resultados[f'tabela_{tabela}'] = f'ERRO: {type(e).__name__}: {str(e)[:100]}'

    # Teste 2: calcular_disponivel
    try:
        disp, p = calcular_disponivel(usuario)
        resultados['calcular_disponivel'] = f'OK (disp={disp:.2f})'
    except Exception as e:
        resultados['calcular_disponivel'] = f'ERRO: {type(e).__name__}: {str(e)[:150]}'

    # Teste 3: constantes viagem
    try:
        resultados['DISTANCIAS_MOITA'] = f'OK ({len(DISTANCIAS_MOITA)} destinos)'
        resultados['PORTAGENS_POR_KM'] = f'OK ({PORTAGENS_POR_KM})'
    except Exception as e:
        resultados['constantes_viagem'] = f'ERRO: {e}'

    # Teste 4: get_reserva
    try:
        r = get_reserva(usuario.id)
        resultados['get_reserva'] = f'OK ({r:.2f})'
    except Exception as e:
        resultados['get_reserva'] = f'ERRO: {type(e).__name__}: {str(e)[:100]}'

    # Teste 5: id_para_codigo
    try:
        resultados['id_para_codigo'] = f'OK ({id_para_codigo(127)})'
    except Exception as e:
        resultados['id_para_codigo'] = f'ERRO: {e}'

    # Teste 6: enviar_mensagem (wrapper de acentos) — SEM enviar de verdade
    try:
        teste_txt = "Situacao teste 100€"
        for errado, certo in _ACENTOS_FIX.items():
            if errado in teste_txt:
                teste_txt = teste_txt.replace(errado, certo)
        resultados['wrapper_acentos'] = f'OK ({teste_txt})'
    except Exception as e:
        resultados['wrapper_acentos'] = f'ERRO: {type(e).__name__}: {str(e)[:100]}'

    # Teste 7: chamar calcular_viagem de verdade (captura o erro real)
    try:
        import io, contextlib
        # Simular sem enviar - chamar a lógica
        t_v = "vamos ao algarve".lower().replace('ã','a').replace('é','e').replace('í','i').replace('ó','o')
        km_v = None
        for cidade, dist in DISTANCIAS_MOITA.items():
            if cidade in t_v:
                km_v = dist; break
        consumo_v = get_consumo_carro(usuario)
        litros_v = km_v * 2 * consumo_v / 100
        total_v = litros_v * 1.75 + km_v * 2 * PORTAGENS_POR_KM * 0.7
        resultados['logica_viagem'] = f'OK (algarve={total_v:.0f}EUR)'
    except Exception as e:
        resultados['logica_viagem'] = f'ERRO: {type(e).__name__}: {str(e)[:150]}'

    # Teste 8: Despesa INSERT (o que o abastecimento faz)
    try:
        d_teste = Despesa(usuario_id=usuario.id, valor=0.01, categoria='combustivel',
            descricao='TESTE DEBUG', data=agora().replace(tzinfo=None))
        db.session.add(d_teste)
        db.session.commit()
        # Apagar logo
        db.session.execute(text("DELETE FROM despesas WHERE descricao='TESTE DEBUG'"))
        db.session.commit()
        resultados['inserir_despesa'] = 'OK'
    except Exception as e:
        db.session.rollback()
        resultados['inserir_despesa'] = f'ERRO: {type(e).__name__}: {str(e)[:150]}'

    # Teste 9: carro_consumo_l100 existe?
    try:
        resultados['carro_consumo'] = f'OK ({get_consumo_carro(usuario)})'
    except Exception as e:
        resultados['carro_consumo'] = f'ERRO: {type(e).__name__}'

    return jsonify(resultados)

@app.route('/api/sync-saldos', methods=['GET'])
def api_sync_saldos():
    """Força atualização dos saldos reais (Revolut via Enable Banking).
    enable_atualizar_saldos() grava em bancos_ligados; replicamos para saldos_contas."""
    token = request.args.get('token','')
    phone = request.args.get('phone','')
    expected = (phone[:8] + 'zef') if phone else ''
    if not token or token != expected:
        return jsonify({'error':'unauthorized'}), 401
    try:
        usuario = Usuario.query.filter_by(phone=phone).first()
        if not usuario:
            return jsonify({'error':'not found'}), 404
        try:
            resultados = enable_atualizar_saldos(usuario, silencioso=True)
        except Exception as e:
            log.error(f"sync-saldos enable {phone}: {e}")
            resultados = []
        NOMES_CONTA_DISPLAY = {
            'revolut_pessoal':'Revolut','revolut_conjunta':'Conta Conjunta',
            'revolut_cofre_casa':'Cofre Casa','revolut_cofre_pc':'Cofre PC novo','revolut':'Revolut',
        }
        for banco, saldo in (resultados or []):
            try:
                nome_display = NOMES_CONTA_DISPLAY.get(banco, banco.replace('_',' ').title())
                db.session.execute(text(
                    "INSERT INTO saldos_contas (usuario_id, conta, valor, atualizado_em) VALUES (:u,:c,:v,NOW()) "
                    "ON CONFLICT (usuario_id, conta) DO UPDATE SET valor=:v, atualizado_em=NOW()"),
                    {'u': usuario.id, 'c': nome_display, 'v': float(saldo)})
                db.session.commit()
            except Exception as e:
                log.error(f"sync-saldos ponte {banco}: {e}"); db.session.rollback()
        saldos = db.session.execute(text(
            "SELECT conta, valor FROM saldos_contas WHERE usuario_id=:u ORDER BY valor DESC"),
            {'u': usuario.id}).fetchall()
        return jsonify({'ok': True, 'sincronizados': len(resultados or []), 'contas': [{'conta': s[0], 'valor': round(float(s[1] or 0), 2)} for s in saldos]})
    except Exception as e:
        log.error(f"sync-saldos {phone}: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/saude', methods=['GET'])
def api_saude():
    """Score de saúde financeira + patrimônio para o dashboard."""
    token = request.args.get('token','')
    phone = request.args.get('phone','')
    expected = (phone[:8] + 'zef') if phone else ''
    if not token or token != expected:
        return jsonify({'error':'unauthorized'}), 401
    try:
        usuario = Usuario.query.filter_by(phone=phone).first()
        if not usuario:
            return jsonify({'error':'not found'}), 404
        mes = agora().month; ano = agora().year
        salario = usuario.salario_liquido or 0
        disp, p = calcular_disponivel(usuario)
        gasto_mes = db.session.query(db.func.sum(Despesa.valor)).filter(
            Despesa.usuario_id==usuario.id,
            db.extract('month',Despesa.data)==mes, db.extract('year',Despesa.data)==ano,
            ~Despesa.descricao.like('[conjunta]%'), ~Despesa.descricao.like('[reserva]%')).scalar() or 0
        reserva = get_reserva(usuario.id)

        # Score
        score = 100
        if salario > 0:
            pct = gasto_mes/salario*100
            if pct > 90: score -= 30
            elif pct > 70: score -= 15
        if reserva < 500: score -= 20
        elif reserva < 1500: score -= 10
        if disp < 0: score -= 15
        score = max(0, min(100, score))

        # Saldos por conta
        saldos = db.session.execute(text(
            "SELECT conta, valor FROM saldos_contas WHERE usuario_id=:u ORDER BY valor DESC"),
            {'u': usuario.id}).fetchall()
        patrimonio = sum(s[1] for s in saldos)

                # Construir lista de contas incluindo conjunta e reserva
        contas_lista = [{'conta': s[0], 'valor': round(s[1], 2)} for s in saldos]
        ja_tem_conjunta = any('conjunta' in c['conta'].lower() for c in contas_lista)
        if not ja_tem_conjunta:
            try:
                saldo_conj = db.session.execute(text(
                    "SELECT COALESCE(SUM(valor),0) FROM conjunta_depositos WHERE usuario_id=:u"),
                    {'u': usuario.id}).scalar() or 0
                contas_lista.append({'conta': 'Conta Conjunta', 'valor': round(float(saldo_conj), 2), 'tipo': 'conjunta'})
            except Exception as e:
                log.warning(f"saldo conjunta api: {e}")
        ja_tem_reserva = any('reserva' in c['conta'].lower() for c in contas_lista)
        if not ja_tem_reserva:
            contas_lista.append({'conta': 'Reserva de Emergência', 'valor': round(float(reserva), 2), 'tipo': 'reserva'})

        return jsonify({
            'score': score,
            'disponivel': round(disp, 2),
            'gasto_mes': round(gasto_mes, 2),
            'reserva': round(reserva, 2),
            'salario': round(salario, 2),
            'poupanca_prevista': round(p.get('poupanca', 0), 2),
            'patrimonio': round(patrimonio, 2),
            'contas': contas_lista
        })
    except Exception as e:
        log.error(f"api_saude: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/relatorio', methods=['GET'])

def gerar_pdf_relatorio(usuario, mes, ano):
    """Gera PDF do relatório mensal com reportlab (já instalado via pdfplumber)."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.lib import colors
    import io

    nomes_mes = ['Janeiro','Fevereiro','Marco','Abril','Maio','Junho',
                 'Julho','Agosto','Setembro','Outubro','Novembro','Dezembro']
    nome = NOMES_CASAL.get(usuario.phone, 'Utilizador')
    mes_nome = nomes_mes[mes-1]

    gastos = db.session.execute(text(
        "SELECT categoria, SUM(valor) FROM despesas "
        "WHERE usuario_id=:u AND EXTRACT(month FROM data)=:m AND EXTRACT(year FROM data)=:y "
        "AND descricao NOT LIKE '[conjunta]%%' GROUP BY categoria ORDER BY SUM(valor) DESC"),
        {'u':usuario.id,'m':mes,'y':ano}).fetchall()
    total_gastos = sum(r[1] for r in gastos) or 0
    receita = db.session.execute(text(
        "SELECT COALESCE(SUM(valor),0) FROM receitas WHERE usuario_id=:u "
        "AND EXTRACT(month FROM data)=:m AND EXTRACT(year FROM data)=:y"),
        {'u':usuario.id,'m':mes,'y':ano}).scalar() or 0
    try: reserva = get_reserva(usuario.id)
    except: reserva = 0
    _, p = calcular_disponivel(usuario)
    poupanca = p.get('poupanca', 0)
    saldo = receita - total_gastos

    NOME_CAT = {'fastfood':'Fast Food','restaurante':'Restaurante','cafe':'Cafe','roupa':'Roupa',
        'tecnologia':'Tecnologia','supermercado':'Supermercado','combustivel':'Combustivel',
        'saude':'Saude','pessoal':'Pessoal','carro':'Carro','lazer':'Lazer','casa':'Casa',
        'subscricoes':'Subscricoes','viagem':'Viagem','gota':'Bebidas','outros':'Outros'}
    CORES_HEX = {'roupa':'#4b9fff','fastfood':'#ff7a45','combustivel':'#ffb84d',
        'supermercado':'#3ddc84','cafe':'#b87cff','restaurante':'#ff5e5e',
        'tecnologia':'#4b9fff','carro':'#789099','casa':'#3ddc84','outros':'#8a9aa8'}

    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)
    w, h = A4

    # Cabeçalho escuro
    c.setFillColor(colors.HexColor('#141922'))
    c.rect(0, h-80, w, 80, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 22)
    c.drawCentredString(w/2, h-38, "Ze das Financas")
    c.setFont("Helvetica", 12)
    c.setFillColor(colors.HexColor('#b4bec8'))
    c.drawCentredString(w/2, h-60, f"Relatorio de {mes_nome} {ano} - {nome}")

    # KPIs em 4 caixas
    kpi_data = [("Receita", f"{receita:.0f}EUR", '#ebf5ff'),
                ("Gastos",  f"{total_gastos:.0f}EUR", '#fff0eb'),
                ("Poupanca",f"{poupanca:.0f}EUR", '#ebffed'),
                ("Reserva", f"{reserva:.0f}EUR", '#fffaeb')]
    kpi_w, kpi_h, kpi_y = 120, 55, h-160
    for i, (label, val, cor) in enumerate(kpi_data):
        kx = 25 + i * 135
        c.setFillColor(colors.HexColor(cor))
        c.roundRect(kx, kpi_y, kpi_w, kpi_h, 8, fill=1, stroke=0)
        c.setFillColor(colors.HexColor('#555'))
        c.setFont("Helvetica", 9)
        c.drawCentredString(kx + kpi_w/2, kpi_y + 38, label)
        c.setFillColor(colors.HexColor('#111'))
        c.setFont("Helvetica-Bold", 14)
        c.drawCentredString(kx + kpi_w/2, kpi_y + 18, val)

    # Saldo
    saldo_y = h - 185
    c.setFont("Helvetica-Bold", 13)
    c.setFillColor(colors.HexColor('#3db46e') if saldo >= 0 else colors.HexColor('#dc3c3c'))
    c.drawCentredString(w/2, saldo_y, f"Saldo do mes: {'+' if saldo>=0 else ''}{saldo:.0f}EUR")

    # Título categorias
    c.setFillColor(colors.HexColor('#1e1e1e'))
    c.setFont("Helvetica-Bold", 14)
    c.drawString(30, h-220, "Para onde foi o dinheiro")

    # Linha separadora
    c.setStrokeColor(colors.HexColor('#e0e0e0'))
    c.setLineWidth(0.5)
    c.line(30, h-228, w-30, h-228)

    y = h - 248
    for cat, valor in gastos[:10]:
        if y < 80: break
        pct = round(valor/total_gastos*100) if total_gastos > 0 else 0
        nome_cat = NOME_CAT.get(cat, cat.capitalize())
        cor_hex = CORES_HEX.get(cat, '#8a9aa8')
        # Nome cat
        c.setFillColor(colors.HexColor('#333'))
        c.setFont("Helvetica", 11)
        c.drawString(30, y, nome_cat)
        # Valor
        c.setFont("Helvetica-Bold", 11)
        c.drawString(155, y, f"{valor:.0f}EUR")
        # Barra
        bar_x, bar_w, bar_h = 220, 270, 9
        c.setFillColor(colors.HexColor('#ebebeb'))
        c.roundRect(bar_x, y-1, bar_w, bar_h, 3, fill=1, stroke=0)
        if pct > 0:
            c.setFillColor(colors.HexColor(cor_hex))
            c.roundRect(bar_x, y-1, bar_w*pct/100, bar_h, 3, fill=1, stroke=0)
        # %
        c.setFillColor(colors.HexColor('#999'))
        c.setFont("Helvetica", 9)
        c.drawString(bar_x + bar_w + 5, y, f"{pct}%")
        y -= 22

    if not gastos:
        c.setFillColor(colors.HexColor('#999'))
        c.setFont("Helvetica", 11)
        c.drawCentredString(w/2, h-260, "Sem gastos registados este mes")

    # Rodapé
    c.setFillColor(colors.HexColor('#141922'))
    c.rect(0, 0, w, 35, fill=1, stroke=0)
    c.setFillColor(colors.HexColor('#8a9aa8'))
    c.setFont("Helvetica", 9)
    c.drawCentredString(w/2, 13, "Ze das Financas  *  zedasfinancas.netlify.app")

    c.save()
    return buf.getvalue()

def api_relatorio():
    """Gera relatorio mensal em HTML ou PDF (formato=pdf)."""
    token = request.args.get('token','')
    phone = request.args.get('phone','')
    expected = (phone[:8] + 'zef') if phone else ''
    if not token or token != expected:
        return jsonify({'error':'unauthorized'}), 401
    mes_p = request.args.get('mes')
    ano_p = request.args.get('ano')
    formato = request.args.get('formato', 'html')
    hoje = agora()
    mes = int(mes_p) if mes_p else hoje.month
    ano = int(ano_p) if ano_p else hoje.year
    # PDF?
    if formato == 'pdf':
        try:
            usuario = Usuario.query.filter_by(phone=phone).first()
            if not usuario:
                return jsonify({'error':'not found'}), 404
            pdf_bytes = gerar_pdf_relatorio(usuario, mes, ano)
            nomes_m = ['Jan','Fev','Mar','Abr','Mai','Jun','Jul','Ago','Set','Out','Nov','Dez']
            fname = f"relatorio_{nomes_m[mes-1]}_{ano}.pdf"
            return pdf_bytes, 200, {
                'Content-Type': 'application/pdf',
                'Content-Disposition': f'inline; filename="{fname}"'}
        except Exception as e:
            log.error(f"api_relatorio pdf: {e}")
            return jsonify({'error': str(e)}), 500
    try:
        usuario = Usuario.query.filter_by(phone=phone).first()
        if not usuario:
            return jsonify({'error':'not found'}), 404
        nomes_mes = ['Janeiro','Fevereiro','Março','Abril','Maio','Junho',
                     'Julho','Agosto','Setembro','Outubro','Novembro','Dezembro']
        # Dados do mês
        gastos = db.session.execute(text(
            "SELECT categoria, SUM(valor) as total FROM despesas "
            "WHERE usuario_id=:u AND EXTRACT(month FROM data)=:m AND EXTRACT(year FROM data)=:y "
            "AND descricao NOT LIKE '[conjunta]%%' "
            "GROUP BY categoria ORDER BY total DESC"),
            {'u':usuario.id,'m':mes,'y':ano}).fetchall()
        total_gastos = sum(r[1] for r in gastos)
        receitas = db.session.execute(text(
            "SELECT SUM(valor) FROM receitas WHERE usuario_id=:u "
            "AND EXTRACT(month FROM data)=:m AND EXTRACT(year FROM data)=:y"),
            {'u':usuario.id,'m':mes,'y':ano}).scalar() or 0
        EMOJI_CAT_R = {'fastfood':'🍔','restaurante':'🍽️','roupa':'👗','supermercado':'🛒',
            'combustivel':'⛽','saude':'💊','lazer':'🎭','outros':'💳','carro':'🚗',
            'tecnologia':'📱','subscricoes':'📺','pessoal':'💅','casa':'🏠','viagem':'✈️'}
        cats_html = ''.join([
            f'<tr><td>{EMOJI_CAT_R.get(r[0],"💳")} {r[0].capitalize()}</td>'
            f'<td style="text-align:right">{r[1]:.2f}€</td>'
            f'<td style="text-align:right;color:#999">{round(r[1]/total_gastos*100) if total_gastos else 0}%</td></tr>'
            for r in gastos
        ])
        html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
        <title>Relatório {nomes_mes[mes-1]} {ano}</title>
        <style>body{{font-family:Arial,sans-serif;max-width:600px;margin:40px auto;color:#222;padding:20px}}
        h1{{color:#5a8ff0;font-size:22px}}h2{{font-size:15px;color:#666;font-weight:normal;margin-bottom:24px}}
        .kpi{{display:flex;gap:16px;margin:20px 0}}.kpi-box{{flex:1;background:#f5f7ff;border-radius:8px;padding:14px;text-align:center}}
        .kpi-label{{font-size:11px;color:#888;text-transform:uppercase}}.kpi-val{{font-size:20px;font-weight:700;color:#5a8ff0}}
        table{{width:100%;border-collapse:collapse;margin:16px 0}}td{{padding:8px 6px;border-bottom:1px solid #eee}}
        .footer{{color:#aaa;font-size:11px;margin-top:32px;text-align:center}}</style></head>
        <body><h1>Relatório Financeiro</h1><h2>{nomes_mes[mes-1]} {ano} · {usuario.nome or phone}</h2>
        <div class="kpi">
          <div class="kpi-box"><div class="kpi-label">Receita</div><div class="kpi-val">{receitas:.0f}€</div></div>
          <div class="kpi-box"><div class="kpi-label">Gastos</div><div class="kpi-val" style="color:#f05a5a">{total_gastos:.2f}€</div></div>
          <div class="kpi-box"><div class="kpi-label">Saldo</div><div class="kpi-val" style="color:{'#22d37a' if receitas-total_gastos>=0 else '#f05a5a'}">{receitas-total_gastos:.0f}€</div></div>
        </div>
        <table><tr><th style="text-align:left">Categoria</th><th style="text-align:right">Total</th><th style="text-align:right">%</th></tr>
        {cats_html}</table>
        <div class="footer">Zé das Finanças · {hoje.strftime('%d/%m/%Y')}</div>
        </body></html>"""
        return html, 200, {'Content-Type':'text/html; charset=utf-8'}
    except Exception as e:
        log.error(f"api_relatorio: {e}")
        return jsonify({'error':str(e)}), 500

@app.route('/api/gasto', methods=['POST'])
def api_gasto():
    """Endpoint para iOS Shortcut / Apple Pay."""
    data = request.get_json(silent=True) or {}
    token = data.get('token') or request.headers.get('X-Token','')
    phone = data.get('phone','')
    expected = (phone[:8] + 'zef') if phone else ''
    if not token or token != expected:
        return jsonify({'error':'unauthorized'}), 401
    valor = float(data.get('valor', 0))
    descricao = data.get('descricao', 'Apple Pay')
    if valor <= 0:
        return jsonify({'error':'valor invalido'}), 400
    try:
        usuario = Usuario.query.filter_by(phone=phone).first()
        if not usuario:
            return jsonify({'error':'utilizador nao encontrado'}), 404
        cat, emoji, nome_loja = categorizar(descricao)
        despesa = Despesa(
            usuario_id=usuario.id, valor=valor, categoria=cat,
            descricao=f'[ApplePay] {nome_loja}',
            data=agora().replace(tzinfo=None)
        )
        db.session.add(despesa); db.session.commit()
        disp, _ = calcular_disponivel(usuario)
        return jsonify({
            'status': 'ok',
            'gasto': valor,
            'categoria': cat,
            'disponivel': round(disp, 2),
            'mensagem': f'{emoji} {nome_loja} {valor:.2f}€ registado'
        })
    except Exception as e:
        log.error(f"api_gasto: {e}"); db.session.rollback()
        return jsonify({'error': str(e)}), 500

@app.route('/api/picos', methods=['GET'])
def api_picos():
    """API para horas extras do utilizador."""
    token = request.args.get('token') or request.headers.get('X-Token','')
    phone = request.args.get('phone','')
    expected = (phone[:8] + 'zef') if phone else ''
    if not token or token != expected:
        return jsonify({'error':'unauthorized'}), 401
    mes_param = request.args.get('mes')
    ano_param = request.args.get('ano')
    import calendar
    hoje = agora()
    mes = int(mes_param) if mes_param else hoje.month
    ano = int(ano_param) if ano_param else hoje.year
    _, ultimo_dia = calendar.monthrange(ano, mes)
    try:
        rows = db.session.execute(text("""
            SELECT data, entrada, saida, horas_trabalhadas, horas_extra, dia_folga
            FROM picos
            WHERE user_phone=:p AND data>=:ini AND data<=:fim
            ORDER BY data ASC
        """), {'p': phone, 'ini': f"{ano}-{mes:02d}-01", 'fim': f"{ano}-{mes:02d}-{ultimo_dia:02d}"}).fetchall()
        dias = []
        total_extra = 0
        for r in rows:
            dias.append({
                'data': r[0].strftime('%d/%m'),
                'entrada': r[1].strftime('%H:%M') if r[1] else None,
                'saida': r[2].strftime('%H:%M') if r[2] else None,
                'horas': round(float(r[3] or 0), 2),
                'extra': round(float(r[4] or 0), 2),
                'folga': bool(r[5])
            })
            total_extra += float(r[4] or 0)
        return jsonify({
            'mes': mes, 'ano': ano,
            'dias': dias,
            'total_extra': round(total_extra, 2),
            'dias_com_extra': len([d for d in dias if d['extra'] > 0])
        })
    except Exception as e:
        log.error(f"api_picos: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/', methods=['GET'])
def health():
    return jsonify({'status':'ok','bot':'Ze das Financas v7'})

# ─── API DASHBOARD ───────────────────────────────────────────
@app.route('/api/banco/organizar', methods=['GET'])
def api_banco_organizar():
    """Limpa duplicados e renomeia contas com os nomes corretos."""
    if request.args.get('t') != 'zef2026':
        return jsonify({'error': 'acesso negado'}), 401
    try:
        # 1. Apagar duplicados — manter só os IDs certos
        # Manter: id=1 (pessoal EUR), id=6 (cofre Casa), id=7 (cofre PC), id=8 (conjunta EUR)
        # Apagar: todos os outros
        db.session.execute(text("DELETE FROM bancos_ligados WHERE id NOT IN (1, 6, 7, 8)"))
        # 2. Renomear com nomes claros
        updates = [
            (1, 'revolut_pessoal'),
            (8, 'revolut_conjunta'),
            (6, 'revolut_cofre_casa'),
            (7, 'revolut_cofre_pc'),
        ]
        for bid, nome in updates:
            db.session.execute(text("UPDATE bancos_ligados SET banco=:b, ativo=TRUE WHERE id=:i"),
                {'b': nome, 'i': bid})
        db.session.commit()
        # Ver o que ficou
        restantes = db.session.execute(text(
            "SELECT id, banco, account_id, saldo FROM bancos_ligados ORDER BY id")).fetchall()
        return jsonify({'ok': True, 'contas': [
            {'id':r[0],'banco':r[1],'account_id':r[2][:8]+'...','saldo':r[3]} for r in restantes
        ]})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@app.route('/api/banco/limpar', methods=['GET'])
def api_banco_limpar():
    """Remove entradas duplicadas da tabela bancos_ligados."""
    if request.args.get('t') != 'zef2026':
        return jsonify({'error': 'acesso negado'}), 401
    try:
        # Manter só a entrada mais recente por account_id
        db.session.execute(text("""
            DELETE FROM bancos_ligados
            WHERE id NOT IN (
                SELECT MAX(id) FROM bancos_ligados
                WHERE account_id IS NOT NULL
                GROUP BY usuario_id, account_id
            ) AND account_id IS NOT NULL
        """))
        # Apagar entradas sem account_id e inativas
        db.session.execute(text(
            "DELETE FROM bancos_ligados WHERE account_id IS NULL AND ativo=FALSE"))
        db.session.commit()
        # Ver o que ficou
        restantes = db.session.execute(text(
            "SELECT id, banco, account_id, saldo, ativo FROM bancos_ligados ORDER BY id")).fetchall()
        return jsonify({
            'ok': True,
            'restantes': [{'id':r[0],'banco':r[1],'account_id':r[2],'saldo':r[3],'ativo':r[4]} for r in restantes]
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@app.route('/api/banco/identificar', methods=['GET'])
def api_banco_identificar():
    """Tenta identificar o tipo de cada conta ligada."""
    if request.args.get('t') != 'zef2026':
        return jsonify({'error': 'acesso negado'}), 401
    import requests as _r
    headers = _enable_headers()
    resultado = []
    try:
        bancos = db.session.execute(text(
            "SELECT id, banco, account_id, saldo FROM bancos_ligados WHERE ativo=TRUE AND account_id IS NOT NULL ORDER BY id")).fetchall()
        for bid, banco, acc_id, saldo in bancos:
            info = {'id': bid, 'banco': banco, 'account_id': acc_id, 'saldo_guardado': saldo}
            if headers:
                # Tentar /details
                for endpoint in ['details', 'balances']:
                    try:
                        r = _r.get(f"{ENABLE_BASE}/accounts/{acc_id}/{endpoint}",
                            headers=headers, timeout=10)
                        if r.status_code == 200:
                            info[f'dados_{endpoint}'] = r.json()
                    except Exception as e:
                        info[f'erro_{endpoint}'] = str(e)
            resultado.append(info)
        return jsonify(resultado)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/banco/estado', methods=['GET'])
def api_banco_estado():
    """Ver estado bruto da tabela bancos_ligados."""
    if request.args.get('t') != 'zef2026':
        return jsonify({'error': 'acesso negado'}), 401
    rows = db.session.execute(text(
        "SELECT id, usuario_id, banco, requisition_id, account_id, saldo, atualizado, ativo, expira FROM bancos_ligados ORDER BY id DESC LIMIT 10")).fetchall()
    return jsonify([{
        'id': r[0], 'usuario_id': r[1], 'banco': r[2],
        'requisition_id': (r[3] or '')[:20] + '...' if r[3] else None,
        'account_id': r[4],
        'saldo': r[5], 'atualizado': str(r[6]) if r[6] else None,
        'ativo': r[7], 'expira': str(r[8]) if r[8] else None
    } for r in rows])

@app.route('/api/banco/contas', methods=['GET'])
def api_banco_contas():
    """Ver todas as contas/subcontas ligadas (incluindo cofres Revolut)."""
    token_acesso = request.args.get('t','')
    if token_acesso != 'zef2026':
        return jsonify({'error': 'acesso negado'}), 401
    import requests as _r
    headers = _enable_headers()
    if not headers:
        return jsonify({'error': 'Enable Banking não configurado'}), 500
    try:
        bancos = db.session.execute(text(
            "SELECT id, banco, account_id, saldo, atualizado FROM bancos_ligados WHERE ativo=TRUE AND account_id IS NOT NULL")).fetchall()
        resultado = []
        for bid, banco, acc_id, saldo, atualizado in bancos:
            info = {'banco': banco, 'account_id': acc_id, 'saldo': saldo, 'atualizado': str(atualizado) if atualizado else None}
            # Tentar obter detalhes via API (pode falhar)
            try:
                r = _r.get(f"{ENABLE_BASE}/accounts/{acc_id}/balances", headers=headers, timeout=10)
                if r.status_code == 200:
                    bals = r.json().get('balances', [])
                    if bals:
                        info['saldo_api'] = float(bals[0].get('balance_amount', {}).get('amount', 0))
                        info['tipo_saldo'] = bals[0].get('balance_type','')
            except Exception:
                pass
            # Tentar transações recentes para ver cofres
            try:
                r2 = _r.get(f"{ENABLE_BASE}/accounts/{acc_id}/transactions",
                    headers=headers, params={'limit': 5}, timeout=10)
                if r2.status_code == 200:
                    txs = r2.json().get('transactions', [])
                    info['ultimas_transacoes'] = [{
                        'desc': tx.get('remittance_information','') or tx.get('creditor_name',''),
                        'valor': tx.get('transaction_amount',{}).get('amount',''),
                        'data': tx.get('booking_date','')
                    } for tx in txs[:3]]
            except Exception:
                pass
            resultado.append(info)
        return jsonify({'contas': resultado, 'total': len(resultado), 'nota': 'saldo=da BD, saldo_api=ao vivo'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/banco/debug', methods=['GET'])
def api_banco_debug():
    """Diagnóstico do Enable Banking — ver o estado das credenciais."""
    # Verificar token de acesso simples
    token_acesso = request.args.get('t','')
    if token_acesso != 'zef2026':
        return jsonify({'error': 'acesso negado'}), 401
    resultado = {}
    # 1. Variáveis de ambiente
    app_id = os.environ.get('ENABLE_APP_ID', '')
    priv_key_raw = os.environ.get('ENABLE_PRIVATE_KEY', '')
    resultado['app_id'] = app_id[:8] + '...' if app_id else 'NÃO DEFINIDO'
    resultado['priv_key_len'] = len(priv_key_raw)
    resultado['priv_key_inicio'] = priv_key_raw[:30] if priv_key_raw else 'NÃO DEFINIDO'
    resultado['tem_begin'] = 'BEGIN PRIVATE KEY' in priv_key_raw
    resultado['tem_backslash_n'] = '\\n' in priv_key_raw
    resultado['tem_newline_real'] = '\n' in priv_key_raw and '\\n' not in priv_key_raw
    # 2. Tentar normalizar
    if priv_key_raw:
        try:
            chave = _normalizar_chave_pem(priv_key_raw)
            resultado['normalizacao'] = 'OK'
            resultado['chave_linhas'] = chave.count('\n')
        except Exception as e:
            resultado['normalizacao'] = f'ERRO: {e}'
    # 3. Tentar JWT
    try:
        jwt_token = _enable_jwt()
        resultado['jwt'] = 'OK' if jwt_token else 'FALHOU (app_id ou chave em falta)'
    except Exception as e:
        resultado['jwt'] = f'ERRO: {e}'
    # 4. Testar API
    if resultado.get('jwt') == 'OK':
        try:
            import requests as _r
            r = _r.get('https://api.enablebanking.com/aspsps?country=PT',
                headers=_enable_headers(), timeout=10)
            resultado['api_status'] = r.status_code
            if r.status_code == 200:
                resultado['bancos_pt'] = len(r.json().get('aspsps', []))
        except Exception as e:
            resultado['api_erro'] = str(e)
    return jsonify(resultado)

@app.route('/api/banco/callback', methods=['GET'])
def api_banco_callback():
    """Callback do Enable Banking — recebe o code e cria a sessão."""
    code = request.args.get('code', '')
    state = request.args.get('state', '')
    if not code:
        return "<h2>Autorização cancelada ou falhou.</h2>", 400
    try:
        # state = "usuarioid_banco_timestamp"
        usuario_id = int(state.split('_')[0]) if state else None
        sessao = enable_criar_sessao(code)
        if not sessao or 'accounts' not in sessao:
            return "<h2>Não consegui criar a sessão. Tenta de novo.</h2>", 500
        accounts = sessao.get('accounts', [])
        session_id = sessao.get('session_id', '')
        log.info(f"Enable callback: sessao={sessao}, accounts={accounts}, usuario_id={usuario_id}")
        # Normalizar accounts — pode vir como lista de strings ou lista de dicts com uid/id/resourceId
        def _get_uid(acc):
            if isinstance(acc, str): return acc
            return acc.get('uid') or acc.get('id') or acc.get('resourceId') or acc.get('account_id') or str(acc)
        # Se accounts vazio, guardar session_id e tentar buscar contas
        session_id = sessao.get('session_id') or sessao.get('id') or ''
        if not accounts and usuario_id and session_id:
            try:
                import requests as _r3
                _h3 = _enable_headers()
                if _h3:
                    # Enable Banking: GET /accounts?session_id=<id>
                    for endpoint in [
                        f"{ENABLE_BASE}/accounts?session_id={session_id}",
                        f"{ENABLE_BASE}/sessions/{session_id}",
                        f"{ENABLE_BASE}/sessions/{session_id}/accounts",
                    ]:
                        r_acc = _r3.get(endpoint, headers=_h3, timeout=20)
                        log.info(f"Enable accounts try {endpoint}: {r_acc.status_code} {r_acc.text[:200]}")
                        if r_acc.status_code == 200:
                            acc_data = r_acc.json()
                            # Pode vir como lista, dict com 'accounts', ou dict com dados da sessão
                            if isinstance(acc_data, list):
                                accounts = acc_data
                            elif 'accounts' in acc_data:
                                accounts = acc_data['accounts']
                            elif 'uid' in acc_data or 'id' in acc_data:
                                # É a conta em si
                                accounts = [acc_data]
                            if accounts:
                                break
            except Exception as e:
                log.error(f"Enable accounts fallback: {e}")
        # Se ainda sem accounts, guardar session_id para tentar mais tarde
        if not accounts and usuario_id and session_id:
            try:
                db.session.execute(text(
                    "UPDATE bancos_ligados SET requisition_id=:s "
                    "WHERE id=(SELECT id FROM bancos_ligados WHERE usuario_id=:u ORDER BY id DESC LIMIT 1)"),
                    {'s': session_id, 'u': usuario_id})
                db.session.commit()
                log.info(f"Enable: session_id guardado para busca posterior: {session_id}")
            except Exception as e:
                log.error(f"Enable: guardar session_id: {e}")
        if accounts and usuario_id:
            u = Usuario.query.get(usuario_id)
            banco_base = db.session.execute(text(
                "SELECT banco FROM bancos_ligados WHERE usuario_id=:u ORDER BY id DESC LIMIT 1"),
                {'u': usuario_id}).scalar() or 'revolut'
            acc_uid_0 = _get_uid(accounts[0])
            log.info(f"Enable callback: guardando account_id={acc_uid_0} banco={banco_base}")
            db.session.execute(text(
                "UPDATE bancos_ligados SET account_id=:a, ativo=TRUE "
                "WHERE id = (SELECT id FROM bancos_ligados WHERE usuario_id=:u ORDER BY id DESC LIMIT 1)"),
                {'a': acc_uid_0, 'u': usuario_id})
            # Contas adicionais: inserir como novas linhas
            # Tentar identificar tipo de conta via API
            def _tipo_conta(uid, idx, banco):
                tipos = {0: banco, 1: f"{banco}_conjunta"}
                return tipos.get(idx, f"{banco}_{idx}")

            for i, acc in enumerate(accounts[1:], 1):
                acc_uid_i = acc.get('uid') if isinstance(acc, dict) else acc
                # Tentar ler o nome real da conta via API
                nome_tipo = f"{banco_base}_{i}"
                try:
                    import requests as _r2
                    _h2 = _enable_headers()
                    if _h2:
                        r_info = _r2.get(f"{ENABLE_BASE}/accounts/{acc_uid_i}/details",
                            headers=_h2, timeout=10)
                        if r_info.status_code == 200:
                            info = r_info.json()
                            nome_real = (info.get('name') or info.get('details') or
                                        info.get('product') or '').lower()
                            if 'joint' in nome_real or 'conjunta' in nome_real or 'shared' in nome_real:
                                nome_tipo = f"{banco_base}_conjunta"
                            elif 'vault' in nome_real or 'savings' in nome_real or 'poupança' in nome_real:
                                nome_tipo = f"{banco_base}_cofre_{i}"
                            elif nome_real:
                                nome_tipo = f"{banco_base}_{nome_real[:20].replace(' ','_')}"
                            else:
                                # Fallback: conjunta é normalmente a 2ª conta
                                nome_tipo = f"{banco_base}_conjunta" if i == 1 else f"{banco_base}_{i}"
                except Exception:
                    nome_tipo = f"{banco_base}_conjunta" if i == 1 else f"{banco_base}_{i}"
                # Verificar se já existe
                existe = db.session.execute(text(
                    "SELECT 1 FROM bancos_ligados WHERE usuario_id=:u AND account_id=:a"),
                    {'u': usuario_id, 'a': acc_uid_i}).fetchone()
                if not existe:
                    db.session.execute(text(
                        "INSERT INTO bancos_ligados (usuario_id, banco, account_id, ativo, expira) "
                        "SELECT :u, :b, :a, TRUE, expira FROM bancos_ligados "
                        "WHERE usuario_id=:u ORDER BY id DESC LIMIT 1"),
                        {'u': usuario_id, 'b': nome_tipo, 'a': acc_uid_i})
            db.session.commit()
            n_contas = len(accounts)
            if u:
                tipos = ['principal'] + ['conjunta' if i==1 else f'extra {i}' for i in range(1, n_contas)]
                lista = '\n'.join(f"  • {t}" for t in tipos)
                enviar_mensagem(f"{u.phone}@lid",
                    f"✅ *Banco ligado com sucesso!* ({n_contas} conta{'s' if n_contas>1 else ''})\n\n{lista}\n\nDiz *saldos reais* para ver 🏦")
        return "<h2>✅ Banco ligado! Podes voltar ao WhatsApp e dizer 'saldos reais'.</h2>", 200
    except Exception as e:
        log.error(f"banco_callback: {e}"); db.session.rollback()
        return f"<h2>Erro: {e}</h2>", 500

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
        try:
            rec_m = db.session.execute(text(
                "SELECT COALESCE(SUM(valor),0) FROM receitas WHERE usuario_id=:u "
                "AND EXTRACT(month FROM data)=:m AND EXTRACT(year FROM data)=:y"),
                {'u':usuario.id,'m':m,'y':y}).scalar() or 0
        except Exception:
            rec_m = 0
        historico.append({'mes': nomes[m-1], 'total': round(total, 2), 'receitas': round(float(rec_m), 2)})

    # Disponível
    modo = get_modo(usuario.id)
    futuras = DespesaFutura.query.filter(DespesaFutura.usuario_id==usuario.id, DespesaFutura.pago==False).all()
    total_fut = sum(d.valor_reserva_mensal for d in futuras)
    mes_atual_str = f"{ano}-{mes:02d}"
    ja_recebeu_flag = getattr(usuario, 'ultimo_salario_mes', '') == mes_atual_str
    # Cross-check robusto: se já há receitas lançadas este mês que cobrem o salário
    # (ex: registado como "Extra" em vez de "Salario"), conta como recebido mesmo sem a flag.
    try:
        _receitas_check = db.session.execute(text(
            "SELECT COALESCE(SUM(valor),0) FROM receitas WHERE usuario_id=:u "
            "AND EXTRACT(month FROM data)=:m AND EXTRACT(year FROM data)=:y"),
            {'u':usuario.id,'m':mes,'y':ano}).scalar() or 0
        _receitas_check = float(_receitas_check)
    except Exception:
        _receitas_check = 0.0
    sal_liq = usuario.salario_liquido or 0
    ja_recebeu = ja_recebeu_flag or (sal_liq > 0 and _receitas_check >= sal_liq * 0.9)
    salario_efetivo = sal_liq if ja_recebeu else 0
    p = calcular_plano(salario_efetivo, modo, total_fut, phone=usuario.phone, usuario_id=usuario.id)
    gastos_mes = sum(v for _, v in por_cat)
    disp = p['gastar'] - gastos_mes
    reserva = get_reserva(usuario.id)

    # Despesas fixas previstas (sempre, independente de salário) — inclui dívida à Luana (usuario_id necessário)
    p_fixos = calcular_plano(usuario.salario_liquido or 0, modo, total_fut, phone=usuario.phone, usuario_id=usuario.id)
    NOMES_FIXOS = {
        'mae':'Mãe','credito1':'Crédito 1','credito2':'Crédito 2','carro':'Carro',
        'conjunta':'Conjunta','combustivel':'Combustível','divida_luana':'Dívida à Luana',
        'ordem':'Ordem','unhas':'Unhas','despesas_mes':'Despesas previstas',
    }
    chaves_excluir = {'total_fixos','salario','fundo','sobra','gastar','poupanca','modo','subsidio'}
    fixos_lista = [{'nome': NOMES_FIXOS.get(k, k.replace('_',' ').capitalize()), 'valor': round(v, 2)}
                   for k, v in p_fixos.items() if k not in chaves_excluir and isinstance(v,(int,float)) and v]

    # ─── RECEBIDO / A RECEBER / PAGO / A PAGAR (valores REAIS da BD) ───
    recebido_mes = round(_receitas_check, 2)  # já calculado acima (cross-check do salário)
    try:
        sal_pend = db.session.execute(text(
            "SELECT COALESCE(SUM(valor),0) FROM salarios_pendentes WHERE usuario_id=:u AND processado=FALSE"),
            {'u':usuario.id}).scalar() or 0
    except Exception:
        sal_pend = 0
    try:
        split_receber = db.session.execute(text(
            "SELECT COALESCE(SUM(valor_cada),0) FROM splitting WHERE usuario_id=:u AND pago=FALSE"),
            {'u':usuario.id}).scalar() or 0
    except Exception:
        split_receber = 0
    a_receber = round(float(sal_pend) + float(split_receber), 2)
    # Só soma o salário a "a receber" se realmente ainda não entrou nada que o cubra
    # (evita duplicar quando o salário já foi lançado por outra via, ex: "dinheiro extra")
    if not ja_recebeu and sal_liq > 0 and sal_pend == 0:
        falta_receber = max(0, sal_liq - recebido_mes)
        a_receber = round(a_receber + falta_receber, 2)
    pago_mes = round(float(gastos_mes), 2)
    try:
        a_pagar = db.session.execute(text(
            "SELECT COALESCE(SUM(COALESCE(valor,valor_medio,0)),0) FROM pagamentos_agendados "
            "WHERE usuario_id=:u AND ativo=TRUE AND prestacoes_pagas < prestacoes_total"),
            {'u':usuario.id}).scalar() or 0
        a_pagar = round(float(a_pagar), 2)
    except Exception:
        a_pagar = 0.0
    receitas_total = round(recebido_mes + a_receber, 2)
    try:
        saldo_bancario = db.session.execute(text(
            "SELECT COALESCE(SUM(valor),0) FROM saldos_contas WHERE usuario_id=:u"),
            {'u':usuario.id}).scalar() or 0
        saldo_bancario = round(float(saldo_bancario), 2)
    except Exception:
        saldo_bancario = 0.0

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
        "SELECT descricao, valor_objetivo, valor_atual, data_meta FROM objetivos_poupanca WHERE usuario_id=:id AND concluido=FALSE"),
        {'id':usuario.id}).fetchall()

    # Dívidas pessoais
    try:
        dividas_rows = db.session.execute(text(
            "SELECT credor, saldo, parcela_mensal FROM dividas_pessoais WHERE usuario_id=:id AND saldo>0"),
            {'id':usuario.id}).fetchall()
    except Exception:
        dividas_rows = []

    # Compromissos / pagamentos agendados
    try:
        compromissos = db.session.execute(text(
            "SELECT nome, COALESCE(valor, valor_medio, 0), dia_mes, categoria, prestacoes_pagas, prestacoes_total FROM pagamentos_agendados "
            "WHERE usuario_id=:id AND ativo=TRUE ORDER BY dia_mes ASC"),
            {'id':usuario.id}).fetchall()
    except Exception:
        compromissos = []

    # Próximos aniversários (60 dias)
    try:
        hoje_d = agora().date()
        em_60 = hoje_d + timedelta(days=60)
        aniv_rows = db.session.execute(text("""
            SELECT nome, data_aniv FROM aniversarios WHERE usuario_id=:id
            AND ((EXTRACT(month FROM data_aniv)=:m1 AND EXTRACT(day FROM data_aniv)>=:d1)
                OR (EXTRACT(month FROM data_aniv)=:m2 AND EXTRACT(day FROM data_aniv)<=:d2))
            """), {'id':usuario.id,'m1':hoje_d.month,'d1':hoje_d.day,'m2':em_60.month,'d2':em_60.day}).fetchall()
        aniversarios = []
        for nome, data_aniv in aniv_rows:
            prox = data_aniv.replace(year=hoje_d.year)
            if prox < hoje_d: prox = prox.replace(year=hoje_d.year+1)
            aniversarios.append({'nome': nome, 'data': prox.strftime('%d/%m'), 'dias': (prox-hoje_d).days})
        aniversarios.sort(key=lambda a:a['dias'])
    except Exception:
        aniversarios = []

    # Previsão de fim de mês
    previsao = None
    try:
        import calendar as _cal
        _, ult_dia = _cal.monthrange(ano, mes)
        dia_atual = agora().day if (mes==agora().month and ano==agora().year) else ult_dia
        if dia_atual > 0 and gastos_mes > 0:
            ritmo_diario = gastos_mes / dia_atual
            previsao_total = ritmo_diario * ult_dia
            sobra_prevista = p['gastar'] - previsao_total
            previsao = {'dia_atual': dia_atual, 'ultimo_dia': ult_dia, 'ritmo_diario': round(ritmo_diario, 2),
                        'gasto_projetado': round(previsao_total, 2), 'sobra_prevista': round(sobra_prevista, 2),
                        'no_caminho': sobra_prevista >= 0}
    except Exception:
        previsao = None

    # Combustível
    combustivel = None
    try:
        ab_rows = db.session.execute(text(
            "SELECT data, km_percorridos, valor, custo_por_km FROM abastecimentos "
            "WHERE user_phone=:p ORDER BY data DESC LIMIT 5"), {'p': usuario.phone}).fetchall()
        if ab_rows:
            st = db.session.execute(text(
                "SELECT COALESCE(SUM(km_percorridos),0), COALESCE(SUM(valor),0), COALESCE(AVG(custo_por_km),0), COUNT(*) "
                "FROM abastecimentos WHERE user_phone=:p"), {'p': usuario.phone}).fetchone()
            combustivel = {'total_km': round(float(st[0] or 0)), 'total_eur': round(float(st[1] or 0), 2),
                'custo_100km': round(float(st[2] or 0) * 100, 2), 'n': int(st[3] or 0),
                'ultimos': [{'data': r[0].strftime('%d/%m') if r[0] else '—', 'km': round(float(r[1] or 0)), 'valor': round(float(r[2] or 0), 2)} for r in ab_rows]}
    except Exception:
        combustivel = None

    # Transações recentes (últimos 30 registos)
    transacoes = db.session.execute(text(
        "SELECT descricao, valor, categoria, data, id FROM despesas WHERE usuario_id=:id ORDER BY data DESC LIMIT 30"),
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
        'objetivos': [{'desc': r[0], 'objetivo': r[1], 'atual': r[2], 'pct': round(r[2]/r[1]*100 if r[1] else 0),
                       'dias_falta': (r[3] - agora().replace(tzinfo=None).date()).days if r[3] else None} for r in objetivos],
        'dividas': [{'nome': r[0].capitalize(), 'pessoa': r[0].capitalize(), 'valor': round(float(r[1] or 0), 2), 'parcela': round(float(r[2] or 0), 2), 'tipo': 'devo'} for r in dividas_rows],
        'compromissos': [{'nome': r[0], 'valor': round(float(r[1] or 0), 2), 'dia': r[2], 'dia_mes': r[2], 'cat': r[3], 'pago': (r[4] or 0) >= (r[5] or 1)} for r in compromissos],
        'aniversarios': aniversarios,
        'fixos': fixos_lista,
        'total_fixos': round(p_fixos.get('total_fixos',0), 2),
        'ja_recebeu_salario': ja_recebeu,
        'receitas_mes': receitas_total,
        'recebido': recebido_mes,
        'a_receber': a_receber,
        'pago': pago_mes,
        'a_pagar': a_pagar,
        'saldo_bancario': saldo_bancario,
        'previsao': previsao,
        'combustivel': combustivel,
        'dias_salario': dias_para_salario(usuario),
        'transacoes': [{'desc': r[0], 'valor': round(r[1],2), 'cat': r[2], 'data': r[3].strftime('%d/%m %H:%M') if r[3] else '', 'id': r[4]} for r in transacoes],
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
    m = re.search(r"(\d{1,2})(?:[h:,\.](\d{1,2}))?", texto.strip().lower())
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
        return "Não encontrei entrada de hoje.\nRegista primeiro: entrei 9h"
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


# ─── COMO ESTOU? — Dashboard natural ─────────────────────────
def como_estou(phone_raw, usuario):
    mes = agora().month; ano = agora().year
    nomes_m = ['Janeiro','Fevereiro','Março','Abril','Maio','Junho','Julho',
               'Agosto','Setembro','Outubro','Novembro','Dezembro']

    disp, p = calcular_disponivel(usuario)
    salario = usuario.salario_liquido or 0

    # Gastos totais do mês
    gasto_mes = db.session.query(db.func.sum(Despesa.valor)).filter(
        Despesa.usuario_id==usuario.id,
        db.extract('month',Despesa.data)==mes, db.extract('year',Despesa.data)==ano,
        ~Despesa.descricao.like('[conjunta]%'),
        ~Despesa.descricao.like('[reserva]%'),
    ).scalar() or 0

    # Gastos mês anterior (para comparação)
    mes_ant = mes - 1 if mes > 1 else 12
    ano_ant = ano if mes > 1 else ano - 1
    gasto_mes_ant = db.session.query(db.func.sum(Despesa.valor)).filter(
        Despesa.usuario_id==usuario.id,
        db.extract('month',Despesa.data)==mes_ant, db.extract('year',Despesa.data)==ano_ant,
        ~Despesa.descricao.like('[conjunta]%'),
    ).scalar() or 0

    # Categorias deste mês
    cats = db.session.execute(text(
        "SELECT categoria, SUM(valor) as total FROM despesas "
        "WHERE usuario_id=:u AND EXTRACT(month FROM data)=:m AND EXTRACT(year FROM data)=:y "
        "AND descricao NOT LIKE '[conjunta]%%' AND descricao NOT LIKE '[reserva]%%' "
        "GROUP BY categoria ORDER BY total DESC LIMIT 5"),
        {'u':usuario.id,'m':mes,'y':ano}).fetchall()

    # Reserva e poupança
    reserva = get_reserva(usuario.id)
    poupanca_prev = p.get('poupanca', 0)

    # Dívidas pendentes (a receber)
    dividas_a_receber = db.session.execute(text(
        "SELECT pessoa, SUM(valor_cada) FROM splitting WHERE usuario_id=:u AND pago=FALSE GROUP BY pessoa"),
        {'u': usuario.id}).fetchall()

    # Objetivos
    try:
        objs_rows = db.session.execute(text(
            "SELECT descricao, valor_objetivo, valor_atual FROM objetivos_poupanca "
            "WHERE usuario_id=:u AND concluido=FALSE ORDER BY id DESC LIMIT 4"),
            {'u': usuario.id}).fetchall()
    except Exception:
        db.session.rollback(); objs_rows = []

    # ── SCORE DE SAÚDE FINANCEIRA (0-100) ──
    score = 100
    avisos = []
    if salario > 0:
        pct_gasto = gasto_mes / salario * 100
        if pct_gasto > 90: score -= 30; avisos.append(f"Gastos em {pct_gasto:.0f}% do salário ⚠️")
        elif pct_gasto > 70: score -= 15; avisos.append(f"Gastos em {pct_gasto:.0f}% do salário")
    if reserva < 500: score -= 20; avisos.append("Fundo de emergência baixo")
    elif reserva < 1500: score -= 10
    # Metas categoria acima de 90%
    metas = db.session.execute(text(
        "SELECT m.categoria, m.limite, COALESCE(SUM(d.valor),0) as gasto "
        "FROM metas_categoria m LEFT JOIN despesas d ON d.usuario_id=m.usuario_id "
        "AND d.categoria=m.categoria AND EXTRACT(month FROM d.data)=:m AND EXTRACT(year FROM d.data)=:y "
        "WHERE m.usuario_id=:u AND m.mes=:m AND m.ano=:y GROUP BY m.categoria, m.limite"),
        {'u':usuario.id,'m':mes,'y':ano}).fetchall()
    for cat_m, lim_m, gasto_m in metas:
        if lim_m and gasto_m/lim_m > 0.9:
            score -= 10
            avisos.append(f"{EMOJI_CAT.get(cat_m,'💳')} {cat_m.capitalize()} acima do orçamento")
    if disp < 0: score -= 15; avisos.append("Disponível negativo!")
    score = max(0, min(100, score))

    # ── COR DO SCORE ──
    if score >= 80: cor = "🟢"; nivel = "Excelente"
    elif score >= 60: cor = "🟡"; nivel = "Razoável"
    elif score >= 40: cor = "🟠"; nivel = "Atenção"
    else: cor = "🔴"; nivel = "Crítico"

    # ── MONTAR MENSAGEM ──
    msg = f"*{cor} Saúde Financeira: {score}/100 — {nivel}*\n\n"

    # Saldos principais
    eh_ruben = usuario.phone == PHONE_RUBEN
    msg += f"💰 Disponível: {disp:.0f}€\n"
    msg += f"🛡️ Emergência: {reserva:.0f}€\n"
    msg += f"💎 Poupança prevista: {poupanca_prev:.0f}€\n\n"

    # Resumo do mês
    msg += f"📈 *{nomes_m[mes-1]}*\n"
    if salario > 0:
        msg += f"Receita: {salario:.0f}€\n"
    msg += f"Gastos: {gasto_mes:.0f}€"
    if gasto_mes_ant > 0:
        diff = ((gasto_mes - gasto_mes_ant) / gasto_mes_ant) * 100
        sinal = "+" if diff > 0 else ""
        msg += f" ({sinal}{diff:.0f}% vs mês passado)"
    msg += "\n\n"

    # Maior categoria com insight
    if cats:
        top_cat, top_val = cats[0][0], cats[0][1]
        emoji_top = EMOJI_CAT.get(top_cat, '💳')
        msg += f"🔥 *Maior gasto: {emoji_top} {top_cat.capitalize()} {top_val:.0f}€*\n"
        # Insight: % dos gastos totais
        if gasto_mes > 0:
            pct_top = top_val / gasto_mes * 100
            msg += f"   {pct_top:.0f}% dos gastos deste mês\n"
        # Comparar com mês passado
        gasto_cat_ant = db.session.execute(text(
            "SELECT COALESCE(SUM(valor),0) FROM despesas WHERE usuario_id=:u AND categoria=:c "
            "AND EXTRACT(month FROM data)=:m AND EXTRACT(year FROM data)=:y"),
            {'u':usuario.id,'c':top_cat,'m':mes_ant,'y':ano_ant}).scalar() or 0
        if gasto_cat_ant > 0 and top_val > gasto_cat_ant:
            pct_aumento = (top_val - gasto_cat_ant) / gasto_cat_ant * 100
            msg += f"   +{pct_aumento:.0f}% vs mês passado\n"
        msg += "\n"

    # Avisos
    if avisos:
        msg += "⚠️ *Atenção:*\n"
        for a in avisos[:3]:
            msg += f"   • {a}\n"
        msg += "\n"

    # Objetivos com progresso
    if objs_rows:
        msg += "🎯 *Objetivos:*\n"
        for desc_o, val_obj, val_at in objs_rows[:3]:
            pct_o = min(100, round((val_at or 0) / val_obj * 100)) if val_obj and val_obj > 0 else 0
            barra = '█' * (pct_o // 20) + '░' * (5 - pct_o // 20)
            msg += f"   {barra} {desc_o} {pct_o}%\n"
        msg += "\n"

    # Dívidas a receber
    if dividas_a_receber:
        msg += "💸 *A receber:*\n"
        for pessoa, val in dividas_a_receber[:2]:
            msg += f"   {pessoa} deve-te {val:.0f}€\n"

    msg += "\n━━━━━━━━━━━━\n"
    msg += "💡 *ver transações* · *resumo*\n"
    msg += "📊 zedasfinancas.netlify.app"
    enviar_mensagem(phone_raw, msg)

def processar_assinaturas(phone_raw, usuario, texto):
    t = texto.lower()

    # Adicionar: "assinatura netflix 12" ou "adiciona spotify 7"
    m_add = re.search(r'(?:assinatura|adiciona|nova)\s+([a-zà-ú+]+)\s+(\d+(?:[.,]\d+)?)', t)
    if m_add and any(p in t for p in ['assinatura','adiciona','nova']):
        nome = m_add.group(1).capitalize()
        valor = float(m_add.group(2).replace(',','.'))
        try:
            db.session.execute(text(
                "INSERT INTO assinaturas (usuario_id, nome, valor) VALUES (:u,:n,:v)"),
                {'u':usuario.id,'n':nome,'v':valor})
            db.session.commit()
            enviar_mensagem(phone_raw, f"📺 Assinatura adicionada!\n{nome} — {valor:.2f}€/mês\n\nDiz 'assinaturas' para ver o total.")
        except Exception as e:
            log.error(f"assinatura add: {e}"); enviar_mensagem(phone_raw, "Erro 😕")
        return

    # Remover: "remove assinatura netflix"
    m_rem = re.search(r'(?:remove|apaga|cancela)\s+(?:assinatura\s+)?([a-zà-ú+]+)', t)
    if m_rem and any(p in t for p in ['remove','apaga','cancela']):
        nome = m_rem.group(1)
        try:
            r = db.session.execute(text(
                "DELETE FROM assinaturas WHERE usuario_id=:u AND LOWER(nome) LIKE :n RETURNING nome"),
                {'u':usuario.id,'n':f'%{nome}%'}).fetchone()
            db.session.commit()
            if r: enviar_mensagem(phone_raw, f"🗑️ '{r[0]}' removida das assinaturas!")
            else: enviar_mensagem(phone_raw, f"Não encontrei '{nome}' nas assinaturas.")
        except Exception as e:
            log.error(f"assinatura rem: {e}"); enviar_mensagem(phone_raw, "Erro 😕")
        return

    # Listar
    try:
        rows = db.session.execute(text(
            "SELECT nome, valor FROM assinaturas WHERE usuario_id=:u ORDER BY valor DESC"),
            {'u':usuario.id}).fetchall()
        if not rows:
            enviar_mensagem(phone_raw, "📺 Sem assinaturas registadas.\n\nAdiciona: 'assinatura netflix 12'")
            return
        total = sum(r[1] for r in rows)
        emojis_ass = {'netflix':'📺','spotify':'🎵','icloud':'☁️','chatgpt':'🤖','disney':'🏰',
                      'hbo':'🎬','youtube':'▶️','amazon':'📦','apple':'🍎','google':'🔍'}
        msg = "📺 Assinaturas mensais\n\n"
        for nome, valor in rows:
            e = next((v for k,v in emojis_ass.items() if k in nome.lower()), '🔹')
            msg += f"{e} {nome} — {valor:.2f}€\n"
        msg += f"\n💰 Total: {total:.2f}€/mês"
        msg += f"\n📅 Por ano: {total*12:.0f}€"
        enviar_mensagem(phone_raw, msg)
    except Exception as e:
        log.error(f"assinaturas: {e}"); enviar_mensagem(phone_raw, "Erro 😕")


# ─── GRUPOS DE CATEGORIAS (Casa / Carro) ─────────────────────
GRUPOS_CAT = {
    'casa': {
        'emoji': '🏠', 'nome': 'Casa',
        'cats': ['casa'],
        'lojas': ['ikea','zara home','leroy','aki','conforama','worten casa'],
        'sub': {'mobilia':'🛋️','decoracao':'🏡','iluminacao':'💡','eletro':'🔌','cozinha':'🍴'}
    },
    'carro': {
        'emoji': '🚗', 'nome': 'Carro',
        'cats': ['combustivel','carro'],
        'lojas': [],
        'sub': {'combustivel':'⛽','seguro':'🛡️','manutencao':'🔧','portagem':'🛣️','estacionamento':'🅿️'}
    },
}

def mostrar_grupo(phone_raw, usuario, grupo_key):
    g = GRUPOS_CAT.get(grupo_key)
    if not g: return
    mes = agora().month; ano = agora().year
    nomes_m = ['Janeiro','Fevereiro','Marco','Abril','Maio','Junho','Julho',
               'Agosto','Setembro','Outubro','Novembro','Dezembro']

    # Gastos das categorias do grupo
    rows = db.session.query(Despesa).filter(
        Despesa.usuario_id==usuario.id,
        db.extract('month',Despesa.data)==mes, db.extract('year',Despesa.data)==ano,
        Despesa.categoria.in_(g['cats'])).order_by(Despesa.data.desc()).all()

    if not rows:
        enviar_mensagem(phone_raw, f"{g['emoji']} {g['nome']} — sem gastos em {nomes_m[mes-1]}.")
        return

    total = sum(d.valor for d in rows)
    # Agrupar por categoria
    por_c = {}
    for d in rows:
        por_c.setdefault(d.categoria, 0)
        por_c[d.categoria] += d.valor

    msg = f"{g['emoji']} {g['nome']} — {nomes_m[mes-1]}\n\n"
    for cat, val in sorted(por_c.items(), key=lambda x:-x[1]):
        msg += f"{EMOJI_CAT.get(cat,'🔹')} {cat.capitalize()}: {val:.0f}€\n"
    msg += f"\n💰 Total: {total:.0f}€"

    # Últimos gastos
    msg += "\n\nÚltimos:\n"
    for d in rows[:5]:
        desc = d.descricao.replace('[conjunta] ','')[:25]
        msg += f"• {d.valor:.0f}€ — {desc}\n"

    enviar_mensagem(phone_raw, msg)


# ─── METAS DE CATEGORIA ──────────────────────────────────────
def processar_meta_categoria(phone_raw, usuario, texto):
    t = texto.lower()
    mes = agora().month; ano = agora().year

    # Ver metas: "metas" ou "desafios"
    if any(p in t for p in ['ver metas','metas categoria','desafios','meus desafios']) or t.strip() in ['metas','desafios']:
        try:
            rows = db.session.execute(text(
                "SELECT categoria, limite FROM metas_categoria WHERE usuario_id=:u AND mes=:m AND ano=:y"),
                {'u':usuario.id,'m':mes,'y':ano}).fetchall()
            if not rows:
                enviar_mensagem(phone_raw, "🏆 Sem metas este mês.\n\nCria: 'máximo fast food 50€'")
                return
            nomes_m = ['Jan','Fev','Mar','Abr','Mai','Jun','Jul','Ago','Set','Out','Nov','Dez']
            msg = f"🏆 Desafios — {nomes_m[mes-1]}\n\n"
            for cat, lim in rows:
                atual = gastos_cat_mes(usuario, cat, mes, ano)
                falta = lim - atual
                pct = round(atual/lim*100) if lim else 0
                barra = '█'*min(int(pct/10),10) + '░'*max(10-int(pct/10),0)
                emoji_c = EMOJI_CAT.get(cat,'💳')
                if atual <= lim:
                    estado = f"✅ Faltam {falta:.0f}€"
                else:
                    estado = f"🔴 Passaste {abs(falta):.0f}€!"
                msg += f"{emoji_c} {cat.capitalize()}\n{barra} {pct}%\n{atual:.0f}€ de {lim:.0f}€ — {estado}\n\n"
            enviar_mensagem(phone_raw, msg)
        except Exception as e:
            log.error(f"metas ver: {e}"); enviar_mensagem(phone_raw, "Erro 😕")
        return

    # Criar meta: "máximo fast food 50" ou "limite roupa 150"
    valor = extrair_valor(texto)
    if valor == 0:
        enviar_mensagem(phone_raw, "Qual o limite? Ex: 'máximo fast food 50€'"); return

    # Detetar categoria
    cat_alvo = None
    for cat in CATEGORIAS_VALIDAS:
        if cat in t:
            cat_alvo = cat; break
    if not cat_alvo:
        cat_alvo = ALIAS_CAT.get(next((w for w in t.split() if w in ALIAS_CAT), ''), None)
    if not cat_alvo:
        # tentar via categorizar
        c, _, _ = categorizar(texto)
        if c != 'outros': cat_alvo = c

    if not cat_alvo:
        enviar_mensagem(phone_raw, f"Que categoria? Ex: 'máximo fast food 50€'\nCategorias: {', '.join(CATEGORIAS_VALIDAS[:8])}...")
        return

    try:
        db.session.execute(text(
            "INSERT INTO metas_categoria (usuario_id, categoria, limite, mes, ano) VALUES (:u,:c,:l,:m,:y) "
            "ON CONFLICT (usuario_id, categoria, mes, ano) DO UPDATE SET limite=:l"),
            {'u':usuario.id,'c':cat_alvo,'l':valor,'m':mes,'y':ano})
        db.session.commit()
        atual = gastos_cat_mes(usuario, cat_alvo, mes, ano)
        enviar_mensagem(phone_raw,
            f"🎯 Desafio criado!\n{EMOJI_CAT.get(cat_alvo,'💳')} {cat_alvo.capitalize()}: máximo {valor:.0f}€\n"
            f"Atual: {atual:.0f}€ — Faltam {valor-atual:.0f}€\n\nVou avisar se te aproximares! 💪")
    except Exception as e:
        log.error(f"meta criar: {e}"); enviar_mensagem(phone_raw, "Erro 😕")



# ─── PREVISÕES / "AO RITMO ATUAL" ────────────────────────────
def previsao_fim_mes(phone_raw, usuario):
    hoje = agora()
    mes = hoje.month; ano = hoje.year
    dia_atual = hoje.day
    import calendar
    _, ultimo_dia = calendar.monthrange(ano, mes)
    dias_restantes = ultimo_dia - dia_atual

    gasto_mes = db.session.query(db.func.sum(Despesa.valor)).filter(
        Despesa.usuario_id==usuario.id,
        db.extract('month',Despesa.data)==mes,
        db.extract('year',Despesa.data)==ano,
        ~Despesa.descricao.like('[conjunta]%'),
        ~Despesa.descricao.like('[reserva]%')).scalar() or 0

    disp, p = calcular_disponivel(usuario)
    gastar = p['gastar']

    if dia_atual == 0 or gasto_mes == 0:
        enviar_mensagem(phone_raw, "Ainda sem dados suficientes este mês 😊"); return

    ritmo_diario = gasto_mes / dia_atual
    previsao_total = ritmo_diario * ultimo_dia
    previsao_fim = gastar - previsao_total

    # Comparação com mês anterior
    mes_ant = mes-1 if mes>1 else 12
    ano_ant = ano if mes>1 else ano-1
    gasto_ant = db.session.query(db.func.sum(Despesa.valor)).filter(
        Despesa.usuario_id==usuario.id,
        db.extract('month',Despesa.data)==mes_ant,
        db.extract('year',Despesa.data)==ano_ant,
        ~Despesa.descricao.like('[conjunta]%')).scalar() or 0

    msg = f"🔮 Previsão\n\n"
    msg += f"📅 Dia {dia_atual} de {ultimo_dia}\n"
    msg += f"🛒 Gasto até agora: {gasto_mes:.0f}€\n"
    msg += f"📈 Ritmo diário: ~{ritmo_diario:.1f}€/dia\n\n"

    if previsao_fim >= 0:
        msg += f"✅ Ao ritmo atual vais terminar o mês com ~{previsao_fim:.0f}€ disponíveis."
    else:
        msg += f"⚠️ Ao ritmo atual vais ultrapassar o orçamento em ~{abs(previsao_fim):.0f}€!"

    if gasto_ant > 0 and dia_atual >= 5:
        gasto_ant_proporcional = gasto_ant / ultimo_dia * dia_atual
        diff_pct = round((gasto_mes - gasto_ant_proporcional) / gasto_ant_proporcional * 100)
        if diff_pct < 0:
            msg += f"\n🎉 Estás a gastar {abs(diff_pct)}% menos que no mês passado à mesma altura!"
        elif diff_pct > 10:
            msg += f"\n😬 Estás a gastar {diff_pct}% mais que no mês passado à mesma altura."

    enviar_mensagem(phone_raw, msg)


# ─── ORÇAMENTO INTELIGENTE (SUGESTÃO BASEADA EM MÉDIA) ───────
def sugerir_orcamento(phone_raw, usuario):
    hoje = agora()
    mes = hoje.month; ano = hoje.year

    msg = "📊 Sugestão de orçamento\nBaseada nos últimos 6 meses:\n\n"
    categorias_com_dados = []

    for cat in ['fastfood','restaurante','roupa','supermercado','combustivel','lazer','saude']:
        totais = []
        for i in range(1, 7):
            m = mes - i
            y = ano
            while m <= 0: m += 12; y -= 1
            v = gastos_cat_mes(usuario, cat, m, y)
            if v > 0: totais.append(v)

        if len(totais) >= 2:
            media = sum(totais) / len(totais)
            sugestao = round(media * 0.95 / 5) * 5  # -5% arredondado a 5
            atual = gastos_cat_mes(usuario, cat, mes, ano)
            emoji_c = EMOJI_CAT.get(cat, '💳')
            msg += f"{emoji_c} {cat.capitalize()}\n"
            msg += f"   Média 6m: {media:.0f}€ → Sugestão: {sugestao:.0f}€\n"
            if atual > 0:
                msg += f"   Este mês: {atual:.0f}€\n"
            categorias_com_dados.append((cat, sugestao))

    if not categorias_com_dados:
        enviar_mensagem(phone_raw, "Ainda sem dados suficientes (preciso de pelo menos 2 meses) 😊")
        return

    msg += "\nPara criar meta: 'máximo roupa 175€'"
    enviar_mensagem(phone_raw, msg)


# ─── SIMULAR COMPRA MELHORADA (com check de objetivos) ────────
def simular_compra(phone_raw, usuario, texto):
    valor = extrair_valor(texto)
    disp, p = calcular_disponivel(usuario)

    if valor == 0:
        enviar_mensagem(phone_raw, f"💚 Tens {disp:.0f}€ para gastar este mês."); return
    if disp <= 0:
        enviar_mensagem(phone_raw, f"🔴 Orçamento já ultrapassado — não compres agora 😅"); return

    pct = valor / disp * 100
    depois = disp - valor

    # Verificar impacto nos objetivos
    objs = db.session.execute(text(
        "SELECT descricao, valor_objetivo, valor_atual FROM objetivos_poupanca WHERE usuario_id=:id AND concluido=FALSE"),
        {'id':usuario.id}).fetchall()

    # Extrair nome do produto
    stop = {'posso','comprar','isto','compra','vale','consegue','consigo','devo','uma','um','umas','uns','para'}
    palavras = [w for w in texto.split() if not re.match(r'[0-9€,.]',w) and len(w)>2 and w.lower() not in stop]
    produto = ' '.join(palavras[:3]).capitalize() if palavras else f'compra de {valor:.0f}€'

    msg = f"🛍️ {produto} — {valor:.0f}€\n"
    msg += f"💰 Tens {disp:.0f}€ disponíveis\n\n"

    if pct <= 25:
        msg += "✅ Compra tranquila — menos de 25% do disponível."
    elif pct <= 50:
        msg += f"🟡 Dá, mas pesa. Ficas com {depois:.0f}€."
    elif pct <= 100:
        msg += f"🟠 Tecnicamente sim mas ficas com apenas {depois:.0f}€. Precisas mesmo?"
    else:
        msg += f"🔴 Não dá — faltam {valor-disp:.0f}€. Deixa para o próximo mês."
        enviar_mensagem(phone_raw, msg); return

    # Check objetivos
    if objs and depois >= 0:
        poupanca_mensal = p.get('poupanca', 0)
        poupanca_restante = poupanca_mensal - valor
        if poupanca_restante < 0:
            obj = objs[0]
            meses_extra = abs(poupanca_restante) / (obj[1] / 12) if obj[1] > 0 else 0
            msg += f"\n⚠️ Atenção: pode atrasar o objetivo '{obj[0]}' em ~{meses_extra:.0f} mês(es)."
        else:
            msg += f"\n🎯 Os teus objetivos ficam intactos."

    enviar_mensagem(phone_raw, msg)


# ─── ANIVERSÁRIO AO RECEBER SALÁRIO ──────────────────────────


def _extrair_pares_valor_nome(texto, nomes):
    """Extrai pares nome+valor de frases como '20 sogra 100 taty' ou 'sogra 20 taty 100',
    associando cada número ao nome mais próximo no texto. Devolve dict {nome: valor}."""
    import re as _re
    t_lower = texto.lower()
    pos_nomes = []
    for nome in nomes:
        idx = t_lower.find(nome.lower())
        if idx >= 0:
            pos_nomes.append((idx, idx + len(nome), nome))
    pos_numeros = []
    for m in _re.finditer(r'(\d+(?:[.,]\d+)?)', texto):
        pos_numeros.append((m.start(), m.end(), float(m.group(1).replace(',', '.'))))
    if not pos_nomes or not pos_numeros:
        return {}
    resultado = {}
    usados_nomes = set()
    for num_start, num_end, valor in pos_numeros:
        melhor_nome = None
        melhor_dist = float('inf')
        for n_start, n_end, nome in pos_nomes:
            if nome in usados_nomes:
                continue
            if num_end <= n_start:
                dist = n_start - num_end
            elif n_end <= num_start:
                dist = num_start - n_end
            else:
                dist = 0
            if dist < melhor_dist:
                melhor_dist = dist
                melhor_nome = nome
        if melhor_nome and melhor_dist <= 20:
            resultado[melhor_nome] = valor
            usados_nomes.add(melhor_nome)
    return resultado

def _avancar_fila_aniversarios(phone_raw, phone, fila):
    """Avança para o próximo aniversário pendente na fila, ou termina se vazia."""
    if not fila:
        return
    proximo = fila[0]
    resto = fila[1:]
    set_estado(phone, 'aniv_apartar', {'nome': proximo['nome'], 'dias': proximo['dias'], 'fila': resto})
    enviar_mensagem(phone_raw,
        f"🎂 E o(a) *{proximo['nome']}*? Faz anos daqui a {proximo['dias']} dias!\n"
        f"Queres apartar dinheiro para a prenda? (sim/não)")

def verificar_aniversarios_proximo_mes(phone_raw, usuario, salario):
    """Chamado quando o salário é registado — verifica aniversários próximos."""
    hoje = agora().date()
    em_30_dias = hoje + timedelta(days=30)
    try:
        rows = db.session.execute(text("""
            SELECT nome, data_aniv FROM aniversarios
            WHERE usuario_id=:id
            AND (
                (EXTRACT(month FROM data_aniv)=:m1 AND EXTRACT(day FROM data_aniv)>=:d1)
                OR
                (EXTRACT(month FROM data_aniv)=:m2 AND EXTRACT(day FROM data_aniv)<=:d2)
            )
        """), {
            'id': usuario.id,
            'm1': hoje.month, 'd1': hoje.day,
            'm2': em_30_dias.month, 'd2': em_30_dias.day
        }).fetchall()
    except Exception: return

    fila_aniv = []
    for nome, data_aniv in rows:
        try:
            prox = data_aniv.replace(year=hoje.year)
            if prox < hoje: prox = prox.replace(year=hoje.year+1)
            dias = (prox - hoje).days
            if 0 < dias <= 30:
                fila_aniv.append({'nome': nome, 'dias': dias})
        except Exception: continue

    if fila_aniv:
        phone = phone_raw.replace('@lid','').replace('@c.us','').split('@')[0]
        # Guardar TODOS na fila — não sobrescreve, processa um a um
        primeiro = fila_aniv[0]
        resto = fila_aniv[1:]
        set_estado(phone, 'aniv_apartar', {'nome': primeiro['nome'], 'dias': primeiro['dias'], 'fila': resto})
        if len(fila_aniv) == 1:
            enviar_mensagem(phone_raw,
                f"🎂 Lembrete: {primeiro['nome']} faz anos daqui a {primeiro['dias']} dias!\n"
                f"Queres apartar dinheiro para a prenda? (sim/não)")
        else:
            nomes = ', '.join(f"{a['nome']} ({a['dias']}d)" for a in fila_aniv)
            enviar_mensagem(phone_raw,
                f"🎂 Aniversários a chegar: {nomes}\n\n"
                f"Vamos um de cada vez — *{primeiro['nome']}* faz anos daqui a {primeiro['dias']} dias!\n"
                f"Queres apartar dinheiro para a prenda? (sim/não)")


# ─── OBJETIVOS CASAL COM CONTRIBUIÇÕES ───────────────────────
def ver_objetivos_casal(phone_raw, usuario):
    parceiro_phone = get_parceiro_phone(usuario.phone)
    parceiro = Usuario.query.filter_by(phone=parceiro_phone).first() if parceiro_phone else None
    meu_nome = NOMES_CASAL.get(usuario.phone, 'Tu')
    par_nome = NOMES_CASAL.get(parceiro_phone, 'Parceiro') if parceiro_phone else 'Parceiro'

    msg = "💑 *Objetivos de casal*\n\n"
    tem_algum = False

    # ── Sistema 1: objetivos_poupanca com prefixo [casal] ──
    try:
        objs = db.session.execute(text(
            "SELECT descricao, valor_objetivo, valor_atual FROM objetivos_poupanca "
            "WHERE usuario_id=:id AND descricao LIKE '[casal]%' AND concluido=FALSE"),
            {'id': usuario.id}).fetchall()
    except Exception:
        objs = []

    for desc, objetivo, meu_atual in objs:
        tem_algum = True
        nome_obj = desc.replace('[casal] ','')
        par_atual = 0
        if parceiro:
            try:
                r = db.session.execute(text(
                    "SELECT valor_atual FROM objetivos_poupanca WHERE usuario_id=:uid AND descricao=:d AND concluido=FALSE"),
                    {'uid': parceiro.id, 'd': desc}).fetchone()
                if r: par_atual = r[0]
            except Exception: pass
        total_atual = meu_atual + par_atual
        pct = round(total_atual / objetivo * 100) if objetivo > 0 else 0
        barra = '█' * min(int(pct/10),10) + '░' * max(10-int(pct/10),0)
        falta = objetivo - total_atual
        msg += f"🎯 {nome_obj}\n"
        msg += f"💰 {meu_nome}: {meu_atual:.0f}€"
        if parceiro: msg += f"  |  {par_nome}: {par_atual:.0f}€"
        msg += f"\n📊 Total: {total_atual:.0f}€ de {objetivo:.0f}€\n"
        msg += f"{barra} {pct}%\n"
        if falta > 0: msg += f"Faltam {falta:.0f}€\n"
        else: msg += f"🎉 Objetivo atingido!\n"
        msg += "\n"

    # ── Sistema 2: objetivos_casal + aportes_casal (criados via "juntar dinheiro com X") ──
    try:
        objs_casal2 = db.session.execute(text(
            "SELECT id, descricao, valor_objetivo FROM objetivos_casal ORDER BY id DESC")).fetchall()
    except Exception:
        objs_casal2 = []

    for obj_id2, nome_obj2, objetivo2 in objs_casal2:
        try:
            meu_atual2 = db.session.execute(text(
                "SELECT COALESCE(SUM(valor),0) FROM aportes_casal WHERE objetivo_id=:o AND usuario_id=:u"),
                {'o': obj_id2, 'u': usuario.id}).scalar() or 0
            par_atual2 = 0
            if parceiro:
                par_atual2 = db.session.execute(text(
                    "SELECT COALESCE(SUM(valor),0) FROM aportes_casal WHERE objetivo_id=:o AND usuario_id=:u"),
                    {'o': obj_id2, 'u': parceiro.id}).scalar() or 0
            total2 = meu_atual2 + par_atual2
            if total2 == 0 and meu_atual2 == 0 and objetivo2 <= 0:
                continue
        except Exception:
            continue
        tem_algum = True
        pct2 = round(total2 / objetivo2 * 100) if objetivo2 > 0 else 0
        barra2 = '█' * min(int(pct2/10),10) + '░' * max(10-int(pct2/10),0)
        falta2 = objetivo2 - total2
        em2 = emoji_objetivo(nome_obj2)
        msg += f"{em2} {nome_obj2}\n"
        msg += f"💰 {meu_nome}: {meu_atual2:.0f}€"
        if parceiro: msg += f"  |  {par_nome}: {par_atual2:.0f}€"
        msg += f"\n📊 Total: {total2:.0f}€ de {objetivo2:.0f}€\n"
        msg += f"{barra2} {pct2}%\n"
        if falta2 > 0: msg += f"Faltam {falta2:.0f}€\n"
        else: msg += f"🎉 Objetivo atingido!\n"
        msg += "\n"

    if not tem_algum:
        enviar_mensagem(phone_raw,
            "Sem objetivos de casal ainda.\n\nCria: 'objetivo casal 1500€ para Paris' "
            "ou 'juntar dinheiro para Aveiro com a Luana' 💑"); return

    enviar_mensagem(phone_raw, msg)


# ─── COMPRAS RECORRENTES ─────────────────────────────────────
def processar_recorrentes(phone_raw, usuario, texto):
    t = texto.lower()
    mes = agora().month; ano = agora().year

    # Adicionar: "recorrente galp 50€ todo o mês"
    m_add = re.search(r'(?:recorrente|todo o mes|todos os meses|mensal)\s+([a-zà-ú\s]+?)\s+(\d+(?:[.,]\d+)?)', t)
    if m_add and any(p in t for p in ['recorrente','todo o mes','todos os meses']):
        desc = m_add.group(1).strip().capitalize()
        valor = float(m_add.group(2).replace(',','.'))
        try:
            db.session.execute(text(
                "INSERT INTO recorrentes (usuario_id, descricao, valor) VALUES (:u,:d,:v) ON CONFLICT DO NOTHING"),
                {'u':usuario.id,'d':desc,'v':valor})
            db.session.commit()
            enviar_mensagem(phone_raw, f"🔁 Recorrente adicionado!\n{desc} — {valor:.0f}€/mês\n\nDiz 'recorrentes' para ver todos.")
        except Exception as e:
            log.error(f"recorrente add: {e}"); enviar_mensagem(phone_raw, "Erro 😕")
        return

    # Listar
    try:
        rows = db.session.execute(text(
            "SELECT descricao, valor FROM recorrentes WHERE usuario_id=:u ORDER BY valor DESC"),
            {'u':usuario.id}).fetchall()
        if not rows:
            enviar_mensagem(phone_raw, "🔁 Sem recorrentes.\nAdiciona: 'recorrente Galp 50€ todo o mês'")
            return
        total = sum(r[1] for r in rows)
        msg = "🔁 Compras recorrentes\n\n"
        for desc, val in rows:
            msg += f"• {desc} — {val:.0f}€\n"
        msg += f"\n💰 Total: {total:.0f}€/mês"
        enviar_mensagem(phone_raw, msg)
    except Exception as e:
        log.error(f"recorrentes: {e}"); enviar_mensagem(phone_raw, "Erro 😕")



def enviar_plano_contas(phone_raw, usuario):
    """Mostra o plano de contas definido pelo utilizador."""
    msg = (
        "🏦 Plano de Contas\n\n"
        "📍 *BPI* — Operacional\n"
        "   Receber salário + pagar fixos + buffer 200€\n\n"
        "🛡️ *Bankinter* — Fundo de Emergência\n"
        "   Meta: 2.000-2.500€ · 5% no 1º ano\n"
        "   Só usar em emergências reais\n\n"
        "💰 *Trade Republic* — Poupança\n"
        "   Cash (2%): objetivos a 1-3 anos\n"
        "   ETF (MSCI World/VUAA): longo prazo 5+ anos\n\n"
        "💑 *Revolut* — Dia a dia\n"
        "   Orçamento variável + conjunta + vaults\n\n"
        "📊 *Fluxo mensal:*\n"
        "   Salário → BPI\n"
        "   → Fixos ficam no BPI\n"
        "   → Variável → Revolut\n"
        "   → Conjunta 50€ → Revolut\n"
        "   → Poupança → Bankinter (fundo incompleto)\n"
        "              → Trade Republic (fundo completo)"
    )
    enviar_mensagem(phone_raw, msg)


def enviar_onde_vai_dinheiro(phone_raw, usuario):
    """Mostra como distribuir o salário — contas específicas de cada um."""
    p = calcular_plano(usuario.salario_liquido or 0, phone=usuario.phone, usuario_id=usuario.id)
    salario = usuario.salario_liquido or 0
    fixos = p.get('total_fixos', 0)
    gastar = p.get('gastar', 0)
    poupanca = p.get('poupanca', 0)
    fundo = p.get('fundo', 0)
    subsidio = p.get('subsidio', False)

    eh_ruben = usuario.phone == PHONE_RUBEN
    banco_principal = "BPI" if eh_ruben else "CGD"
    conta_fundo = "Bankinter" if eh_ruben else "Revolut (cofre)"
    conta_poupanca = "Trade Republic" if eh_ruben else "Poupança CGD"

    META_FUNDO = 2500
    try:
        saldo_reserva = get_reserva(usuario.id)
    except Exception:
        saldo_reserva = 0
    fundo_completo = saldo_reserva >= META_FUNDO
    pct_fundo = min(100, round(saldo_reserva / META_FUNDO * 100))

    msg = f"💶 *Distribuição de {salario:.0f}€*\n"
    if subsidio:
        msg += "🎉 Mês de subsídio incluído!\n"
    msg += "\n"
    msg += f"🏦 *{banco_principal}* — fica cá\n"
    msg += f"   Fixos: {fixos:.0f}€\n"
    msg += f"   Buffer: 200€\n\n"
    msg += f"💜 *Revolut* — transferir {gastar + 50:.0f}€\n"
    msg += f"   Variável do mês: {gastar:.0f}€\n"
    msg += f"   Conjunta: 50€\n\n"
    if not fundo_completo:
        msg += f"🛡️ *{conta_fundo}* — transferir {fundo:.0f}€\n"
        msg += f"   Fundo emergência: {saldo_reserva:.0f}€/{META_FUNDO}€ ({pct_fundo}%)\n"
        barra = '█' * (pct_fundo // 10) + '░' * (10 - pct_fundo // 10)
        msg += f"   {barra}\n\n"
    else:
        msg += f"🛡️ *{conta_fundo}* — fundo completo ✅\n"
        msg += f"   (os {fundo:.0f}€ vão para a poupança)\n\n"
        poupanca += fundo
    msg += f"💰 *{conta_poupanca}* — transferir {poupanca:.0f}€\n"
    msg += f"   Toda a poupança do mês\n\n"
    msg += f"━━━━━━━━━━━━\n"
    msg += f"📊 Total: {salario:.0f}€ ✓"
    enviar_mensagem(phone_raw, msg)

def processar_abastecimento(phone_raw, usuario, texto):
    """Regista abastecimento de gasolina com tracking de km."""
    t = texto.lower()

    # Ver histórico: "gasolina", "abastecimentos", "consumo"
    if any(p in t for p in ['historico gasolina','historico abastecimento','ver abastecimentos','consumo carro']):
        try:
            rows = db.session.execute(text(
                "SELECT data, km_antes, km_depois, valor, km_ganhos, custo_por_km "
                "FROM abastecimentos WHERE usuario_id=:u ORDER BY data DESC LIMIT 6"),
                {'u': usuario.id}).fetchall()
            if not rows:
                enviar_mensagem(phone_raw, "⛽ Ainda sem abastecimentos registados.\nDiz: tinha 50km meti 25€ agora tenho 450km")
                return
            msg = "⛽ Últimos abastecimentos\n\n"
            for r in rows:
                data_fmt = r[0].strftime('%d/%m')
                msg += f"📅 {data_fmt} — {r[3]:.0f}€"
                if r[4]: msg += f" | {r[4]:.0f}km"
                if r[5]: msg += f" | {r[5]:.3f}€/km"
                msg += "\n"
            # Médias
            media_rows = db.session.execute(text(
                "SELECT AVG(valor), AVG(km_ganhos), AVG(custo_por_km) "
                "FROM abastecimentos WHERE usuario_id=:u AND km_ganhos IS NOT NULL"),
                {'u': usuario.id}).fetchone()
            if media_rows and media_rows[0]:
                msg += f"\n📊 Médias:\n"
                msg += f"   Custo: {media_rows[0]:.0f}€/abastecimento\n"
                if media_rows[1]: msg += f"   Autonomia: {media_rows[1]:.0f}km\n"
                if media_rows[2]: msg += f"   Eficiência: {media_rows[2]:.3f}€/km\n"
            enviar_mensagem(phone_raw, msg)
        except Exception as e:
            log.error(f"historico abastecimento: {e}")
            enviar_mensagem(phone_raw, "Erro 😕")
        return

    # Registar: "tinha Xkm meti Z€ agora tenho Ykm"
    import re as re2
    km_antes = None; km_depois = None; valor = None; litros = None

    m_antes = re2.search(r'tinha\s+(\d+(?:[.,]\d+)?)\s*km', t)
    m_depois = re2.search(r'(?:agora(?:\s+tenho)?|tenho agora)\s+(\d+(?:[.,]\d+)?)\s*km', t)
    m_valor = re2.search(r'(\d+(?:[.,]\d+)?)\s*(?:€|euros?|euro)', t)
    m_litros = re2.search(r'(\d+(?:[.,]\d+)?)\s*(?:l|litros?)', t)

    if m_antes: km_antes = float(m_antes.group(1).replace(',','.'))
    if m_depois: km_depois = float(m_depois.group(1).replace(',','.'))
    if m_valor: valor = float(m_valor.group(1).replace(',','.'))
    if m_litros: litros = float(m_litros.group(1).replace(',','.'))

    if valor is None:
        enviar_mensagem(phone_raw,
            "⛽ Para registar abastecimento diz:\n"
            "tinha 50km meti 25€ agora tenho 450km\n\n"
            "Para ver histórico: historico gasolina")
        return

    km_ganhos = None; custo_por_km = None
    if km_antes is not None and km_depois is not None and km_depois > km_antes:
        km_ganhos = km_depois - km_antes
        custo_por_km = round(valor / km_ganhos, 4) if km_ganhos > 0 else None

    try:
        db.session.execute(text(
            "INSERT INTO abastecimentos (usuario_id, data, km_antes, km_depois, valor, litros, km_ganhos, custo_por_km) "
            "VALUES (:u, :d, :ka, :kd, :v, :l, :kg, :cpk)"),
            {'u': usuario.id, 'd': agora().replace(tzinfo=None),
             'ka': km_antes, 'kd': km_depois, 'v': valor,
             'l': litros, 'kg': km_ganhos, 'cpk': custo_por_km})
        # Também regista como despesa
        despesa = Despesa(usuario_id=usuario.id, valor=valor, categoria='combustivel',
            descricao='Gasolina', data=agora().replace(tzinfo=None))
        db.session.add(despesa)
        db.session.commit()

        msg = f"⛽ Abastecimento registado!\n"
        msg += f"💰 {valor:.0f}€"
        if litros: msg += f" | {litros:.1f}L"
        if km_ganhos:
            msg += f"\n🛣️ +{km_ganhos:.0f}km de autonomia"
        if custo_por_km:
            msg += f"\n📊 {custo_por_km:.3f}€/km"
            # Comparar com carro do utilizador
            consumo = get_consumo_carro(usuario)
            msg += f"\n🚗 {getattr(usuario, 'carro_nome', None) or 'Carro'}: ~{consumo}L/100km"
        enviar_mensagem(phone_raw, msg)
    except Exception as e:
        log.error(f"abastecimento: {e}"); db.session.rollback()
        enviar_mensagem(phone_raw, "Erro 😕")


def registar_abastecimento(phone_raw, usuario, texto):
    """Regista abastecimento com autonomia antes/depois e valor."""
    try:
        t = texto.lower()
        nums = re.findall(r'\d+(?:[.,]\d+)?', t)
        floats = [float(n.replace(',','.')) for n in nums]

        if len(floats) < 3:
            enviar_mensagem(phone_raw,
                "⛽ Não percebi. Tenta assim:\n*tinha 80km meti 20€ fiquei com 200km*")
            return

        m_antes = re.search(r'tinha\s+(\d+)', t)
        m_valor = re.search(r'(?:meti|gastei|paguei|custou)\s+(\d+(?:[.,]\d+)?)', t)
        m_depois = re.search(r'(?:fiquei|tenho|ficou|agora)\s+(?:com\s+)?(\d+)', t)

        if m_antes and m_valor and m_depois:
            km_antes = float(m_antes.group(1))
            valor = float(m_valor.group(1).replace(',','.'))
            km_depois = float(m_depois.group(1))
        else:
            # Fallback: menor número = valor (€), os outros dois = km
            ordenados = sorted(floats[:3])
            valor = ordenados[0]
            km_antes = ordenados[1]
            km_depois = ordenados[2]

        km_ganhos = km_depois - km_antes
        consumo = get_consumo_carro(usuario)
        # Litros estimados a partir do PREÇO (mais fiável que dos km)
        PRECO_LITRO_MEDIO = 1.85
        litros_estimados = round(valor / PRECO_LITRO_MEDIO, 1)
        custo_litro = PRECO_LITRO_MEDIO
        # Custo por km percorrido (real, com base na autonomia ganha)
        custo_km = round(valor / km_ganhos, 3) if km_ganhos > 0 else 0
        # Autonomia teórica desses litros
        km_teoricos = round(litros_estimados / consumo * 100) if consumo > 0 else 0

        # Garantir tabela
        db.session.execute(text(
            "CREATE TABLE IF NOT EXISTS abastecimentos (id SERIAL PRIMARY KEY, user_phone VARCHAR(50), "
            "data TIMESTAMP DEFAULT NOW(), km_antes FLOAT, km_depois FLOAT, km_percorridos FLOAT, "
            "valor FLOAT, litros FLOAT, custo_por_km FLOAT, consumo_l100 FLOAT)"))
        db.session.commit()

        db.session.execute(text(
            "INSERT INTO abastecimentos (user_phone, data, km_antes, km_depois, km_percorridos, "
            "valor, litros, custo_por_km, consumo_l100) VALUES (:p,:d,:a,:b,:c,:v,:l,:cpk,:cons)"),
            {'p': usuario.phone, 'd': agora().replace(tzinfo=None),
             'a': km_antes, 'b': km_depois, 'c': km_ganhos,
             'v': valor, 'l': litros_estimados, 'cpk': custo_km, 'cons': consumo})

        despesa = Despesa(usuario_id=usuario.id, valor=valor, categoria='combustivel',
            descricao=f'Gasolina +{km_ganhos:.0f}km autonomia', data=agora().replace(tzinfo=None))
        db.session.add(despesa)
        db.session.commit()

        um_mes = agora().replace(tzinfo=None) - __import__('datetime').timedelta(days=30)
        hist = db.session.execute(text(
            "SELECT SUM(km_percorridos), SUM(valor), COUNT(*) FROM abastecimentos "
            "WHERE user_phone=:p AND data>=:d"),
            {'p': usuario.phone, 'd': um_mes}).fetchone()

        msg = f"⛽ *Abastecimento registado!*\n"
        msg += f"━━━━━━━━━━━━━━\n"
        msg += f"💰 Meteste:  {valor:.0f}€  (~{litros_estimados:.1f}L)\n"
        msg += f"🛣️ Autonomia:  {km_antes:.0f} → {km_depois:.0f}km  (+{km_ganhos:.0f}km)\n"
        if custo_km > 0:
            msg += f"📊 Custo:  {custo_km:.2f}€/km\n"
        if hist and hist[0]:
            msg += f"━━━━━━━━━━━━━━\n"
            msg += f"📅 30 dias: {hist[1]:.0f}€ em gasolina · {hist[2]}x"
        enviar_mensagem(phone_raw, msg)
    except Exception as e:
        db.session.rollback()
        log.error(f"abastecimento ERRO: {type(e).__name__}: {e}", exc_info=True)
        enviar_mensagem(phone_raw, "Não consegui registar o abastecimento 😕")

def ver_historico_combustivel(phone_raw, usuario):
    """Mostra histórico de abastecimentos."""
    try:
        rows = db.session.execute(text(
            "SELECT data, km_antes, km_depois, km_percorridos, valor, custo_por_km "
            "FROM abastecimentos WHERE user_phone=:p "
            "ORDER BY data DESC LIMIT 5"),
            {'p': usuario.phone}).fetchall()

        if not rows:
            enviar_mensagem(phone_raw,
                "⛽ Sem abastecimentos registados.\n\nRegista assim:\n"
                "*tinha 52340 tenho 52640 meti 20€*")
            return

        # Stats gerais
        stats = db.session.execute(text(
            "SELECT SUM(km_percorridos), SUM(valor), AVG(custo_por_km), COUNT(*) "
            "FROM abastecimentos WHERE user_phone=:p"),
            {'p': usuario.phone}).fetchone()

        msg = "⛽ Histórico combustível\n\n"
        if stats[0]:
            msg += f"📊 Total: {stats[0]:.0f}km | {stats[1]:.0f}€\n"
            msg += f"💰 Média: {stats[2]*100:.1f}€/100km\n\n"

        msg += "Últimos abastecimentos:\n"
        for r in rows:
            data_fmt = r[0].strftime('%d/%m') if r[0] else '—'
            msg += f"• {data_fmt}: {r[3]:.0f}km | {r[4]:.0f}€\n"

        enviar_mensagem(phone_raw, msg)
    except Exception as e:
        log.error(f"historico combustivel: {e}")
        enviar_mensagem(phone_raw, "Erro 😕")


def processar_sms_banco(phone_raw, usuario, texto):
    """Deteta SMS/notificação de banco encaminhada e regista o gasto."""
    t = texto.lower()
    # Padrões comuns PT: BPI, CGD, Santander, ActivoBank, Revolut, Millennium
    # "Compra ... 24,50 EUR ... CONTINENTE" / "compra com cartao no valor de X em Y"
    m_valor = re.search(r'(\d+[.,]\d{2})\s*(?:eur|€|euros)?', t)
    if not m_valor:
        return False
    valor = float(m_valor.group(1).replace(',', '.'))

    # Extrair comerciante: depois de "em", "no", "loja", ou ÚLTIMA palavra maiúscula longa
    comerciante = None
    m_com = re.search(r'(?:\bem\b|\bno\b|\bna\b)\s+([A-ZÀ-Ú][A-ZÀ-Ú0-9\s&\.\*]{2,30})', texto)
    if m_com:
        comerciante = m_com.group(1).strip()
    else:
        # palavras todas maiúsculas no texto original (formato típico de SMS)
        caps = re.findall(r'\b[A-ZÀ-Ú][A-ZÀ-Ú0-9&\*\.]{3,}\b', texto)
        caps = [c for c in caps if c not in ['EUR','EUROS','IBAN','SMS','MB','MBWAY','CARTAO','CARTÃO','COMPRA','VALOR','DEBITO','DÉBITO','CONTA','SALDO']]
        if caps:
            comerciante = caps[-1]

    if not comerciante:
        comerciante = 'Compra cartão'

    cat, emoji, _ = categorizar(comerciante.lower())
    try:
        despesa = Despesa(usuario_id=usuario.id, valor=valor, categoria=cat,
            descricao=f'[SMS] {comerciante.title()[:50]}', data=agora().replace(tzinfo=None))
        db.session.add(despesa); db.session.commit()
        disp, _ = calcular_disponivel(usuario)
        enviar_mensagem(phone_raw,
            f"🏦 SMS do banco detetado!\n\n"
            f"{emoji} {comerciante.title()[:40]} — {valor:.2f}€\n"
            f"📁 Categoria: {cat}\n"
            f"💚 Disponível: {disp:.2f}€")
        return True
    except Exception as e:
        log.error(f"sms_banco: {e}"); db.session.rollback()
        return False


def eh_sms_banco(texto):
    """Verifica se o texto parece SMS/notificação de banco."""
    t = texto.lower()
    indicadores = ['compra com cartao','compra com cartão','compra no valor','debito de','débito de',
                   'cartao final','cartão final','movimento a debito','movimento a débito',
                   'pagamento efetuado','compra efetuada','autorizada a compra','transacao aprovada',
                   'transação aprovada','cartao terminado','disponivel apos','disponível após']
    return any(i in t for i in indicadores)


def extrair_data_relativa(texto):
    """Deteta referências temporais e devolve a data correta (ou None = hoje)."""
    t = texto.lower()
    hoje = agora().date()
    if 'anteontem' in t:
        return hoje - timedelta(days=2)
    if 'ontem' in t:
        return hoje - timedelta(days=1)
    dias_semana = {'segunda':0,'terca':1,'terça':1,'quarta':2,'quinta':3,'sexta':4,'sabado':5,'sábado':5,'domingo':6}
    for nome, wd in dias_semana.items():
        if nome in t:
            delta = (hoje.weekday() - wd) % 7
            if delta == 0: delta = 7  # "sábado" hoje sendo sábado = sábado passado
            return hoje - timedelta(days=delta)
    m = re.search(r'dia\s+(\d{1,2})\b', t)
    if m:
        d = int(m.group(1))
        if 1 <= d <= 31 and d <= hoje.day:
            try: return hoje.replace(day=d)
            except ValueError: pass
    return None


def apagar_ultimo_gasto(phone_raw, usuario):
    """Apaga o último gasto registado."""
    try:
        r = db.session.execute(text(
            "DELETE FROM despesas WHERE id=(SELECT id FROM despesas WHERE usuario_id=:u ORDER BY id DESC LIMIT 1) "
            "RETURNING descricao, valor"), {'u': usuario.id}).fetchone()
        db.session.commit()
        if r:
            enviar_mensagem(phone_raw, f"🗑️ Apagado: {r[0]} — {r[1]:.2f}€\nComo se nunca tivesse acontecido 😉")
        else:
            enviar_mensagem(phone_raw, "Não há gastos para apagar 🤷")
    except Exception as e:
        log.error(f"apagar_ultimo: {e}"); db.session.rollback()
        enviar_mensagem(phone_raw, "Erro ao apagar 😕")


def corrigir_ultimo_gasto(phone_raw, usuario, texto):
    """Corrige o valor do último gasto: 'corrige para 25' / 'altera o último para 25€'."""
    valor = extrair_valor(texto)
    if valor <= 0:
        enviar_mensagem(phone_raw, "Qual é o valor correto? Ex: 'corrige para 25€'"); return
    try:
        r = db.session.execute(text(
            "UPDATE despesas SET valor=:v WHERE id=(SELECT id FROM despesas WHERE usuario_id=:u ORDER BY id DESC LIMIT 1) "
            "RETURNING descricao"), {'v': valor, 'u': usuario.id}).fetchone()
        db.session.commit()
        if r:
            enviar_mensagem(phone_raw, f"✏️ Corrigido! {r[0]} agora é {valor:.2f}€")
        else:
            enviar_mensagem(phone_raw, "Não há gastos para corrigir 🤷")
    except Exception as e:
        log.error(f"corrigir_ultimo: {e}"); db.session.rollback()
        enviar_mensagem(phone_raw, "Erro 😕")


def processar_fatura_referencia(phone_raw, usuario, texto):
    """Deteta entidade/referência MB numa fatura colada e formata para pagamento."""
    # Entidade: 5 dígitos | Referência: 9 dígitos (formato XXX XXX XXX comum)
    m_ent = re.search(r'entidade[:\s]*(\d{5})', texto, re.IGNORECASE)
    m_ref = re.search(r'refer[êe]ncia[:\s]*([\d\s]{9,15})', texto, re.IGNORECASE)
    m_val = re.search(r'(?:valor|montante|total|importância)[:\s]*(\d+[.,]\d{2})', texto, re.IGNORECASE)

    if not (m_ent and m_ref):
        return False

    entidade = m_ent.group(1)
    referencia = re.sub(r'\s', '', m_ref.group(1))[:9]
    valor = float(m_val.group(1).replace(',', '.')) if m_val else 0

    msg = "🧾 Fatura detetada! Dados para pagamento:\n\n"
    msg += f"🏛️ Entidade: *{entidade}*\n"
    msg += f"🔢 Referência: *{referencia[:3]} {referencia[3:6]} {referencia[6:9]}*\n"
    if valor > 0:
        msg += f"💰 Valor: *{valor:.2f}€*\n"
    msg += "\nCopia e cola no homebanking 📲"
    if valor > 0:
        msg += "\n\nQuando pagares diz 'paguei a fatura' que eu registo o gasto 😉"
        phone = usuario.phone
        set_estado(phone, 'fatura_pendente', {'valor': valor, 'entidade': entidade})
    enviar_mensagem(phone_raw, msg)
    return True


def processar_ja_paguei(phone_raw, usuario, texto):
    """'Já paguei ao Ruben' — marca dívidas ao parceiro como pagas e notifica-o."""
    t = texto.lower()
    m = re.search(r'paguei\s+(?:ao|à|a)\s+([a-záàâãéêíóôõúç]+)', t)
    if not m:
        return False
    credor = m.group(1).capitalize()
    meu_nome = NOMES_CASAL.get(usuario.phone, 'Alguém')

    # O credor registou que EU lhe devo → procurar na conta DELE splits com o MEU nome
    parceiro_phone = get_parceiro_phone(usuario.phone)
    if not parceiro_phone or NOMES_CASAL.get(parceiro_phone,'').lower() != credor.lower():
        return False
    parceiro = Usuario.query.filter_by(phone=parceiro_phone).first()
    if not parceiro:
        return False
    try:
        rows = db.session.execute(text(
            "UPDATE splitting SET pago=TRUE WHERE usuario_id=:u AND LOWER(pessoa)=LOWER(:p) AND pago=FALSE "
            "RETURNING descricao, valor_cada"),
            {'u': parceiro.id, 'p': meu_nome}).fetchall()
        db.session.commit()
        if rows:
            total = sum(r[1] for r in rows)
            enviar_mensagem(phone_raw,
                f"✅ Boa! Marquei como pago: {total:.2f}€ ao(à) {credor} 💪")
            notificar_parceiro(usuario.phone,
                f"💰 {meu_nome} pagou-te {total:.2f}€!\n"
                f"📝 {', '.join(r[0] for r in rows[:3])}\n"
                f"Estão quites 🤝")
            return True
        else:
            enviar_mensagem(phone_raw, f"Não encontrei dívidas tuas ao(à) {credor} 🤔")
            return True
    except Exception as e:
        log.error(f"ja_paguei: {e}"); db.session.rollback()
        return False


DISTANCIAS_MOITA = {
    'algarve': 240, 'faro': 250, 'albufeira': 230, 'portimao': 260, 'lagos': 280,
    'porto': 320, 'braga': 370, 'guimaraes': 360, 'aveiro': 250, 'coimbra': 180,
    'lisboa': 30, 'sintra': 55, 'cascais': 55, 'setubal': 25, 'evora': 100,
    'fatima': 140, 'nazare': 150, 'obidos': 110, 'peniche': 120, 'ericeira': 75,
    'sesimbra': 30, 'troia': 40, 'comporta': 60, 'badajoz': 180, 'sevilha': 300,
    'madrid': 630, 'salamanca': 450, 'viana': 400, 'guarda': 290, 'viseu': 250,
}
PORTAGENS_POR_KM = 0.095  # média A2/A1


def get_consumo_carro(usuario):
    """Busca consumo do carro de forma segura (coluna pode não existir no modelo)."""
    try:
        return getattr(usuario, 'carro_consumo_l100', None) or _consumo_por_phone(usuario.phone)
    except Exception:
        return _consumo_por_phone(usuario.phone)

def _consumo_por_phone(phone):
    """Consumo fixo por utilizador (fallback)."""
    if phone == PHONE_RUBEN:
        return 7.5  # Ibiza 6J 1.2
    elif phone == PHONE_LUANA:
        return 5.5  # Taigo
    return 6.5

def calcular_viagem(phone_raw, usuario, texto):
    """'vamos ao algarve' -> custo estimado: combustivel + portagens."""
    try:
        t = texto.lower().replace('ã','a').replace('é','e').replace('í','i').replace('ó','o').replace('â','a')
        destino = None
        km = None
        for cidade, dist in DISTANCIAS_MOITA.items():
            if cidade in t:
                destino = cidade.capitalize(); km = dist; break
        if not km:
            m = re.search(r'(\d{2,4})\s*km', t)
            if m:
                km = int(m.group(1)); destino = f"{km}km"
        if not km:
            enviar_mensagem(phone_raw,
                "🗺️ Para onde vais? Conheço: Algarve, Porto, Coimbra, Évora...\n"
                "Ou diz os km: 'viagem de 300km'")
            return

        consumo = get_consumo_carro(usuario)
        ida_volta = km * 2
        litros = ida_volta * consumo / 100
        preco_litro = 1.75
        custo_comb = litros * preco_litro
        custo_portagens = ida_volta * PORTAGENS_POR_KM * 0.7
        total = custo_comb + custo_portagens

        try:
            disp, _ = calcular_disponivel(usuario)
        except Exception:
            disp = 0

        msg = f"🗺️ *Viagem ao {destino}* (ida e volta)\n"
        msg += f"━━━━━━━━━━━━━━\n"
        msg += f"🛣️ Distância:  {ida_volta}km\n"
        msg += f"⛽ Combustível:  ~{custo_comb:.0f}€ ({litros:.0f}L)\n"
        msg += f"🛂 Portagens:  ~{custo_portagens:.0f}€\n"
        msg += f"━━━━━━━━━━━━━━\n"
        msg += f"💰 *Total: ~{total:.0f}€*\n"
        if usuario.phone in [PHONE_RUBEN, PHONE_LUANA]:
            msg += f"💑 A dividir: {total/2:.0f}€ cada\n"
        if disp > 0:
            pct = total / disp * 100
            msg += f"\n💳 É {pct:.0f}% do teu disponível"
            if pct > 50:
                msg += " — pesa bem 😅"
        enviar_mensagem(phone_raw, msg)
    except Exception as e:
        log.error(f"calcular_viagem ERRO: {type(e).__name__}: {e}", exc_info=True)
        enviar_mensagem(phone_raw, "Não consegui calcular a viagem 😕 Tenta: *viagem de 300km*")

def detetar_multi_gastos(texto):
    """Deteta se a mensagem tem múltiplos gastos e divide via Groq. Devolve lista ou None."""
    t = texto.lower()
    # Heurística: 2+ valores E conectores de divisão
    valores = re.findall(r'\b\d+(?:[.,]\d{1,2})?\b', t)
    conectores = ['mas ','sendo ',' foram ',' foi para',' e o resto',' resto ',' dos quais',' sendo ']
    if len(valores) < 2 or not any(c in t for c in conectores):
        return None
    try:
        from groq import Groq
        resp = Groq(api_key=GROQ_API_KEY).chat.completions.create(
            model='llama-3.1-8b-instant', max_tokens=200, temperature=0,
            messages=[
                {'role':'system','content':
                 'Divide a mensagem em gastos individuais. Responde APENAS JSON array: '
                 '[{"valor": 4.0, "descricao": "café"}, {"valor": 30.0, "descricao": "compras continente"}]. '
                 'Se "o resto" for mencionado, calcula a diferença. Sem texto extra.'},
                {'role':'user','content': texto}
            ])
        import json as _json
        txt = resp.choices[0].message.content.strip()
        txt = re.sub(r'^```(?:json)?|```$', '', txt, flags=re.MULTILINE).strip()
        gastos = _json.loads(txt)
        if isinstance(gastos, list) and 2 <= len(gastos) <= 5:
            validos = [g for g in gastos if isinstance(g.get('valor'), (int,float)) and g['valor'] > 0]
            return validos if len(validos) >= 2 else None
    except Exception as e:
        log.error(f"multi_gastos: {e}")
    return None


def processar_lembrete(phone_raw, usuario, texto):
    """Regista lembrete por data/hora natural."""
    t = texto.lower()
    agora_dt = agora()

    # Extrair quando
    quando = None
    from datetime import time as _time
    m_hora = re.search(r'(?:às|as|as|à)?\s*(\d{1,2})h?(?:[h:](\d{2}))?', t)
    hora = int(m_hora.group(1)) if m_hora else 9
    minuto = int(m_hora.group(2)) if m_hora and m_hora.group(2) else 0

    if 'amanhã' in t or 'amanha' in t:
        quando = (agora_dt + __import__('datetime').timedelta(days=1)).replace(
            hour=hora, minute=minuto, second=0, microsecond=0, tzinfo=None)
    elif 'hoje' in t:
        quando = agora_dt.replace(hour=hora, minute=minuto, second=0, microsecond=0, tzinfo=None)
    else:
        # Dias da semana
        dias = {'segunda':0,'terca':1,'terça':1,'quarta':2,'quinta':3,'sexta':4,'sabado':5,'sábado':5,'domingo':6}
        for nome, wd in dias.items():
            if nome in t:
                delta = (wd - agora_dt.weekday()) % 7
                if delta == 0: delta = 7
                quando = (agora_dt + __import__('datetime').timedelta(days=delta)).replace(
                    hour=hora, minute=minuto, second=0, microsecond=0, tzinfo=None)
                break

    if not quando:
        enviar_mensagem(phone_raw, "Quando? Ex: 'lembra-me de ligar ao mecânico amanhã às 10h'")
        return

    # Extrair o texto do lembrete (remover palavras de tempo)
    texto_limpo = re.sub(r'lembra(?:-me|\s+me)?\s+(?:de\s+|que\s+|para\s+)?', '', t, flags=re.IGNORECASE)
    texto_limpo = re.sub(r'(?:amanhã|amanha|hoje|segunda|terça|terca|quarta|quinta|sexta|sábado|sabado|domingo|às|as|daqui).*', '', texto_limpo, flags=re.IGNORECASE).strip()
    texto_limpo = texto_limpo.capitalize() or "Lembrete"

    try:
        db.session.execute(text(
            "INSERT INTO lembretes (usuario_id, texto, quando) VALUES (:u,:t,:q)"),
            {'u': usuario.id, 't': texto_limpo[:300], 'q': quando})
        db.session.commit()
        data_fmt = quando.strftime('%d/%m às %H:%M')
        enviar_mensagem(phone_raw, f"⏰ Lembrete anotado!\n*{texto_limpo}*\n📅 {data_fmt}")
    except Exception as e:
        log.error(f"lembrete: {e}"); db.session.rollback()
        enviar_mensagem(phone_raw, "Erro ao guardar lembrete 😕")


def registar_saldo_conta(phone_raw, usuario, texto):
    """'saldo BPI 1200' / 'saldo revolut 350' — regista saldo manual."""
    t = texto.lower()
    contas = ['bpi','cgd','caixa','bankinter','revolut','trade republic','tr','millennium','santander','activo','wise']
    conta = next((c for c in contas if c in t), None)
    if not conta: return False
    valor = extrair_valor(texto)
    if valor <= 0: return False
    conta_nome = {'tr':'Trade Republic','cgd':'CGD','caixa':'CGD'}.get(conta, conta.upper())
    try:
        db.session.execute(text(
            "INSERT INTO saldos_contas (usuario_id, conta, valor, atualizado_em) VALUES (:u,:c,:v,NOW()) "
            "ON CONFLICT (usuario_id, conta) DO UPDATE SET valor=:v, atualizado_em=NOW()"),
            {'u': usuario.id, 'c': conta_nome, 'v': valor})
        db.session.commit()
        enviar_mensagem(phone_raw, f"🏦 *{conta_nome}*: {valor:.2f}€ guardado ✅")
        return True
    except Exception as e:
        log.error(f"saldo_conta: {e}"); db.session.rollback()
        return False


def ver_patrimonio(phone_raw, usuario):
    """Mostra todos os saldos e total."""
    try:
        rows = db.session.execute(text(
            "SELECT conta, valor, atualizado_em FROM saldos_contas WHERE usuario_id=:u ORDER BY valor DESC"),
            {'u': usuario.id}).fetchall()
        if not rows:
            enviar_mensagem(phone_raw,
                "Ainda não tens saldos registados.\n"
                "Diz por exemplo: *saldo BPI 1200*")
            return
        total = sum(r[1] for r in rows)
        msg = "🏦 *Patrimônio*\n\n"
        for conta, val, updated in rows:
            data_u = updated.strftime('%d/%m') if updated else '—'
            msg += f"   {conta}: {val:.0f}€  _{data_u}_\n"
        msg += f"\n━━━━━━━━\n💰 *Total: {total:.0f}€*"
        enviar_mensagem(phone_raw, msg)
    except Exception as e:
        log.error(f"patrimonio: {e}"); db.session.rollback()


def ver_transacoes(phone_raw, usuario, limite=15):
    """Lista as últimas transações com código para apagar."""
    try:
        rows = db.session.execute(text(
            "SELECT id, descricao, valor, categoria, data FROM despesas "
            "WHERE usuario_id=:u ORDER BY data DESC, id DESC LIMIT :lim"),
            {'u': usuario.id, 'lim': limite}).fetchall()
        if not rows:
            enviar_mensagem(phone_raw, "Ainda não tens gastos registados 📭\nRegista um: *gastei 5 no café*")
            return

        dias_pt = ['Seg','Ter','Qua','Qui','Sex','Sáb','Dom']
        nomes_m = ['Jan','Fev','Mar','Abr','Mai','Jun','Jul','Ago','Set','Out','Nov','Dez']

        # Agrupar por mês
        msg = "📋 *Últimas transações*\n\n"
        mes_atual = None
        total_listado = 0
        for tid, desc, valor, cat, data in rows:
            mes_label = f"{nomes_m[data.month-1]} {data.year}"
            if mes_label != mes_atual:
                msg += f"*📅 {mes_label}*\n"
                mes_atual = mes_label
            emoji_c = EMOJI_CAT.get(cat, '💳')
            cod = id_para_codigo(tid)
            desc_curta = (desc or cat).replace('[conjunta] ','').replace('[reserva] ','')[:24]
            dia_txt = f"{data.strftime('%d/%m')}"
            msg += f"• {dia_txt} {emoji_c} {desc_curta} — *{valor:.0f}€*  `{cod}`\n"
            total_listado += valor

        msg += f"\n━━━━━━━━━━━━\n"
        msg += f"💰 Total listado: {total_listado:.0f}€\n"
        msg += f"🗑️ Para apagar: *apaga CÓDIGO*"
        enviar_mensagem(phone_raw, msg)
    except Exception as e:
        log.error(f"ver_transacoes: {e}"); db.session.rollback()
        enviar_mensagem(phone_raw, "Erro ao listar 😕")


def eh_objetivo_poupanca(texto):
    """Deteta intenção de criar objetivo de poupança (muitas variações)."""
    t = texto.lower()
    # Padrões claros de poupança/objetivo
    padroes = [
        'quero poupar', 'poupar para', 'objetivo de poupanca', 'objetivo de poupança',
        'meta de poupanca', 'meta de poupança', 'quero juntar', 'juntar para',
        'poupar dinheiro', 'quero guardar', 'guardar para', 'preciso de poupar',
        'tenho que poupar', 'tenho de poupar', 'criar objetivo', 'novo objetivo',
        'quero ter', 'juntar dinheiro', 'amealhar'
    ]
    if any(p in t for p in padroes):
        return True
    # "X euros para [algo]" com verbo de poupança implícito
    if re.search(r'(?:poupar|juntar|guardar|amealhar)\s+\d+', t):
        return True
    return False


def processar_aporte(phone_raw, usuario, texto):
    """'guardei 50 para o pc' → adiciona ao objetivo (individual ou conjunto)."""
    valor = extrair_valor(texto)
    if valor <= 0:
        enviar_mensagem(phone_raw, "Quanto guardaste? Ex: *guardei 50 para o pc*"); return

    t = texto.lower()
    # Extrair nome do objetivo (depois de "para")
    m_nome = re.search(r'para\s+(?:o\s+|a\s+|os\s+|as\s+)?(.+)', t)
    alvo = m_nome.group(1).strip() if m_nome else ''

    # Procurar objetivo INDIVIDUAL que combine
    obj_ind = None
    if alvo:
        obj_ind = db.session.execute(text(
            "SELECT id, descricao, valor_objetivo, valor_atual FROM objetivos_poupanca "
            "WHERE usuario_id=:u AND concluido=FALSE AND LOWER(descricao) LIKE :a ORDER BY id DESC LIMIT 1"),
            {'u': usuario.id, 'a': f'%{alvo[:15]}%'}).fetchone()

    # Procurar objetivo CONJUNTO que combine
    obj_conj = None
    if alvo and not obj_ind:
        obj_conj = db.session.execute(text(
            "SELECT id, descricao, valor_objetivo FROM objetivos_casal "
            "WHERE LOWER(descricao) LIKE :a ORDER BY id DESC LIMIT 1"),
            {'a': f'%{alvo[:15]}%'}).fetchone()

    try:
        if obj_ind:
            novo_valor = (obj_ind[3] or 0) + valor
            db.session.execute(text(
                "UPDATE objetivos_poupanca SET valor_atual=:v WHERE id=:i"),
                {'v': novo_valor, 'i': obj_ind[0]})
            db.session.commit()
            pct = round(novo_valor / obj_ind[2] * 100) if obj_ind[2] else 0
            barra = '█'*(pct//10) + '░'*(10-pct//10)
            emoji_o = emoji_objetivo(obj_ind[1])
            falta = obj_ind[2] - novo_valor
            msg = f"💰 +{valor:.0f}€ guardado!\n\n"
            msg += f"{emoji_o} *{obj_ind[1]}*\n"
            msg += f"{barra} {pct}%\n"
            msg += f"💶 {novo_valor:.0f}€ de {obj_ind[2]:.0f}€"
            if falta > 0:
                msg += f"\n📊 Falta {falta:.0f}€"
            else:
                msg += f"\n\n🎉 *Objetivo atingido!* Parabéns! 🥳"
                db.session.execute(text("UPDATE objetivos_poupanca SET concluido=TRUE WHERE id=:i"), {'i': obj_ind[0]})
                db.session.commit()
            enviar_mensagem(phone_raw, msg)
        elif obj_conj:
            db.session.execute(text(
                "INSERT INTO aportes_casal (objetivo_id, usuario_id, valor) VALUES (:o,:u,:v)"),
                {'o': obj_conj[0], 'u': usuario.id, 'v': valor})
            db.session.commit()
            total = db.session.execute(text(
                "SELECT COALESCE(SUM(valor),0) FROM aportes_casal WHERE objetivo_id=:o"),
                {'o': obj_conj[0]}).scalar() or 0
            # Quanto cada um contribuiu
            meu_total = db.session.execute(text(
                "SELECT COALESCE(SUM(valor),0) FROM aportes_casal WHERE objetivo_id=:o AND usuario_id=:u"),
                {'o': obj_conj[0], 'u': usuario.id}).scalar() or 0
            pct = round(total / obj_conj[2] * 100) if obj_conj[2] else 0
            barra = '█'*(pct//10) + '░'*(10-pct//10)
            emoji_o = emoji_objetivo(obj_conj[1])
            msg = f"💰 +{valor:.0f}€ no objetivo conjunto!\n\n"
            msg += f"{emoji_o}💑 *{obj_conj[1]}*\n"
            msg += f"{barra} {pct}%\n"
            msg += f"💶 {total:.0f}€ de {obj_conj[2]:.0f}€\n"
            msg += f"🙋 A tua parte: {meu_total:.0f}€"
            enviar_mensagem(phone_raw, msg)
            notificar_parceiro(usuario.phone,
                f"💰 {NOMES_CASAL.get(usuario.phone,'')} meteu {valor:.0f}€ em *{obj_conj[1]}*!\n"
                f"Total: {total:.0f}€ de {obj_conj[2]:.0f}€ ({pct}%)")
        else:
            enviar_mensagem(phone_raw,
                f"Não encontrei o objetivo \"{alvo}\" 🤔\n"
                f"Cria primeiro: *quero poupar X para {alvo}*")
    except Exception as e:
        log.error(f"aporte: {e}"); db.session.rollback()
        enviar_mensagem(phone_raw, "Erro ao guardar 😕")


def enviar_pdf_whatsapp(phone, usuario, mes, ano):
    """Envia o PDF do relatório direto no WhatsApp via WAHA sendFile."""
    import requests as _req
    try:
        WAHA = os.environ.get('WAHA_URL', 'https://evolution-api-production-634b.up.railway.app')
        KEY = os.environ.get('WAHA_API_KEY', 'waha123')
        SESSION = os.environ.get('WAHA_SESSION', 'default')
        chat_id = phone if '@' in phone else f"{phone}@c.us"
        # URL pública do PDF (o WAHA descarrega e envia)
        url_pdf = (f"https://luanabot-production.up.railway.app/api/relatorio"
                   f"?phone={usuario.phone}&token={usuario.phone[:8]}zef&formato=pdf")
        nomes_m = ['Jan','Fev','Mar','Abr','Mai','Jun','Jul','Ago','Set','Out','Nov','Dez']
        fname = f"Relatorio_{nomes_m[mes-1]}_{ano}.pdf"
        r = _req.post(
            f'{WAHA}/api/sendFile',
            headers={'X-Api-Key': KEY, 'Content-Type': 'application/json'},
            json={'session': SESSION, 'chatId': chat_id,
                  'file': {'url': url_pdf, 'filename': fname, 'mimetype': 'application/pdf'},
                  'caption': f'📄 Aqui está o teu relatório de {nomes_m[mes-1]}! 📊'},
            timeout=30)
        if r.status_code in [200, 201]:
            return True
        log.error(f"sendFile falhou: {r.status_code} {r.text[:200]}")
        return False
    except Exception as e:
        log.error(f"enviar_pdf_whatsapp: {e}")
        return False


def iniciar_viagem(phone_raw, usuario, texto):
    """Inicia o modo viagem — agrupa gastos até terminar."""
    nome = re.sub(r'(?:iniciar?|come[çc]ar?|nova|modo)\s+viagem\s*(?:a|para|ao|em|à)?\s*', '', texto.lower()).strip()
    nome = nome.capitalize()[:50] or "Viagem"
    try:
        # Fechar viagens anteriores ativas
        db.session.execute(text("UPDATE viagens SET ativa=FALSE WHERE usuario_id=:u AND ativa=TRUE"), {'u': usuario.id})
        db.session.execute(text("INSERT INTO viagens (usuario_id, nome) VALUES (:u, :n)"), {'u': usuario.id, 'n': nome})
        db.session.commit()
        enviar_mensagem(phone_raw,
            f"✈️ *Modo viagem ativado: {nome}!*\n\n"
            f"A partir de agora agrupo todos os teus gastos.\n"
            f"Regista normalmente (ex: _35 no jantar_).\n\n"
            f"Quando voltares, diz *terminar viagem* para o total 🧳")
    except Exception as e:
        log.error(f"iniciar_viagem: {e}"); db.session.rollback()
        enviar_mensagem(phone_raw, "Erro ao iniciar viagem 😕")

def viagem_ativa(usuario_id):
    """Devolve (id, nome, inicio) da viagem ativa ou None."""
    try:
        return db.session.execute(text(
            "SELECT id, nome, inicio FROM viagens WHERE usuario_id=:u AND ativa=TRUE ORDER BY id DESC LIMIT 1"),
            {'u': usuario_id}).fetchone()
    except Exception:
        return None

def terminar_viagem(phone_raw, usuario):
    """Termina a viagem e dá o resumo total."""
    v = viagem_ativa(usuario.id)
    if not v:
        enviar_mensagem(phone_raw, "Não tens nenhuma viagem ativa 🤔\nInicia com *viagem Algarve*"); return
    try:
        vid, vnome, vinicio = v
        # Gastos desde o início da viagem
        gastos = db.session.execute(text(
            "SELECT categoria, SUM(valor) as t, COUNT(*) FROM despesas "
            "WHERE usuario_id=:u AND data >= :inicio GROUP BY categoria ORDER BY t DESC"),
            {'u': usuario.id, 'inicio': vinicio}).fetchall()
        total = sum(r[1] for r in gastos)
        ndias = max(1, (agora().replace(tzinfo=None) - vinicio).days + 1) if vinicio else 1
        db.session.execute(text("UPDATE viagens SET ativa=FALSE, fim=NOW() WHERE id=:i"), {'i': vid})
        db.session.commit()
        msg = f"🧳 *Viagem terminada: {vnome}!*\n"
        msg += f"━━━━━━━━━━━━━━\n"
        msg += f"💰 Total gasto:  *{total:.0f}€*\n"
        msg += f"📅 Duração:  {ndias} dia{'s' if ndias>1 else ''}\n"
        msg += f"📊 Média:  {total/ndias:.0f}€/dia\n"
        if gastos:
            msg += f"━━━━━━━━━━━━━━\n"
            for cat, t, n in gastos[:6]:
                em = EMOJI_CAT.get(cat, '💳')
                msg += f"{em} {cat.capitalize()}: {t:.0f}€\n"
        msg += f"\n✨ Espero que tenhas aproveitado!"
        enviar_mensagem(phone_raw, msg)
    except Exception as e:
        log.error(f"terminar_viagem: {e}"); db.session.rollback()
        enviar_mensagem(phone_raw, "Erro ao terminar viagem 😕")


# ─── ENABLE BANKING (saldos reais) ───────────────────────────
ENABLE_BASE = "https://api.enablebanking.com"

def _normalizar_chave_pem(raw):
    """Normaliza chave PEM independentemente do formato do Railway.
    Aceita: PEM normal, PEM com \n literais, base64 da chave inteira."""
    import base64 as _b64
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    raw = raw.strip()

    # Tentar base64 puro primeiro (mais fácil de colocar no Railway)
    if not raw.startswith('-'):
        try:
            decoded = _b64.b64decode(raw).decode()
            load_pem_private_key(decoded.encode(), password=None)
            return decoded
        except: pass

    # Tentar variações do PEM
    for tentativa in [
        raw,
        raw.replace('\\n', '\n'),
        raw.replace('\\\\n', '\n'),
        raw.strip().replace('\\n', '\n'),
    ]:
        try:
            load_pem_private_key(tentativa.encode(), password=None)
            return tentativa
        except: continue

    # Último recurso: reconstruir linha a linha
    import re as _re
    texto = raw.replace('\\n', '\n')
    m = _re.search(r'-----BEGIN[^-]+-----(.+?)-----END[^-]+-----', texto, _re.DOTALL)
    if m:
        corpo = ''.join(m.group(1).split())
        linhas = '\n'.join(corpo[i:i+64] for i in range(0, len(corpo), 64))
        reconstruida = f"-----BEGIN PRIVATE KEY-----\n{linhas}\n-----END PRIVATE KEY-----\n"
        load_pem_private_key(reconstruida.encode(), password=None)
        return reconstruida
    raise ValueError("Formato PEM inválido")

def _enable_jwt():
    """Gera JWT RS256 para autenticar na Enable Banking."""
    try:
        import jwt as pyjwt
        app_id = os.environ.get('ENABLE_APP_ID', '')
        priv_key_raw = os.environ.get('ENABLE_PRIVATE_KEY', '')
        if not app_id or not priv_key_raw:
            return None
        priv_key = _normalizar_chave_pem(priv_key_raw)
        iat = int(agora().timestamp())
        token = pyjwt.encode(
            {"iss": "enablebanking.com", "aud": "api.enablebanking.com",
             "iat": iat, "exp": iat + 3600},
            priv_key, algorithm="RS256", headers={"kid": app_id})
        return token
    except Exception as e:
        log.error(f"enable_jwt: {e}")
        return None

def _enable_headers():
    jwt = _enable_jwt()
    return {"Authorization": f"Bearer {jwt}"} if jwt else None

# Bancos PT na Enable Banking (nome exato do ASPSP)
BANCOS_ENABLE = {
    # Nomes exatos do Enable Banking PT
    'bpi': 'BPI', 'banco bpi': 'BPI',
    'cgd': 'Caixa Geral de Depositos', 'caixa': 'Caixa Geral de Depositos', 'cgd pt': 'Caixa Geral de Depositos',
    'caixa geral': 'Caixa Geral de Depositos',
    'millennium': 'Millennium bcp', 'bcp': 'Millennium bcp', 'millenium': 'Millennium bcp',
    'santander': 'Santander Totta', 'santander totta': 'Santander Totta',
    'novobanco': 'Novo Banco', 'novo banco': 'Novo Banco',
    'bankinter': 'Bankinter Portugal', 'activobank': 'ActivoBank',
    'montepio': 'Banco Montepio', 'revolut': 'Revolut',
    'wise': 'Wise', 'n26': 'N26',
    'credito agricola': 'Credito Agricola', 'crédito agrícola': 'Credito Agricola',
    'ing': 'ING', 'deutsche': 'Deutsche Bank', 'unicre': 'Unicre',
    'edenred': 'Edenred', 'paypal': 'PayPal',
}

BANCOS_ENABLE_SEARCH = {
    'bpi': 'bpi', 'banco bpi': 'bpi',
    'cgd': 'caixa', 'caixa': 'caixa', 'cgd pt': 'caixa', 'caixa geral': 'caixa',
    'millennium': 'millennium', 'bcp': 'millennium', 'millenium': 'millennium',
    'santander': 'santander', 'santander totta': 'santander',
    'novobanco': 'novo banco', 'novo banco': 'novo banco',
    'bankinter': 'bankinter', 'activobank': 'activobank',
    'montepio': 'montepio', 'revolut': 'revolut',
    'wise': 'wise', 'n26': 'n26',
    'credito agricola': 'agricola', 'crédito agrícola': 'agricola',
    'ing': 'ing', 'deutsche': 'deutsche', 'unicre': 'unicre',
    'edenred': 'edenred', 'paypal': 'paypal',
}

def _buscar_nome_aspsp_real(banco_chave):
    """Consulta a lista REAL de bancos do Enable Banking e devolve o nome exato esperado.
    Evita o erro 'Wrong ASPSP name provided' por adivinhar nomes errados."""
    import requests as _r2
    palavra_busca = BANCOS_ENABLE_SEARCH.get(banco_chave, banco_chave)
    try:
        headers = _enable_headers()
        if not headers:
            return BANCOS_ENABLE.get(banco_chave)
        r = _r2.get('https://api.enablebanking.com/aspsps?country=PT', headers=headers, timeout=15)
        if r.status_code == 200:
            aspsps = r.json().get('aspsps', [])
            for a in aspsps:
                nome = a.get('name', '')
                if palavra_busca.lower() in nome.lower():
                    return nome
    except Exception as e:
        log.error(f"_buscar_nome_aspsp_real: {e}")
    # Fallback para o nome adivinhado se a busca falhar
    return BANCOS_ENABLE.get(banco_chave)

def enable_ligar_banco(phone_raw, usuario, texto):
    """Inicia ligação a um banco via Enable Banking — gera link de autorização."""
    import requests as _r
    from datetime import timezone as _tz
    t = texto.lower()
    banco = next((b for b in BANCOS_ENABLE if b in t), None)
    if not banco:
        lista = 'BPI, CGD, Millennium, Santander, Novo Banco, Bankinter, ActivoBank, Revolut, Wise'
        enviar_mensagem(phone_raw,
            f"🏦 *Ligar banco real*\n\nQual banco?\n_Diz_ *ligar banco BPI*\n\nDisponíveis: {lista}")
        return
    headers = _enable_headers()
    if not headers:
        enviar_mensagem(phone_raw, "⚠️ O serviço de bancos ainda não está configurado no servidor.")
        return
    try:
        nome_aspsp = _buscar_nome_aspsp_real(banco)
        if not nome_aspsp:
            enviar_mensagem(phone_raw,
                f"Não encontrei o {banco.upper()} na lista de bancos disponíveis 😕\n"
                f"Bancos que funcionam bem: *Revolut*, *Wise*, *N26*")
            return
        body = {
            "access": {"valid_until": (agora().astimezone(_tz.utc) + timedelta(days=90)).isoformat()},
            "aspsp": {"name": nome_aspsp, "country": "PT"},
            "state": f"{usuario.id}_{banco}_{int(agora().timestamp())}",
            "redirect_url": "https://luanabot-production.up.railway.app/api/banco/callback",
            "psu_type": "personal",
        }
        r = _r.post(f"{ENABLE_BASE}/auth", json=body, headers=headers, timeout=20)
        if r.status_code == 200:
            auth_url = r.json().get("url")
            db.session.execute(text(
                "INSERT INTO bancos_ligados (usuario_id, banco, requisition_id, expira, ativo) "
                "VALUES (:u, :b, :s, NOW() + INTERVAL '90 days', FALSE)"),
                {'u': usuario.id, 'b': banco, 's': body["state"]})
            db.session.commit()
            enviar_mensagem(phone_raw,
                f"🏦 *Ligar {banco.upper()}*\n\n"
                f"1️⃣ Abre este link:\n{auth_url}\n\n"
                f"2️⃣ Faz login no banco e autoriza\n"
                f"3️⃣ Volta aqui e diz *saldos reais*\n\n"
                f"_A ligação dura 90 dias 🔒_")
        else:
            log.error(f"enable auth falhou: {r.status_code} {r.text[:200]}")
            erro_txt = r.json().get('message', '') if r.headers.get('content-type','').startswith('application/json') else r.text[:100]
            enviar_mensagem(phone_raw,
                f"Não consegui gerar o link para o {banco.upper()} 😕\n\n"
                f"O banco pode não estar disponível no Enable Banking.\n"
                f"Bancos que funcionam bem: *Revolut*, *Wise*, *N26*\n\n"
                f"_Erro: {erro_txt[:80]}_")
    except Exception as e:
        log.error(f"enable_ligar: {e}"); db.session.rollback()
        enviar_mensagem(phone_raw, "Erro ao ligar banco 😕")

def enable_criar_sessao(code):
    """Troca o code da autorização por uma sessão (account uids)."""
    import requests as _r
    headers = _enable_headers()
    if not headers:
        return None
    try:
        r = _r.post(f"{ENABLE_BASE}/sessions", json={"code": code}, headers=headers, timeout=20)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        log.error(f"enable_sessao: {e}")
    return None

def enable_atualizar_saldos(usuario, silencioso=True):
    """Lê saldos reais via Enable Banking. Devolve lista (banco, saldo)."""
    import requests as _r
    headers = _enable_headers()
    if not headers:
        return []
    resultados = []
    try:
        bancos = db.session.execute(text(
            "SELECT id, banco, account_id FROM bancos_ligados "
            "WHERE usuario_id=:u AND ativo=TRUE AND account_id IS NOT NULL"), {'u': usuario.id}).fetchall()
        for bid, banco, acc_id in bancos:
            try:
                r = _r.get(f"{ENABLE_BASE}/accounts/{acc_id}/balances", headers=headers, timeout=20)
                if r.status_code == 200:
                    bals = r.json().get('balances', [])
                    if bals:
                        # Procurar saldo disponível ou contabilístico
                        saldo = None
                        for b in bals:
                            amt = b.get('balance_amount', {})
                            if amt.get('amount'):
                                saldo = float(amt['amount'])
                                break
                        if saldo is not None:
                            db.session.execute(text(
                                "UPDATE bancos_ligados SET saldo=:s, atualizado=NOW() WHERE id=:i"),
                                {'s': saldo, 'i': bid})
                            db.session.commit()
                            resultados.append((banco, saldo))
            except Exception as e:
                log.error(f"saldo enable {banco}: {e}")
        return resultados
    except Exception as e:
        log.error(f"enable_atualizar: {e}"); db.session.rollback()
        return []

def enable_buscar_todas_contas(usuario):
    """Busca saldos de TODAS as contas/subcontas (cofres incluídos)."""
    import requests as _r
    headers = _enable_headers()
    if not headers: return []
    resultados = []
    try:
        bancos = db.session.execute(text(
            "SELECT id, banco, account_id FROM bancos_ligados WHERE usuario_id=:u AND ativo=TRUE AND account_id IS NOT NULL"),
            {'u': usuario.id}).fetchall()
        for bid, banco, acc_id in bancos:
            # Ler saldo desta conta
            r = _r.get(f"{ENABLE_BASE}/accounts/{acc_id}/balances", headers=headers, timeout=15)
            if r.status_code == 200:
                bals = r.json().get('balances', [])
                for b in bals:
                    amt = b.get('balance_amount', {})
                    if amt.get('amount'):
                        saldo = float(amt['amount'])
                        tipo = b.get('balance_type', '')
                        if tipo in ['CLBD','ITAV','XPCD','interimAvailable','closingBooked','']:
                            resultados.append((banco, acc_id, saldo, tipo))
                            break
        return resultados
    except Exception as e:
        log.error(f"buscar_todas_contas: {e}")
        return []

def enable_alertas_saldo(usuario):
    """Verifica se algum saldo está abaixo do mínimo definido."""
    try:
        alertas = db.session.execute(text(
            "SELECT conta, minimo FROM saldos_contas WHERE usuario_id=:u AND alerta_minimo IS NOT NULL"),
            {'u': usuario.id}).fetchall()
        # simplificado: usar saldo guardado
        for banco_a, minimo_a in alertas:
            saldo_a = db.session.execute(text(
                "SELECT saldo FROM bancos_ligados WHERE usuario_id=:u AND banco=:b AND ativo=TRUE"),
                {'u': usuario.id, 'b': banco_a}).scalar() or 0
            if saldo_a > 0 and saldo_a < minimo_a:
                yield banco_a, saldo_a, minimo_a
    except Exception:
        return

def enviar_saldos_reais(phone_raw, usuario):
    """Mostra os saldos reais dos bancos ligados, incluindo cofres."""
    enviar_mensagem(phone_raw, "🔄 A ler os saldos... 1 segundo!")
    saldos = enable_atualizar_saldos(usuario)
    if not saldos:
        tem = db.session.execute(text("SELECT COUNT(*) FROM bancos_ligados WHERE usuario_id=:u"), {'u': usuario.id}).scalar() or 0
        if tem == 0:
            enviar_mensagem(phone_raw, "🏦 Ainda não ligaste nenhum banco.\n_Diz_ *ligar banco revolut* _para começar_")
        else:
            enviar_mensagem(phone_raw, "🤔 Não consigo ler os saldos agora.\nA ligação pode ter expirado — diz *renovar revolut*")
        return
    total = sum(s for _, s in saldos)
    # Nomes mais amigáveis para as contas
    NOMES_CONTA = {
        'revolut': '💳 Revolut',
        'revolut_pessoal': '💳 Revolut (pessoal)',
        'revolut_conjunta': '💑 Revolut (conjunta)',
        'revolut_cofre_casa': '🏠 Cofre Casa',
        'revolut_cofre_pc': '💻 Cofre PC novo',
    }
    def _nome_conta(banco):
        if banco in NOMES_CONTA:
            return NOMES_CONTA[banco]
        if 'conjunta' in banco: return '💑 Revolut (conjunta)'
        if 'cofre' in banco:
            nome_cofre = banco.replace('revolut_cofre_','').replace('_',' ').title()
            return f"🏦 Cofre {nome_cofre}"
        return f"💳 {banco.split('_')[0].upper()}"
    # Deduplicar e filtrar contas com saldo 0 (cofres vazios não interessam)
    vistos_acc = set()
    saldos_unicos = []
    for banco, saldo in saldos:
        if banco not in vistos_acc:
            vistos_acc.add(banco)
            # Mostrar sempre: pessoal, conjunta, cofres (mesmo vazios)
            if saldo > 0 or any(k in banco for k in ['pessoal','conjunta','cofre']):
                saldos_unicos.append((banco, saldo))
    total = sum(s for _, s in saldos_unicos)
    msg = f"🏦 *Saldos reais*\n━━━━━━━━━━━━━━\n"
    for banco, saldo in saldos_unicos:
        icon = '💚' if saldo > 100 else ('🟡' if saldo > 20 else '🔴')
        nome = _nome_conta(banco.lower())
        msg += f"{icon} {nome}:  *{saldo:.2f}€*\n"
    msg += f"━━━━━━━━━━━━━━\n💰 *Total:* {total:.2f}€"
    # Verificar objetivos ligados a cofres
    try:
        objs = db.session.execute(text(
            "SELECT descricao, valor_objetivo, valor_atual FROM objetivos_poupanca "
            "WHERE usuario_id=:u AND concluido=FALSE ORDER BY id DESC LIMIT 3"),
            {'u': usuario.id}).fetchall()
        if objs:
            # Filtrar objetivos com nomes válidos (não os de teste)
            objs_validos = [(d,v,a) for d,v,a in objs
                           if v and v > 0 and len(d) > 2
                           and 'sabes' not in d.lower() and d.lower() not in ['novo','gasto','poupança']]
            if objs_validos:
                msg += f"\n\n🎯 *Objetivos:*\n"
                for desc_o, val_o, at_o in objs_validos:
                    pct = round((at_o or 0)/val_o*100) if val_o else 0
                    barra = '█'*(pct//10) + '░'*(10-pct//10)
                    msg += f"{emoji_objetivo(desc_o)} {desc_o}: {barra} {pct}%\n"
    except Exception:
        pass
    enviar_mensagem(phone_raw, msg)


# ─── PAGAMENTOS AGENDADOS / PRESTAÇÕES (Klarna, Via Verde, etc) ───
def registar_pagamento_agendado(phone_raw, usuario, texto):
    """Regista débito com data ou compra parcelada (Klarna).
    Ex: 'comprei airpods 200 no klarna, dei 140, faltam 2 de 30 dia 15'
        'via verde 25 dia 8'
        'cofidis 248 dia 28'."""
    t = texto.lower()
    valor = extrair_valor(texto)
    # Detetar dia do mês
    m_dia = re.search(r'dia\s+(\d{1,2})', t)
    dia_mes = int(m_dia.group(1)) if m_dia else None
    # Detetar prestações: "2 de 30", "faltam 2 prestações de 30", "2x 30"
    m_prest = re.search(r'(\d+)\s*(?:presta|x|vezes|meses)\w*\s*(?:de\s+)?(\d+(?:[.,]\d+)?)', t)
    # Entrada: "dei 140", "entrada de 140", "paguei 140 de entrada"
    m_entrada = re.search(r'(?:dei|entrada|paguei)\s+(\d+(?:[.,]\d+)?)', t)
    entrada = float(m_entrada.group(1).replace(',','.')) if m_entrada else 0

    # Nome do pagamento (limpar)
    nome = re.sub(r'comprei|paguei|gastei|no klarna|na klarna|klarna|via verde|dia \d+|\d+(?:[.,]\d+)?\s*(?:€|euros?)?|dei \d+|faltam|presta\w*|de entrada|entrada', '', t)
    nome = ' '.join(w for w in nome.split() if len(w) > 1).strip().capitalize()[:40] or 'Pagamento'

    # Klarna / parcelado
    eh_klarna = 'klarna' in t or m_prest
    categoria, _, _ = categorizar(texto)

    try:
        if m_prest:
            n_prest = int(m_prest.group(1))
            valor_prest = float(m_prest.group(2).replace(',','.'))
            db.session.execute(text(
                "INSERT INTO pagamentos_agendados (usuario_id, nome, valor, dia_mes, prestacoes_total, prestacoes_pagas, categoria) "
                "VALUES (:u, :n, :v, :d, :pt, 0, :c)"),
                {'u': usuario.id, 'n': nome, 'v': valor_prest, 'd': dia_mes or 1,
                 'pt': n_prest, 'c': categoria})
            db.session.commit()
            # Se houve entrada, registar como gasto hoje
            if entrada > 0:
                db.session.add(Despesa(usuario_id=usuario.id, valor=entrada,
                    descricao=f"{nome} (entrada)", categoria=categoria, data=agora().replace(tzinfo=None)))
                db.session.commit()
            total_restante = valor_prest * n_prest
            msg = f"💳 *Compra parcelada registada!*\n"
            msg += f"━━━━━━━━━━━━━━\n"
            msg += f"🛍️ {nome}\n"
            if entrada > 0:
                msg += f"💶 Entrada: {entrada:.0f}€ (registada hoje)\n"
            msg += f"📅 {n_prest} prestações de {valor_prest:.0f}€\n"
            msg += f"💰 Falta pagar: {total_restante:.0f}€\n"
            if dia_mes:
                msg += f"📆 Sai dia {dia_mes} de cada mês\n"
            msg += f"━━━━━━━━━━━━━━\n"
            msg += f"✅ Vou avisar-te antes de cada prestação sair!"
            enviar_mensagem(phone_raw, msg)
        elif dia_mes and valor > 0:
            # Débito mensal fixo (Via Verde, Cofidis, etc)
            db.session.execute(text(
                "INSERT INTO pagamentos_agendados (usuario_id, nome, valor, dia_mes, prestacoes_total, categoria) "
                "VALUES (:u, :n, :v, :d, 999, :c)"),  # 999 = recorrente sem fim
                {'u': usuario.id, 'n': nome, 'v': valor, 'd': dia_mes, 'c': categoria})
            db.session.commit()
            msg = f"📅 *Débito agendado!*\n"
            msg += f"━━━━━━━━━━━━━━\n"
            msg += f"💳 {nome}\n"
            msg += f"💰 {valor:.0f}€\n"
            msg += f"📆 Sai dia {dia_mes} de cada mês\n"
            msg += f"━━━━━━━━━━━━━━\n"
            msg += f"✅ Vou avisar-te 3 dias antes!"
            enviar_mensagem(phone_raw, msg)
        elif dia_mes and valor == 0:
            # Débito VARIÁVEL (Via Verde, luz, água) — valor muda todo o mês
            db.session.execute(text(
                "INSERT INTO pagamentos_agendados (usuario_id, nome, valor, dia_mes, prestacoes_total, categoria, variavel) "
                "VALUES (:u, :n, 0, :d, 999, :c, TRUE)"),
                {'u': usuario.id, 'n': nome, 'd': dia_mes, 'c': categoria})
            db.session.commit()
            msg = f"📅 *Débito variável agendado!*\n"
            msg += f"━━━━━━━━━━━━━━\n"
            msg += f"💳 {nome}\n"
            msg += f"📆 Sai dia {dia_mes} de cada mês\n"
            msg += f"💡 Valor varia — eu pergunto-te no dia!\n"
            msg += f"━━━━━━━━━━━━━━\n"
            msg += f"✅ No dia {dia_mes} pergunto quanto foi 😉"
            enviar_mensagem(phone_raw, msg)
        else:
            enviar_mensagem(phone_raw,
                "🤔 Não percebi bem. Tenta:\n"
                "_via verde dia 8_ (valor varia, eu pergunto)\n"
                "_cofidis 248 dia 28_ (valor fixo)\n"
                "_airpods 200 no klarna, dei 140, faltam 2 de 30 dia 15_")
    except Exception as e:
        log.error(f"pagamento agendado: {e}"); db.session.rollback()
        enviar_mensagem(phone_raw, "Erro ao registar 😕")

def ver_agenda(phone_raw, usuario):
    """Mostra o calendário financeiro do mês."""
    try:
        hoje = agora()
        pagamentos = db.session.execute(text(
            "SELECT nome, valor, dia_mes, prestacoes_total, prestacoes_pagas, categoria "
            "FROM pagamentos_agendados WHERE usuario_id=:u AND ativo=TRUE ORDER BY dia_mes"),
            {'u': usuario.id}).fetchall()
        # Juntar com despesas futuras
        futuras = DespesaFutura.query.filter_by(usuario_id=usuario.id, pago=False).all()

        if not pagamentos and not futuras:
            enviar_mensagem(phone_raw,
                "📅 Sem pagamentos agendados.\n\n_Adiciona com_ *via verde 25 dia 8* _ou_ *cofidis 248 dia 28*")
            return

        nomes_mes = ['Jan','Fev','Mar','Abr','Mai','Jun','Jul','Ago','Set','Out','Nov','Dez']
        msg = f"📅 *Agenda — {nomes_mes[hoje.month-1]}*\n"
        msg += f"━━━━━━━━━━━━━━\n"
        total_mes = 0
        for nome, valor, dia, p_tot, p_pag, cat in pagamentos:
            em = EMOJI_CAT.get(cat, '💳')
            falta_dias = dia - hoje.day
            aviso = ""
            if 0 <= falta_dias <= 3:
                aviso = f" ⚠️ {falta_dias}d!" if falta_dias > 0 else " ⚠️ HOJE!"
            prest_txt = ""
            if p_tot < 999:
                prest_txt = f" ({p_pag+1}/{p_tot})"
            msg += f"`{dia:>2}` {em} {nome}{prest_txt} — *{valor:.0f}€*{aviso}\n"
            if falta_dias >= 0:
                total_mes += valor
        msg += f"━━━━━━━━━━━━━━\n"
        msg += f"💰 Falta sair este mês: *{total_mes:.0f}€*"
        enviar_mensagem(phone_raw, msg)
    except Exception as e:
        log.error(f"ver_agenda: {e}"); db.session.rollback()
        enviar_mensagem(phone_raw, "Erro ao ver agenda 😕")

def calcular_valor_seguro(usuario):
    """Disponível menos os pagamentos pendentes até ao salário = valor seguro."""
    disp, p = calcular_disponivel(usuario)
    hoje = agora()
    try:
        pendentes = db.session.execute(text(
            "SELECT COALESCE(SUM(valor),0) FROM pagamentos_agendados "
            "WHERE usuario_id=:u AND ativo=TRUE AND dia_mes >= :d"),
            {'u': usuario.id, 'd': hoje.day}).scalar() or 0
    except Exception:
        pendentes = 0
    return disp, pendentes, max(disp - pendentes, 0)


def ligar_objetivo_cofre(usuario_id, desc_objetivo):
    """Tenta ligar um objetivo a um cofre do Revolut pelo nome."""
    try:
        # Procurar cofre com nome parecido
        cofres = db.session.execute(text(
            "SELECT id, banco, account_id FROM bancos_ligados "
            "WHERE usuario_id=:u AND banco LIKE 'revolut_cofre_%' AND ativo=TRUE"),
            {'u': usuario_id}).fetchall()
        desc_lower = desc_objetivo.lower().replace(' ','_')
        for cid, banco, acc_id in cofres:
            nome_cofre = banco.replace('revolut_cofre_','').lower()
            if nome_cofre in desc_lower or desc_lower in nome_cofre:
                return acc_id, banco
    except Exception:
        pass
    return None, None

def saldo_cofre_objetivo(usuario_id, desc_objetivo):
    """Lê o saldo real do cofre ligado a um objetivo."""
    import requests as _r
    acc_id, _ = ligar_objetivo_cofre(usuario_id, desc_objetivo)
    if not acc_id:
        return None
    headers = _enable_headers()
    if not headers:
        return None
    try:
        r = _r.get(f"{ENABLE_BASE}/accounts/{acc_id}/balances", headers=headers, timeout=10)
        if r.status_code == 200:
            bals = r.json().get('balances', [])
            if bals:
                return float(bals[0].get('balance_amount',{}).get('amount', 0))
    except Exception:
        pass
    return None



# ─── DETEÇÃO AUTOMÁTICA DE TRANSAÇÕES REVOLUT ──────────────────────────
def enable_buscar_transacoes_recentes(usuario, minutos=35):
    """Busca transações do Revolut dos últimos N minutos."""
    import requests as _r
    from datetime import timezone as _tz
    headers = _enable_headers()
    if not headers:
        return []
    try:
        # Buscar contas Revolut pessoal
        contas = db.session.execute(text(
            "SELECT account_id, banco FROM bancos_ligados "
            "WHERE usuario_id=:u AND ativo=TRUE AND account_id IS NOT NULL "
            "AND banco NOT LIKE '%cofre%'"),
            {'u': usuario.id}).fetchall()
        if not contas:
            return []
        todas_txs = []
        agora_utc = agora()
        desde = agora_utc - timedelta(minutes=minutos)
        for acc_id, banco in contas:
            try:
                r = _r.get(f"{ENABLE_BASE}/accounts/{acc_id}/transactions",
                    headers=headers,
                    params={
                        'date_from': desde.strftime('%Y-%m-%dT%H:%M:%S'),
                        'date_to': agora_utc.strftime('%Y-%m-%dT%H:%M:%S'),
                    }, timeout=15)
                if r.status_code == 200:
                    txs = r.json().get('transactions', [])
                    for tx in txs:
                        tx['_banco'] = banco
                    todas_txs.extend(txs)
            except Exception as e:
                log.error(f"enable_txs {banco}: {e}")
        return todas_txs
    except Exception as e:
        log.error(f"enable_buscar_txs: {e}")
        return []

def tx_ja_registada(usuario_id, tx_id, valor, desc, data_tx):
    """Verifica se esta transação já foi registada (por ID externo ou por match desc+valor+data)."""
    try:
        # Verificar por transaction_id externo
        if tx_id:
            existe = db.session.execute(text(
                "SELECT COUNT(*) FROM despesas WHERE usuario_id=:u AND descricao LIKE :tid"),
                {'u': usuario_id, 'tid': f'%[txid:{tx_id}]%'}).scalar()
            if existe:
                return True
        # Verificar por match desc+valor dentro de ±2h
        from datetime import timezone as _tz
        data_dt = datetime.fromisoformat(data_tx.replace('Z','')) if isinstance(data_tx, str) else data_tx
        existe2 = db.session.execute(text(
            "SELECT COUNT(*) FROM despesas WHERE usuario_id=:u "
            "AND ABS(valor - :v) < 0.01 "
            "AND data BETWEEN :d1 AND :d2 "
            "AND descricao ILIKE :desc"),
            {'u': usuario_id, 'v': abs(valor),
             'd1': data_dt - timedelta(hours=2),
             'd2': data_dt + timedelta(hours=2),
             'desc': f'%{desc[:15]}%' if desc else '%'}).scalar()
        return bool(existe2)
    except Exception as e:
        log.error(f"tx_ja_registada: {e}")
        return False

def processar_tx_revolut_auto(phone_raw, usuario, tx):
    """Processa uma transação Revolut detetada automaticamente."""
    valor_raw = tx.get('transaction_amount', {}).get('amount', 0)
    valor = float(valor_raw) if valor_raw else 0
    # Só despesas (valores negativos ou créditos de lojas)
    if valor >= 0:
        return  # receita ou transferência positiva — ignorar
    valor = abs(valor)
    desc = (tx.get('creditor_name') or tx.get('remittance_information') or
            tx.get('additional_information') or 'Pagamento Revolut').strip()
    tx_id = tx.get('transaction_id') or tx.get('internal_transaction_id') or ''
    data_tx = tx.get('booking_date') or tx.get('value_date') or agora().isoformat()
    banco_origem = tx.get('_banco', '')
    eh_conjunta = 'conjunta' in banco_origem

    # Verificar se já foi registada
    if tx_ja_registada(usuario.id, tx_id, valor, desc, data_tx):
        return

    # Categorizar
    cat, loja, _ = categorizar(desc)
    loja_n = loja or desc[:30]
    em = EMOJI_CAT.get(cat, '💳')

    # Registar automaticamente — gastos da conjunta marcados [conjunta] (não afetam o para gastar pessoal)
    try:
        prefixo = '[conjunta] ' if eh_conjunta else ''
        desc_bd = f"{prefixo}{loja_n} [txid:{tx_id}]" if tx_id else f"{prefixo}{loja_n}"
        db.session.add(Despesa(
            usuario_id=usuario.id, valor=valor,
            descricao=desc_bd, categoria=cat,
            data=agora().replace(tzinfo=None)))
        db.session.commit()
        log.info(f"Tx Revolut auto: {loja_n} {valor}€ → {cat} (conjunta={eh_conjunta})")
    except Exception as e:
        log.error(f"registar tx auto: {e}")
        db.session.rollback()
        return

    if eh_conjunta:
        # Avisar OS DOIS — gasto saiu da conta conjunta, não afeta o disponível pessoal
        meu_nome_tx = NOMES_CASAL.get(usuario.phone, 'Alguém')
        msg_tx = (f"{em} *{loja_n}* — {valor:.2f}€\n"
                  f"💑 Saiu da conta conjunta (detetado automaticamente)")
        enviar_mensagem(phone_raw, msg_tx)
        notificar_parceiro(usuario.phone, msg_tx)
    else:
        # Notificar utilizador (despesa pessoal)
        enviar_mensagem(phone_raw,
            f"{em} *{loja_n}* — {valor:.2f}€\n"
            f"💜 Detetado no Revolut e registado automaticamente!\n"
            f"_Diz 'corrige' se a categoria estiver errada_")

def sincronizar_revolut():
    """Job a cada 30 min: deteta novas transações Revolut e regista automaticamente.
    Também atualiza o saldo real e verifica saldo baixo em tempo real (não só às 8h)."""
    with app.app_context():
        for u in Usuario.query.all():
            if not u.phone: continue
            try:
                phone_raw = f"{u.phone}@lid"
                txs = enable_buscar_transacoes_recentes(u, minutos=35)
                for tx in txs:
                    processar_tx_revolut_auto(phone_raw, u, tx)
                if txs:
                    # Houve movimento — atualizar saldo e verificar se ficou baixo
                    try:
                        enable_atualizar_saldos(u, silencioso=True)
                        _verificar_saldo_baixo_usuario(u)
                    except Exception as e:
                        log.error(f"sincronizar_revolut saldo {u.phone}: {e}")
            except Exception as e:
                log.error(f"sincronizar_revolut {u.phone}: {e}")


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

        if estado == 'aniv_apartar':
            nome_aniv = dados_estado.get('nome','')
            dias_aniv = dados_estado.get('dias', 0)
            fila_aniv = dados_estado.get('fila', [])
            limpar_estado(phone)
            palavras = t.strip().split()
            # Deteta "sim" em qualquer posição da frase (mais flexível)
            tem_sim = any(p in ['sim','s','yes','claro','quero'] for p in palavras)
            tem_nao = any(p in ['nao','não','n','no'] for p in palavras) and not tem_sim
            if tem_nao:
                enviar_mensagem(phone_raw, f"Ok! Podes sempre apartar depois dizendo 'prenda {nome_aniv} 50€' 💪")
                _avancar_fila_aniversarios(phone_raw, phone, fila_aniv)
            elif tem_sim:
                set_estado(phone, 'aniv_apartar_valor', {'nome': nome_aniv, 'fila': fila_aniv})
                enviar_mensagem(phone_raw, f"💝 Quanto queres apartar para a prenda do(a) {nome_aniv}?")
            else:
                # Mensagem não relacionada — sai do estado e processa normalmente
                processar_texto(phone_raw, phone, texto)
            return
        if estado == 'aniv_apartar_valor':
            nome_aniv = dados_estado.get('nome','')
            fila_aniv = dados_estado.get('fila', [])
            limpar_estado(phone)

            # Tentar apanhar MÚLTIPLOS valores+nomes na mesma frase
            # ex: "20 sogra 100 taty" ou "sogra 20 taty 100"
            todos_nomes = [nome_aniv] + [a['nome'] for a in fila_aniv]
            pares_detetados = _extrair_pares_valor_nome(texto, todos_nomes)

            if len(pares_detetados) >= 2:
                # Registar todos de uma vez — objetivo + despesa (sai do disponível)
                feedback = []
                total_apartado = 0
                for nome_p, valor_p in pares_detetados.items():
                    try:
                        db.session.execute(text(
                            "INSERT INTO objetivos_poupanca (usuario_id, descricao, valor_objetivo, valor_atual) VALUES (:u,:d,:v,0)"),
                            {'u':usuario.id,'d':f'🎁 Prenda {nome_p}','v':valor_p})
                        db.session.add(Despesa(usuario_id=usuario.id, valor=valor_p,
                            descricao=f'🎁 Prenda {nome_p} (apartado)', categoria='presentes',
                            data=agora().replace(tzinfo=None)))
                        feedback.append(f"💝 {nome_p}: {valor_p:.0f}€")
                        total_apartado += valor_p
                    except Exception: pass
                db.session.commit()
                enviar_mensagem(phone_raw,
                    "✅ Apartado!\n" + "\n".join(feedback) +
                    f"\n💳 Total {total_apartado:.0f}€ já saiu do disponível\nVê em 'objetivos' 🎁")
                fila_restante = [a for a in fila_aniv if a['nome'] not in pares_detetados]
                _avancar_fila_aniversarios(phone_raw, phone, fila_restante)
                return

            # Se a mensagem parece OUTRO comando, sai do fluxo e processa normalmente
            palavras_cmd = ['entrei','sai','saí','gastei','quanto','picos','quero','nike','poupar',
                            'juntar','vamos','viagem','objetivo','meta','recebi','saldo','lembra',
                            'comprei','wishlist','como estou','resumo','ajuda','apaga','património']
            if any(w in t for w in palavras_cmd) or len(t.split()) > 4:
                limpar_estado(phone)
                processar_texto(phone_raw, phone, texto); return

            valor_prenda = extrair_valor(texto)
            if valor_prenda > 0:
                try:
                    db.session.execute(text(
                        "INSERT INTO objetivos_poupanca (usuario_id, descricao, valor_objetivo, valor_atual) VALUES (:u,:d,:v,0)"),
                        {'u':usuario.id,'d':f'🎁 Prenda {nome_aniv}','v':valor_prenda})
                    db.session.add(Despesa(usuario_id=usuario.id, valor=valor_prenda,
                        descricao=f'🎁 Prenda {nome_aniv} (apartado)', categoria='presentes',
                        data=agora().replace(tzinfo=None)))
                    db.session.commit()
                    enviar_mensagem(phone_raw, f"💝 Apartei {valor_prenda:.0f}€ para a prenda do(a) {nome_aniv}!\n💳 Já saiu do disponível deste mês\nVê em 'objetivos' 🎁")
                except Exception: enviar_mensagem(phone_raw, "Erro 😕")
            _avancar_fila_aniversarios(phone_raw, phone, fila_aniv)
            return
        if estado == 'conjunta_sem_desc':
            valor_c = dados_estado.get('valor', 0)
            limpar_estado(phone)
            processar_despesa(phone_raw, usuario, f"{texto} {valor_c} na conjunta")
            return
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

        if estado == 'confirmar_pessoal_conjunta':
            d = dados_estado
            txt_orig = d.get('texto','')
            limpar_estado(phone)
            if 'ignora' in t or 'cancelar' in t or 'cancel' in t:
                enviar_mensagem(phone_raw, "Ok, cancelado 👍"); return
            eh_conjunta = any(p in t for p in ['conjunta','conjunto','casal','nós','nos dois'])
            if eh_conjunta:
                # Registar na conjunta
                val = extrair_valor(txt_orig)
                if val > 0:
                    cat, loja, _ = categorizar(txt_orig)
                    loja_n = loja or txt_orig[:30]
                    try:
                        db.session.add(Despesa(usuario_id=usuario.id, valor=val,
                            descricao=f"[conjunta] {loja_n}", categoria=cat,
                            data=agora().replace(tzinfo=None)))
                        db.session.commit()
                        em = EMOJI_CAT.get(cat,'💳')
                        enviar_mensagem(phone_raw,
                            f"✅ {em} *{loja_n}* — {val:.2f}€\n💑 Registado na *conjunta*")
                    except Exception as e:
                        log.error(f"conj pessoal: {e}"); db.session.rollback()
                        enviar_mensagem(phone_raw, "Erro ao registar 😕")
                else:
                    enviar_mensagem(phone_raw, "Não percebi o valor 🤔")
            elif any(p in t for p in ['pessoal','meu','minha','eu']):
                processar_despesa(phone_raw, usuario, txt_orig)
            else:
                # Não percebeu — repetir
                set_estado(phone, 'confirmar_pessoal_conjunta', d)
                enviar_mensagem(phone_raw, "Diz *pessoal* ou *conjunta* 😊")
            return

        if estado == 'confirmar_divisao_conta':
            d_div = dados_estado
            parte_div = d_div.get('parte', 0)
            limpar_estado(phone)
            palavras_div = t.strip().split()
            tem_sim_div = any(p in ['sim','s','yes','claro'] for p in palavras_div)
            tem_nao_div = any(p in ['nao','não','n','no'] for p in palavras_div) and not tem_sim_div
            if tem_nao_div:
                enviar_mensagem(phone_raw, "Ok, sem problema! 😊"); return
            if tem_sim_div and parte_div > 0:
                desc_div = texto
                for p in ['sim','s','yes','claro']:
                    desc_div = re.sub(rf'\b{p}\b', '', desc_div, flags=re.IGNORECASE).strip()
                desc_div = desc_div or 'Conta dividida'
                try:
                    cat_div, loja_div, _ = categorizar(desc_div)
                    db.session.add(Despesa(usuario_id=usuario.id, valor=parte_div,
                        descricao=desc_div[:90], categoria=cat_div, data=agora().replace(tzinfo=None)))
                    db.session.commit()
                    enviar_mensagem(phone_raw, f"✅ Registado! {parte_div:.2f}€ — {loja_div or desc_div}")
                except Exception as e:
                    log.error(f"confirmar_divisao_conta: {e}"); db.session.rollback()
                    enviar_mensagem(phone_raw, "Erro ao registar 😕")
            else:
                processar_texto(phone_raw, phone, texto)
            return

        if estado == 'confirmar_debito_variavel':
            d = dados_estado
            pid = d.get('pid'); nome_d = d.get('nome',''); cat_d = d.get('cat','outros')
            limpar_estado(phone)
            # Usar média ou valor dado
            if 'media' in t or 'média' in t:
                media = db.session.execute(text("SELECT valor_medio FROM pagamentos_agendados WHERE id=:i"), {'i': pid}).scalar() or 0
                valor_d = media
            else:
                valor_d = extrair_valor(texto)
            if valor_d <= 0:
                enviar_mensagem(phone_raw, "Não percebi o valor 🤔 Tenta só o número, ex: *24*"); return
            try:
                # Registar o gasto
                db.session.add(Despesa(usuario_id=usuario.id, valor=valor_d,
                    descricao=nome_d, categoria=cat_d, data=agora().replace(tzinfo=None)))
                # Atualizar média (média móvel simples)
                media_atual = db.session.execute(text("SELECT valor_medio FROM pagamentos_agendados WHERE id=:i"), {'i': pid}).scalar() or 0
                nova_media = valor_d if media_atual == 0 else round((media_atual + valor_d) / 2, 2)
                db.session.execute(text("UPDATE pagamentos_agendados SET valor_medio=:m WHERE id=:i"), {'m': nova_media, 'i': pid})
                db.session.commit()
                em_d = EMOJI_CAT.get(cat_d, '💳')
                enviar_mensagem(phone_raw,
                    f"✅ Registado!\n{em_d} {nome_d} — {valor_d:.0f}€\n\n_Média atualizada: {nova_media:.0f}€_")
            except Exception as e:
                log.error(f"confirmar debito: {e}"); db.session.rollback()
                enviar_mensagem(phone_raw, "Erro ao registar 😕")
            return

        if estado == 'objetivo_valor_pendente':
            d_pend = dados_estado
            valor_pend = extrair_valor(texto)
            if valor_pend == 0:
                enviar_mensagem(phone_raw, "Preciso de um número 🙂 Ex: 200€"); return
            limpar_estado(phone)
            emoji_d = emoji_objetivo(d_pend.get('desc','')) if d_pend.get('desc') else '🎯'
            set_estado(phone, 'objetivo_tudo', {'valor': valor_pend, 'desc': d_pend.get('desc',''), 'emoji': emoji_d})
            desc_txt = f" para *{d_pend['desc']}*" if d_pend.get('desc') else ""
            msg = f"{emoji_d} *Novo objetivo: {valor_pend:.0f}€*{desc_txt}\n\n"
            msg += f"Conta-me numa mensagem:\n"
            if not d_pend.get('desc'):
                msg += f"📝 *Para quê* (ex: Viagem, PS5)\n"
            msg += f"📅 *Até quando* (ex: dezembro, daqui a 6 meses)\n"
            msg += f"💰 *Quanto já tens* (ou diz que não tens)\n\n"
            if d_pend.get('conjunto'):
                msg += f"_(já percebi que é com o teu par 💑)_"
            else:
                msg += f"_Se for a dois, diz \"com a Luana\" 💑_"
            enviar_mensagem(phone_raw, msg)
            return

        if estado == 'objetivo_tudo':
            d = dados_estado
            valor_obj = d.get('valor', 0)
            desc_obj = d.get('desc', '')

            # Tentar IA primeiro (melhor compreensão)
            ia = interpretar_objetivo_ia(texto)
            if ia:
                if not desc_obj:
                    desc_obj = ia['nome']
                mes_alvo = ia['mes_alvo']
                inicial = ia['valor_inicial']
                eh_conjunto = ia['conjunto']
            else:
                # Fallback manual (regex)
                t_resp = texto.lower()
                if not desc_obj:
                    stop_n = {'para','o','a','os','as','em','no','na','ate','até','daqui','meses','mes','mês',
                              'ja','já','tenho','nao','não','euros','euro','de','do','da','dezembro','janeiro',
                              'fevereiro','marco','março','abril','maio','junho','julho','agosto','setembro',
                              'outubro','novembro','um','uma','uns','umas','quero','poupar','juntar','guardar',
                              'ir','comprar','novo','nova','meu','minha','com','que','e'}
                    t_nome = re.sub(r'\d+', '', t_resp)
                    palavras_n = [w for w in re.findall(r'[a-zà-ú]+', t_nome) if w not in stop_n and len(w) >= 2]
                    desc_obj = ' '.join(palavras_n[:3]).capitalize() if palavras_n else 'Poupança'
                meses_nome = {'janeiro':1,'fevereiro':2,'marco':3,'março':3,'abril':4,'maio':5,'junho':6,
                              'julho':7,'agosto':8,'setembro':9,'outubro':10,'novembro':11,'dezembro':12}
                mes_alvo = next((n for nm,n in meses_nome.items() if nm in t_resp), None)
                m_ini = re.search(r'(?:tenho|com|guardado)\s+(\d+)', t_resp)
                inicial = float(m_ini.group(1)) if m_ini else 0
                eh_conjunto = any(p in t_resp for p in ['luana','casal','juntos','conjunto','nós','nos dois'])

            # Calcular meses
            if mes_alvo:
                meses_falta = (mes_alvo - agora().month) % 12 or 12
            else:
                meses_falta = 6
                mes_alvo = ((agora().month - 1 + 6) % 12) + 1

            emoji_o = emoji_objetivo(desc_obj)
            por_mes = round((valor_obj - inicial) / meses_falta, 2) if meses_falta > 0 else valor_obj
            nomes_m_o = ['Jan','Fev','Mar','Abr','Mai','Jun','Jul','Ago','Set','Out','Nov','Dez']
            mes_txt = nomes_m_o[mes_alvo-1] if mes_alvo else '?'

            limpar_estado(phone)
            try:
                if eh_conjunto:
                    # Objetivo CONJUNTO
                    db.session.execute(text(
                        "INSERT INTO objetivos_casal (descricao, valor_objetivo) VALUES (:d,:v)"),
                        {'d': desc_obj, 'v': valor_obj})
                    obj_id = db.session.execute(text(
                        "SELECT id FROM objetivos_casal ORDER BY id DESC LIMIT 1")).scalar()
                    if inicial > 0:
                        db.session.execute(text(
                            "INSERT INTO aportes_casal (objetivo_id, usuario_id, valor) VALUES (:o,:u,:v)"),
                            {'o': obj_id, 'u': usuario.id, 'v': inicial})
                    db.session.commit()
                    por_mes_cada = por_mes / 2
                    nome_sub = desc_obj.replace(' ', '')[:15]
                    msg_o = f"✅ *Objetivo conjunto criado!* {emoji_o}💑\n"
                    msg_o += f"━━━━━━━━━━━━━━\n"
                    msg_o += f"{emoji_o} *{desc_obj}*\n"
                    msg_o += f"💰 Meta:  {valor_obj:.0f}€\n"
                    if inicial > 0:
                        msg_o += f"💶 Já têm:  {inicial:.0f}€\n"
                        msg_o += f"📊 Falta:  {valor_obj-inicial:.0f}€\n"
                    msg_o += f"📅 Meta:  {mes_txt} ({meses_falta} meses)\n"
                    msg_o += f"💪 ~{por_mes:.0f}€/mês ({por_mes_cada:.0f}€ cada)\n"
                    msg_o += f"━━━━━━━━━━━━━━\n"
                    msg_o += f"💡 *Dica:* criem um cofre *conjunto* no Revolut chamado *\"{nome_sub}\"*\n"
                    msg_o += f"   e metam lá ~{por_mes_cada:.0f}€/mês cada 🎯\n\n"
                    msg_o += f"Digam *guardei 50 para {desc_obj.lower()}* quando contribuírem"
                    enviar_mensagem(phone_raw, msg_o)
                    # Notificar o parceiro
                    notificar_parceiro(usuario.phone,
                        f"{emoji_o} *Novo objetivo conjunto!*\n"
                        f"{NOMES_CASAL.get(usuario.phone,'')} criou: *{desc_obj}* ({valor_obj:.0f}€)\n"
                        f"💪 ~{por_mes_cada:.0f}€/mês cada até {mes_txt}")
                else:
                    # Objetivo INDIVIDUAL
                    db.session.execute(text(
                        "INSERT INTO objetivos_poupanca (usuario_id,descricao,valor_objetivo,valor_atual) VALUES (:u,:d,:v,:a)"),
                        {'u':usuario.id,'d':desc_obj,'v':valor_obj,'a':inicial})
                    db.session.commit()
                    pct_ini = round(inicial/valor_obj*100) if valor_obj > 0 else 0
                    barra = '█'*(pct_ini//10) + '░'*(10-pct_ini//10)
                    nome_sub = desc_obj.replace(' ', '')[:15]
                    msg_o = f"✅ *Objetivo criado!* {emoji_o}\n"
                    msg_o += f"━━━━━━━━━━━━━━\n"
                    msg_o += f"{emoji_o} *{desc_obj}*\n"
                    msg_o += f"💰 Meta:  {valor_obj:.0f}€\n"
                    if inicial > 0:
                        msg_o += f"💶 Já tens:  {inicial:.0f}€\n"
                        msg_o += f"📊 Falta:  {valor_obj-inicial:.0f}€\n"
                        msg_o += f"{barra} {pct_ini}%\n"
                    msg_o += f"📅 Meta:  {mes_txt} ({meses_falta} {'mês' if meses_falta==1 else 'meses'})\n"
                    msg_o += f"💪 ~{por_mes:.0f}€/mês\n"
                    msg_o += f"━━━━━━━━━━━━━━\n"
                    msg_o += f"💡 *Dica:* cria um cofre no Revolut chamado *\"{nome_sub}\"*\n"
                    msg_o += f"   e mete lá ~{por_mes:.0f}€/mês 🎯\n\n"
                    msg_o += f"Diz *guardei 50 para {desc_obj.lower()}* quando puseres dinheiro"
                    enviar_mensagem(phone_raw, msg_o)
            except Exception as e:
                log.error(f"obj tudo: {e}"); db.session.rollback()
                enviar_mensagem(phone_raw, "Erro ao criar objetivo 😕")
            return

        if estado == 'objetivo_nome':
            valor_obj = dados_estado.get('valor', 0)
            nome_obj = texto.strip()[:30].capitalize()
            if len(nome_obj) < 2:
                enviar_mensagem(phone_raw, "Diz-me um nome para o objetivo 😊"); return
            emoji_obj = emoji_objetivo(nome_obj)
            set_estado(phone, 'objetivo_data', {'valor': valor_obj, 'desc': nome_obj, 'emoji': emoji_obj})
            enviar_mensagem(phone_raw,
                f"{emoji_obj} *{nome_obj}: {valor_obj:.0f}€*\n\n📅 Até quando queres atingir?\nEx: _dezembro, março, daqui a 3 meses_")
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
            nomes_m = ['Jan','Fev','Mar','Abr','Mai','Jun','Jul','Ago','Set','Out','Nov','Dez']
            mes_txt = nomes_m[mes_alvo-1] if mes_alvo else '?'
            # Perguntar valor inicial (estilo GranaZen)
            emoji_od = dados_estado.get('emoji', emoji_objetivo(desc_obj))
            set_estado(phone, 'objetivo_inicial', {
                'valor': valor_obj, 'desc': desc_obj, 'mes_txt': mes_txt,
                'por_mes': por_mes, 'meses': meses_falta, 'emoji': emoji_od})
            enviar_mensagem(phone_raw,
                f"{emoji_od} *{desc_obj}: {valor_obj:.0f}€*\n"
                f"📅 Meta: {mes_txt} ({meses_falta} meses)\n"
                f"💰 ~{por_mes:.0f}€/mês\n\n"
                f"Já tens algum valor guardado para começar?\nDiz o valor ou 'não' 😊")
            return

        if estado == 'objetivo_inicial':
            d = dados_estado
            inicial = 0 if any(p in t for p in ['nao','não','zero','nada','n']) else extrair_valor(texto)
            limpar_estado(phone)
            try:
                meses_falta_calc = d.get('meses', 6)
                hoje_obj = agora().replace(tzinfo=None)
                mes_total = hoje_obj.month - 1 + meses_falta_calc
                ano_meta = hoje_obj.year + mes_total // 12
                mes_meta = mes_total % 12 + 1
                import calendar as _cal_obj
                dia_meta = min(hoje_obj.day, _cal_obj.monthrange(ano_meta, mes_meta)[1])
                data_meta_calc = hoje_obj.replace(year=ano_meta, month=mes_meta, day=dia_meta).date()
                db.session.execute(text(
                    "INSERT INTO objetivos_poupanca (usuario_id,descricao,valor_objetivo,valor_atual,data_meta,por_mes_sugerido) "
                    "VALUES (:u,:d,:v,:a,:dm,:pm)"),
                    {'u':usuario.id,'d':d.get('desc','Objetivo'),'v':d.get('valor',0),'a':inicial,
                     'dm':data_meta_calc,'pm':d.get('por_mes',0)})
                db.session.commit()
                falta = d.get('valor',0) - inicial
                pct_ini = round(inicial/d.get('valor',1)*100) if d.get('valor',0)>0 else 0
                barra = '█'*(pct_ini//10) + '░'*(10-pct_ini//10)
                emoji_final = d.get('emoji', '🎯')
                msg_obj = f"✅ *Objetivo criado!* {emoji_final}\n"
                msg_obj += f"━━━━━━━━━━━━━━\n"
                msg_obj += f"{emoji_final} *{d.get('desc')}*\n"
                msg_obj += f"💰 Meta:  {d.get('valor',0):.0f}€\n"
                if inicial > 0:
                    msg_obj += f"💶 Já tens:  {inicial:.0f}€\n"
                    msg_obj += f"📊 Falta:  {falta:.0f}€\n"
                    msg_obj += f"{barra} {pct_ini}%\n"
                msg_obj += f"📅 Meta:  {d.get('mes_txt')} ({d.get('meses')} meses)\n"
                msg_obj += f"💪 ~{d.get('por_mes',0):.0f}€/mês\n"
                msg_obj += f"━━━━━━━━━━━━━━\n"
                msg_obj += f"💡 Diz *guardei 50 para {d.get('desc','').lower()}* para registar"
                enviar_mensagem(phone_raw, msg_obj)
            except Exception as e:
                log.error(f"obj inicial: {e}"); db.session.rollback()
                enviar_mensagem(phone_raw, "Erro ao criar objetivo 😕")
            return

        if estado == 'confirmar_salario':
            if any(p in t for p in ['sim','yes','correto','certo','exato','e isso','é isso']):
                valor = dados_estado.get('valor', 0); limpar_estado(phone)
                # Se o pagamento é amanhã/futuro → guardar pendente
                pag = dia_pagamento_usuario(usuario, agora().year, agora().month)
                if pag.date() > agora().date():
                    guardar_salario_pendente(usuario.id, valor, pag.date())
                    enviar_mensagem(phone_raw,
                        f"Boa! 💰 {valor:.2f}€ anotado!\n"
                        f"No dia {pag.strftime('%d/%m')} confirmo a entrada e mando o plano 😉")
                else:
                    processar_receita(phone_raw, usuario, f"recebi {valor}")
            elif tem_numero(texto):
                limpar_estado(phone); processar_receita(phone_raw, usuario, texto)
            else:
                limpar_estado(phone); enviar_mensagem(phone_raw, "Ok, diz: recebi X euros 💰")
            return

        if estado == 'aguardar_loja_talao':
            valor_talao = dados_estado.get('valor', 0); limpar_estado(phone)
            if valor_talao > 0:
                processar_despesa(phone_raw, usuario, f"{texto} {valor_talao}€")
            return

        if estado == 'recibo_repergunta':
            # Se mandou valor/recibo antes da repergunta, trata como aguardar_recibo
            estado = 'aguardar_recibo'
        if estado == 'aguardar_recibo':
            data_pag_str = dados_estado.get('data_pagamento','')
            if any(p in t for p in ['sim','yes','quero','manda','envia']):
                # mantém estado para apanhar o PDF/valor a seguir
                enviar_mensagem(phone_raw, "Manda o PDF ou foto do recibo 📄")
            elif tem_numero(texto):
                limpar_estado(phone)
                valor_rec = extrair_valor(texto)
                if valor_rec > 0 and data_pag_str:
                    from datetime import date as _date
                    dp = datetime.strptime(data_pag_str, '%Y-%m-%d').date()
                    if dp > agora().date():
                        guardar_salario_pendente(usuario.id, valor_rec, dp)
                        enviar_mensagem(phone_raw,
                            f"Boa! 💰 {valor_rec:.2f}€ anotado!\n"
                            f"Amanhã quando o dinheiro cair eu confirmo e mando o plano todo 😉")
                        return
                # data já passou ou sem data → processa já
                processar_receita(phone_raw, usuario, texto)
            elif any(p in t for p in ['nao','não','ainda nao','ainda não']):
                set_estado(phone, 'recibo_repergunta', {'data_pagamento': data_pag_str})
                enviar_mensagem(phone_raw, "Ok! Volto a perguntar à tarde 😉 Se chegar entretanto, manda-me 📄")
            else:
                limpar_estado(phone)
                processar_texto(phone_raw, phone, texto)
            return

        # NOTA: estado 'escolher_modo' NAO bloqueia mais o bot
        # O utilizador pode escolher modo mas continuar a usar o bot normalmente
        if estado == 'escolher_modo':
            if 'maximo' in t or 'máximo' in t or t.strip() == 'modo 1' or t.strip() == 'opcao 1':
                set_modo(usuario.id, 'maximo'); limpar_estado(phone)
                enviar_mensagem(phone_raw, "💎 Modo Máximo ativado! Modo monge ON 🧘"); return
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
                enviar_mensagem(phone_raw, "💎 Modo Máximo ativado!"); return
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
        # Depositar na reserva: "reserva 50", "mete 50 na reserva", "guarda 50 na reserva"
        m_dep_res = re.search(r'(?:reserva|reforça reserva|reforçar reserva|guarda(?:r)?|mete(?:r)?|p[õo]e)\s+(\d+(?:[.,]\d+)?)\s*(?:€|euros?)?\s*(?:na |para a |[àa] )?(?:reserva|fundo|emergencia|emergência)', t)
        if not m_dep_res:
            m_dep_res = re.search(r'reserva\s+(\d+(?:[.,]\d+)?)', t)
        if m_dep_res and 'gastei' not in t and 'tirei' not in t and 'usei' not in t:
            val_res = float(m_dep_res.group(1).replace(',','.'))
            try:
                atual_res = get_reserva(usuario.id)
                set_reserva(usuario.id, atual_res + val_res)
                novo_res = atual_res + val_res
                msg_res = f"🛡️ +{val_res:.0f}€ na reserva de emergência!\n💰 Total: *{novo_res:.0f}€*"
                if novo_res >= 2500:
                    msg_res += f"\n\n🎉 Já tens uma reserva sólida! Bom trabalho 💪"
                enviar_mensagem(phone_raw, msg_res)
            except Exception as e:
                log.error(f"deposito reserva: {e}")
                enviar_mensagem(phone_raw, "Erro ao guardar na reserva 😕")
            return
        if re.search(r'(?:gastei|usei|tirei|meti|fui|busquei).{0,20}reserva', t) or \
           re.search(r'reserva.{0,20}(?:gastei|usei|tirei)', t):
            processar_gasto_reserva(phone_raw, usuario, texto); return
        if any(p in t for p in ['quanto tenho na reserva','saldo da reserva','ver reserva','minha reserva']):
            r = get_reserva(usuario.id)
            enviar_mensagem(phone_raw, f"🛡️ Reserva de emergência: {r:.2f}€\n\nPara usar: 'gastei 30 da reserva'"); return

        # ── PICOS / HORAS EXTRAS (so Ruben) ──────────────────────────
        if phone == PHONE_RUBEN:
            t_norm = t.replace('í','i').replace('á','a').replace('à','a')
            # Entrada E saída na mesma mensagem: "entrei às 9 e saí às 23h30"
            if re.search(r'\bentr(ei|e)\b', t_norm) and re.search(r'\bsai\b', t_norm):
                m_ent = re.search(r'entr(?:ei|e)\s*(?:as|às)?\s*([\d:h,\.]+)', t_norm)
                m_sai = re.search(r'sai\s*(?:as|às)?\s*([\d:h,\.]+)', t_norm)
                r1 = pico_entrada(phone, f"entrei {m_ent.group(1) if m_ent else ''}")
                r2 = pico_saida(phone, f"sai {m_sai.group(1) if m_sai else ''}")
                enviar_mensagem(phone_raw, r2); return
            if re.match(r'^entr(ei|e|a)', t_norm):
                enviar_mensagem(phone_raw, pico_entrada(phone, t_norm)); return
            if re.match(r'^sai', t_norm) or t_norm.strip() in ['sai','saiu']:
                enviar_mensagem(phone_raw, pico_saida(phone, t_norm)); return
            if any(p in t_norm for p in ['quantas horas','horas fiz','horas hoje','horas trabalhei']) or t_norm.strip() == 'picos hoje':
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
        # ── PRENDA para aniversário ──
        m_prenda = re.search(r'prenda\s+([a-záàâãéêíóôõúç]+)\s+(\d+)', t)
        if m_prenda:
            nome_p = m_prenda.group(1).capitalize()
            valor_p = float(m_prenda.group(2))
            try:
                db.session.execute(text(
                    "INSERT INTO objetivos_poupanca (usuario_id, descricao, valor_objetivo, valor_atual) VALUES (:u,:d,:v,0)"),
                    {'u':usuario.id,'d':f'Prenda {nome_p}','v':valor_p})
                db.session.commit()
                enviar_mensagem(phone_raw, f"💝 Apartado {valor_p:.0f}€ para prenda do(a) {nome_p}! 🎁")
            except Exception: enviar_mensagem(phone_raw, "Erro 😕")
            return
        # ── ANIVERSÁRIOS ──
        if any(p in t for p in ['aniversario','aniversário','faz anos']) or t.strip() == 'aniversarios':
            processar_aniversario(phone_raw, usuario, texto); return

        # ── APRENDER ──
        # Formato 1: "aprende que X é Y"
        m = re.search(r'aprende que (.+?) (?:é|e|sao|são) (?:da categoria |categoria )?(\w[\w\s]*)', t)
        # Formato 2 natural: "X é loja de roupa" / "X é uma loja de Y" / "X é supermercado"
        if not m:
            m = re.search(r'^(.+?)\s+(?:é|eh|e)\s+(?:uma?\s+)?(?:loja\s+de\s+|categoria\s+(?:de\s+)?)?([a-zà-ú]+)\s*$', t)
        if m and any(w in t for w in ['é ','eh ','loja','categoria','aprende','ensina']):
            chave = m.group(1).strip().strip('"\'').replace('gastei ','').replace('paguei ','')
            chave = re.sub(r'^(?:o |a |os |as )', '', chave)
            chave = re.sub(r'^\d+\s*(?:euros?|€)?\s*(?:na |no |em )?', '', chave).strip()
            cat_raw = m.group(2).strip()
            # Mapear palavras comuns para categorias
            mapa_cat = {'roupa':'roupa','comida':'supermercado','supermercado':'supermercado',
                'restaurante':'restaurante','cafe':'cafe','café':'cafe','tecnologia':'tecnologia',
                'gasolina':'combustivel','combustivel':'combustivel','saude':'saude','saúde':'saude',
                'casa':'casa','carro':'carro','lazer':'lazer','desporto':'desporto','viagem':'viagem',
                'perfume':'pessoal','perfumes':'pessoal','beleza':'pessoal','sapatilhas':'roupa',
                'calçado':'roupa','calcado':'roupa','fastfood':'fastfood'}
            cat = mapa_cat.get(cat_raw) or normalizar_categoria(cat_raw)
            if cat in CATEGORIAS_VALIDAS and len(chave) > 1:
                if guardar_aprendida(chave, cat):
                    msg_apr = f"🧠 Aprendido! *{chave.capitalize()}* = {cat.capitalize()} para sempre 😎"
                    # Corrigir o último gasto se for dessa loja
                    try:
                        ult = db.session.execute(text(
                            "SELECT id, categoria FROM despesas WHERE usuario_id=:u "
                            "AND LOWER(descricao) LIKE :c ORDER BY id DESC LIMIT 1"),
                            {'u': usuario.id, 'c': f'%{chave[:12]}%'}).fetchone()
                        if ult and ult[1] != cat:
                            db.session.execute(text("UPDATE despesas SET categoria=:c WHERE id=:i"),
                                {'c': cat, 'i': ult[0]})
                            db.session.commit()
                            msg_apr += f"\n✏️ Corrigi também o último gasto para {cat.capitalize()}"
                    except Exception:
                        db.session.rollback()
                    enviar_mensagem(phone_raw, msg_apr)
                else:
                    enviar_mensagem(phone_raw, "Ops 😕")
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

        # ── PREVISÃO / AO RITMO ATUAL ──
        if any(p in t for p in ['ao ritmo atual','previsao','previsão','como vou acabar','como acabo','quanto vou gastar']):
            previsao_fim_mes(phone_raw, usuario); return
        # ── ORÇAMENTO INTELIGENTE ──
        if any(p in t for p in ['sugerir orcamento','sugere orcamento','orcamento inteligente','media gastos','quanto devia gastar']):
            sugerir_orcamento(phone_raw, usuario); return
        # ── OBJETIVOS CASAL ──
        if any(p in t for p in ['objetivos casal','objetivo casal ver','metas casal']):
            ver_objetivos_casal(phone_raw, usuario); return
        # ── RECORRENTES ──
        if any(p in t for p in ['recorrente','recorrentes','compras mensais']):
            processar_recorrentes(phone_raw, usuario, texto); return
        # ── COMO ESTOU? (dashboard natural) ──
        if any(p in t for p in ['como estou','como e que estou','situacao atual','dashboard','overview','resumo geral','visao geral']):
            como_estou(phone_raw, usuario); return
        # ── ASSINATURAS ──
        if any(p in t for p in ['assinatura','assinaturas','subscricoes','subscricao']):
            processar_assinaturas(phone_raw, usuario, texto); return
        # ── GRUPO CASA ──
        if t.strip() == 'casa' or t.strip() == 'gastos casa' or (t.startswith('casa') and len(t) < 12 and not tem_numero(texto)):
            mostrar_grupo(phone_raw, usuario, 'casa'); return
        # ── GRUPO CARRO ──
        if t.strip() == 'carro' or t.strip() == 'gastos carro' or (t.startswith('carro') and len(t) < 12 and not tem_numero(texto)):
            mostrar_grupo(phone_raw, usuario, 'carro'); return
        # ── METAS / DESAFIOS DE CATEGORIA ──
        if any(p in t for p in ['maximo ','máximo ','limite ','desafio','meta de ','metas','desafios']) and (tem_numero(texto) or t.strip() in ['metas','desafios']):
            if not any(p in t for p in ['poupar','objetivo']):
                processar_meta_categoria(phone_raw, usuario, texto); return
        # ── AJUDA / BOAS VINDAS ──
        # ── PAGAMENTO DÍVIDA PESSOAL ──────────────────────────────────
        m_divida = re.search(r'paguei\s+(\d+(?:[.,]\d+)?)\s+[àa]\s+([a-záàâãéêíóôõúç]+)', t)
        if m_divida:
            valor_pago = float(m_divida.group(1).replace(',','.'))
            credor = m_divida.group(2).capitalize()
            saldo_atual, parcela = get_saldo_divida(usuario.id, credor)
            if saldo_atual > 0:
                novo_saldo = max(0, saldo_atual - valor_pago)
                set_saldo_divida(usuario.id, credor, novo_saldo, parcela)
                # Registar como gasto
                despesa = Despesa(usuario_id=usuario.id, valor=valor_pago, categoria='outros',
                    descricao=f'Pagamento divida {credor}', data=agora().replace(tzinfo=None))
                db.session.add(despesa); db.session.commit()
                meu_nome = NOMES_CASAL.get(usuario.phone, 'Parceiro')
                if novo_saldo > 0:
                    meses_rest = int(novo_saldo / parcela) + (1 if novo_saldo % parcela > 0 else 0)
                    msg_eu = (f"✅ Pago {valor_pago:.0f}€ à {credor}\n"
                              f"💳 Saldo restante: {novo_saldo:.0f}€\n"
                              f"📅 ~{meses_rest} mes(es) para terminar")
                    msg_parceiro = (f"💸 {meu_nome} pagou {valor_pago:.0f}€ da dívida\n"
                                    f"💳 Falta: {novo_saldo:.0f}€\n"
                                    f"📅 ~{meses_rest} mes(es) para terminar")
                else:
                    msg_eu = f"🎉 Dívida à {credor} paga! Zero euros em dívida 💪"
                    msg_parceiro = f"🎉 {meu_nome} pagou a dívida toda! Estão quites 💪"
                enviar_mensagem(phone_raw, msg_eu)
                notificar_parceiro(usuario.phone, msg_parceiro)
                return
        # ──────────────────────────────────────────────────────────────
        # ── RELATÓRIO PDF ────────────────────────────────────────────
        if any(p in t for p in ['relatorio pdf','relatório pdf','pdf do mes','pdf do mês','manda o pdf','relatorio em pdf','quero o pdf','exporta pdf','relatorio','relatório']):
            mes_r = agora().month; ano_r = agora().year
            enviar_mensagem(phone_raw, "📄 A preparar o teu relatório... 1 segundo!")
            if enviar_pdf_whatsapp(phone_raw, usuario, mes_r, ano_r):
                pass  # PDF enviado com sucesso
            else:
                url_pdf = (f"https://luanabot-production.up.railway.app/api/relatorio"
                           f"?phone={usuario.phone}&token={usuario.phone[:8]}zef&formato=pdf")
                enviar_mensagem(phone_raw, f"📄 Descarrega aqui:\n{url_pdf}")
            return
        if (t.strip() in ['gastos','gastos?','resumo','resumo?','meus gastos','os meus gastos','quanto gastei','onde gastei','onde gastei dinheiro','onde foi o dinheiro','no que gastei','em que gastei','quanto gastei este mes','quanto gastei este mês','ver gastos','os gastos']
                or any(p in t for p in ['resumo do mes','resumo do mês','resumo mensal','onde foi o meu dinheiro','no que gastei'])):
            enviar_resumo(phone_raw, usuario); return
        if any(p in t for p in ['ajuda','help','/start','comandos']):
            enviar_ajuda(phone_raw); return

        if t in ['ola','olá','oi','boas','hey','hello'] or any(p in t for p in ['bom dia','boa tarde','boa noite']):
            enviar_boas_vindas(phone_raw, usuario, phone); return

        # ── MODO TESO ──
        if any(p in t for p in ['estou teso','tou teso','sem dinheiro','estou liso','estou falido','sem um tostao','sem um tostão','à rasca','a rasca','estou apertado','estou sem dinheiro','sem guita','liso']):
            modo_teso(phone_raw, usuario); return

        # ── PERGUNTA SOBRE LIMITE/CATEGORIA ──────────────────────────
        if any(p in t for p in ['passei o limite','passei o orcamento','passei o orçamento','já passei','ja passei','estou dentro do orcamento','quanto gastei em','gastei muito em']):
            # Detetar categoria mencionada
            cat_q = None
            for c in CATEGORIAS_VALIDAS:
                if c in t: cat_q = c; break
            for alias, c in ALIAS_CAT.items():
                if alias in t: cat_q = c; break
            if 'gasolina' in t or 'combustivel' in t or 'combustível' in t: cat_q = 'combustivel'
            if cat_q:
                mes_q=agora().month; ano_q=agora().year
                gasto_q = db.session.execute(text(
                    "SELECT COALESCE(SUM(valor),0) FROM despesas WHERE usuario_id=:u AND categoria=:c "
                    "AND EXTRACT(month FROM data)=:m AND EXTRACT(year FROM data)=:y"),
                    {'u':usuario.id,'c':cat_q,'m':mes_q,'y':ano_q}).scalar() or 0
                # Limite: meta definida ou BASE_COMBUSTIVEL para gasolina
                limite_q = None
                if cat_q == 'combustivel': limite_q = BASE_COMBUSTIVEL
                meta_q = db.session.execute(text(
                    "SELECT limite FROM metas_categoria WHERE usuario_id=:u AND categoria=:c AND mes=:m AND ano=:y"),
                    {'u':usuario.id,'c':cat_q,'m':mes_q,'y':ano_q}).scalar()
                if meta_q: limite_q = meta_q
                emoji_q = EMOJI_CAT.get(cat_q,'💳')
                msg_q = f"{emoji_q} *{cat_q.capitalize()}* este mês: {gasto_q:.0f}€"
                if limite_q:
                    if gasto_q > limite_q:
                        msg_q += f"\n🚨 Orçamento: {limite_q:.0f}€ — *passaste {gasto_q-limite_q:.0f}€!*"
                    else:
                        msg_q += f"\n✅ Orçamento: {limite_q:.0f}€ — ainda tens {limite_q-gasto_q:.0f}€"
                enviar_mensagem(phone_raw, msg_q); return
        # ── GASOLINA ──
        gasolina_keywords = ['gasolina mais barata','posto mais barato','gasolina barata',
                             'valor gasolina','preco gasolina','preço gasolina','onde e a gasolina',
                             'onde é a gasolina','combustivel mais barato']
        municipios = ['barreiro','moita','seixal','almada','montijo','palmela','alcochete','setubal','setúbal']
        e_municipio_gas = any(m in t for m in municipios) and any(p in t for p in ['gasolina','combustivel','posto','barata','barato','preco','preço','valor'])
        e_na_local = bool(re.search(r'^e\s+(na|no)\s+(moita|barreiro|seixal|almada|montijo)[\s?!.]*$', t.strip()))
        if any(p in t for p in gasolina_keywords) or e_municipio_gas or e_na_local:
            gasolina_barata(phone_raw, t); return

        if any(p in t for p in ['recebemos','metemos','depositamos','deposito','meti','coloquei','pus','transferi']) and 'conjunta' in t and tem_numero(texto):
            registar_deposito_conjunta(phone_raw, usuario, texto); return
        # Gasto conjunta sem descricao clara — perguntar em que
        if 'conjunta' in t and tem_numero(texto) and not any(p in t for p in ['quanto','tenho','sobra','resta','ver','recebemos','metemos','depositamos']):
            cat_c, _, nome_loja_c = categorizar(texto)
            if cat_c == 'outros' and nome_loja_c == 'Gasto':
                valor_c = extrair_valor(texto)
                set_estado(phone, 'conjunta_sem_desc', {'valor': valor_c})
                enviar_mensagem(phone_raw, f"💑 {valor_c:.0f}€ na conjunta — em que? (ex: cinema, jantar, farmacia)")
                return
        # ── CONJUNTA ──
        if 'conjunta' in t and any(p in t for p in ['quanto','tenho','sobra','resta','ver']):
            enviar_conjunta(phone_raw, usuario); return

        # ── QUANTO TENHO / SALDO ──
        # Registar/ver saldo de conta ANTES do quanto tenho
        if re.search(r'\bsaldo\b', t):
            if tem_numero(texto):
                if registar_saldo_conta(phone_raw, usuario, texto): return
            else:
                # "saldo bankinter" sem número → ver saldo dessa conta
                contas_s = ['bpi','cgd','caixa','bankinter','revolut','trade republic','tr','millennium','santander','wise']
                conta_s = next((c for c in contas_s if c in t), None)
                if conta_s:
                    ver_patrimonio(phone_raw, usuario); return
        # "quanto posso gastar" → valor seguro (desconta pagamentos pendentes)
        if any(p in t for p in ['quanto posso gastar','posso gastar quanto','valor seguro','quanto gasto sem problema']):
            disp_s, pendentes_s, seguro_s = calcular_valor_seguro(usuario)
            msg_s = f"💰 *Saldo disponível:* {disp_s:.0f}€\n"
            if pendentes_s > 0:
                msg_s += f"\n📅 *Pagamentos pendentes:*\n"
                hoje_s = agora()
                pgs = db.session.execute(text(
                    "SELECT nome, valor, dia_mes, categoria FROM pagamentos_agendados "
                    "WHERE usuario_id=:u AND ativo=TRUE AND dia_mes >= :d ORDER BY dia_mes"),
                    {'u': usuario.id, 'd': hoje_s.day}).fetchall()
                for nome_s, val_s, dia_s, cat_s in pgs:
                    em_s = EMOJI_CAT.get(cat_s, '💳')
                    msg_s += f"{em_s} {nome_s} → {val_s:.0f}€ (dia {dia_s})\n"
                msg_s += f"\n💡 *Valor seguro para gastar:*\n*{seguro_s:.0f}€*"
            else:
                msg_s += f"\n✅ Sem pagamentos pendentes — podes gastar à vontade!"
            enviar_mensagem(phone_raw, msg_s); return
        if any(p in t for p in ['posso gastar hoje','quanto hoje','quanto posso gastar','gastar hoje','quanto por dia','orcamento de hoje','orçamento de hoje']):
            enviar_orcamento_hoje(phone_raw, usuario); return

        if any(p in t for p in ['quanto tenho','quanto me resta','quanto sobra']):
            foco_qt = None
            if 'conjunta' in t: foco_qt = 'conjunta'
            elif 'reserva' in t or 'emergencia' in t or 'emergência' in t: foco_qt = 'reserva'
            elif 'poupan' in t: foco_qt = 'poupanca'
            enviar_quanto_tenho(phone_raw, usuario, foco_qt); return

        # ── RESUMO ──
        if any(p in t for p in ['resumo anterior','mes passado','mes anterior']):
            mes_ant = agora().month-1 if agora().month>1 else 12
            ano_ant = agora().year if agora().month>1 else agora().year-1
            enviar_resumo(phone_raw, usuario, mes_ant, ano_ant); return

        if any(p in t for p in ['resumo','quanto gastei','gastos do mes','gastos deste mes','ver gastos']):
            enviar_resumo(phone_raw, usuario); return

        # ── PLANO ──
        if any(p in t for p in ['onde vai o dinheiro','onde vai o meu dinheiro','distribuir','como distribuir','onde meto','onde coloco','onde ponho','dividir salario','distribuicao','distribuição']):
            enviar_onde_vai_dinheiro(phone_raw, usuario); return
        if any(p in t for p in ['plano contas','plano de contas','onde poupar','contas bancarias','minhas contas']):
            enviar_plano_contas(phone_raw, usuario); return
        if any(p in t for p in ['plano','transferencia','transferência','distribuicao','ver plano']):
            enviar_plano_mes(phone_raw, usuario); return

        # ── SCORE ──
        if any(p in t for p in ['score','conquistas','badges','pontuacao']):
            enviar_score(phone_raw, usuario); return

        # ── OBJETIVO POUPANÇA ──
        # ── APRESENTAÇÃO / QUEM CRIOU ────────────────────────────────
        if any(p in t for p in ['quem te criou','quem és','quem é o teu criador','quem te fez','quem te programou','quem é o criador']):
            enviar_mensagem(phone_raw,
                "🧑‍💻 Fui criado pelo *tuga27*!\n\n"
                "Mais conhecido por:\n"
                "🎬 Criar o *Zé Flix* — plataforma de streaming de filmes e séries\n"
                "💸 Criar o *Zé das Finanças* — o teu gestor financeiro no WhatsApp\n\n"
                "Um homem, dois Zés 😎 Diz olá da minha parte!"); return
        # ── APORTE A OBJETIVO: "guardei 50 para o pc" ────────────────
        if re.search(r'\b(?:guardei|meti|poupei|juntei)\b.*\bpara\b', t) and tem_numero(texto):
            processar_aporte(phone_raw, usuario, texto); return
        if eh_objetivo_poupanca(texto):
            processar_objetivo_poupanca(phone_raw, usuario, texto); return
        if any(p in t for p in ['objetivos','ver objetivos']):
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
        if any(p in t for p in ['meti','entrou','depositei']) and any(p in t for p in ['na minha conta','na conta','no banco','no bpi','no revolut']) and tem_numero(texto):
            valor_extra = extrair_valor(texto)
            if valor_extra > 0:
                db.session.add(Receita(usuario_id=usuario.id, valor=valor_extra, descricao='Extra', data=agora().replace(tzinfo=None)))
                db.session.commit()
                enviar_mensagem(phone_raw, f"💰 +{valor_extra:.0f}€ registado na tua conta!\nSe quiseres atualizar o disponível diz 'quanto tenho' 😊")
                return
        verbos_receita = ['recebi','ganhei','ordenado','salario','salário','vencimento','caiu o','entrou o',
                          'entrou a','pagaram','mandaram-me','transferiram','devolveram','reembolso',
                          'caiu a guita','entrou a massa','entrou dinheiro','recebi os','recebi das',
                          'recebi o','prémio','premio','comissao','comissão','duodecimo','duodécimo',
                          'subsidio','subsídio','caiu na conta','ja caiu','já caiu','ja entrou','já entrou']
        if any(p in t for p in verbos_receita) and tem_numero(texto):
            if eh_dinheiro_extra(t):
                processar_dinheiro_extra(phone_raw, usuario, texto); return
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
        # Wishlist: "quero X", "curtia X", "ando a ver X", "gostava de X"
        gatilho_wish = (t.startswith('quero ') or t.startswith('curtia ') or t.startswith('gostava ')
                        or 'ando a ver' in t or 'estou a pensar comprar' in t or 'preciso de' in t
                        or 'ando de olho' in t or 'está-me a chamar' in t or 'um dia compro' in t
                        or 'tenho de comprar' in t or 'gostava de ter' in t or 'estou tentado' in t)
        if gatilho_wish and not any(p in t for p in ['poupar','gastar',' ver ','saber','juntar','guardar',' que ','os meus','o meu','consultar','mostrar','quanto']):
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

        # -- X PAGOU / MARCAR SPLIT PAGO (alguem pagou-me) --
        m_pagou = re.search(r'([A-Za-zÀ-ú]{2,})\s+pagou', t)
        if m_pagou:
            pessoa_pagou = m_pagou.group(1).capitalize()
            try:
                r = db.session.execute(text(
                    "UPDATE splitting SET pago=TRUE WHERE usuario_id=:u AND LOWER(pessoa)=LOWER(:p) AND pago=FALSE AND descricao NOT LIKE '[EU DEVO]%' RETURNING descricao,valor_cada"),
                    {'u':usuario.id,'p':pessoa_pagou}).fetchone()
                db.session.commit()
                if r:
                    enviar_mensagem(phone_raw, f"OK {pessoa_pagou} pagou {r[1]:.2f}EUR -- {r[0]}!")
                    meu_nome_p = NOMES_CASAL.get(usuario.phone, 'Alguem')
                    parceiro_phone_p = get_parceiro_phone(usuario.phone)
                    nome_parceiro_p = NOMES_CASAL.get(parceiro_phone_p, '').lower() if parceiro_phone_p else ''
                    if parceiro_phone_p and pessoa_pagou.lower() == nome_parceiro_p:
                        notificar_parceiro(usuario.phone, f"{meu_nome_p} confirmou que recebeu os {r[1]:.2f}EUR! Divida fechada.")
                else: enviar_mensagem(phone_raw, f"Nao encontrei splits pendentes com {pessoa_pagou}")
            except Exception as e:
                log.error(f"pagou: {e}"); enviar_mensagem(phone_raw, "Erro")
            return

        # -- JA PAGUEI A/AO [PESSOA] (eu liquidei o que devia) --
        m_ja_paguei = re.search(r'(?:ja|j\u00e1)\s+paguei\s+(?:ao|\u00e0|a)\s+([A-Za-z\u00c0-\u00fa]{2,})', t)
        if m_ja_paguei:
            pessoa_paga = m_ja_paguei.group(1).capitalize()
            try:
                r = db.session.execute(text(
                    "UPDATE splitting SET pago=TRUE WHERE usuario_id=:u AND LOWER(pessoa)=LOWER(:p) AND pago=FALSE AND descricao LIKE '[EU DEVO]%' RETURNING descricao,valor_cada"),
                    {'u':usuario.id,'p':pessoa_paga}).fetchone()
                db.session.commit()
                if r:
                    desc_limpa = r[0].replace('[EU DEVO] ', '')
                    enviar_mensagem(phone_raw, f"OK! Marquei como pago -- {r[1]:.2f}EUR ao {pessoa_paga} ({desc_limpa})")
                    meu_nome_jp = NOMES_CASAL.get(usuario.phone, 'Alguem')
                    parceiro_phone_jp = get_parceiro_phone(usuario.phone)
                    nome_parceiro_jp = NOMES_CASAL.get(parceiro_phone_jp, '').lower() if parceiro_phone_jp else ''
                    if parceiro_phone_jp and pessoa_paga.lower() == nome_parceiro_jp:
                        notificar_parceiro(usuario.phone, f"{meu_nome_jp} pagou-te os {r[1]:.2f}EUR que devia! Divida fechada.")
                else:
                    enviar_mensagem(phone_raw, f"Nao encontrei nenhuma divida tua ao {pessoa_paga} em aberto")
            except Exception as e:
                log.error(f"ja_paguei: {e}"); enviar_mensagem(phone_raw, "Erro")
            return

        # ── DÍVIDAS ──
        if any(p in t for p in ['ativar lembrete nif','ligar lembrete nif','lembra-me do nif','lembra me do nif','quero lembrete de nif']):
            try:
                db.session.execute(text("UPDATE usuarios SET lembrar_nif=TRUE WHERE id=:u"), {'u': usuario.id})
                db.session.commit()
                enviar_mensagem(phone_raw, "✅ Lembrete do NIF ativado!\nA partir de agora aviso-te a cada gasto 💳\nPara desligar: *desativar lembrete nif*")
            except Exception as e:
                log.error(f"ativar nif: {e}"); db.session.rollback()
            return

        if any(p in t for p in ['desativar lembrete nif','desligar lembrete nif','para de lembrar o nif','já não quero lembrete nif']):
            try:
                db.session.execute(text("UPDATE usuarios SET lembrar_nif=FALSE WHERE id=:u"), {'u': usuario.id})
                db.session.commit()
                enviar_mensagem(phone_raw, "👍 Lembrete do NIF desativado!")
            except Exception as e:
                log.error(f"desativar nif: {e}"); db.session.rollback()
            return

        m_tag_q = re.search(r'(?:quanto gastei|gastos|quanto)\s+(?:em\s+|com\s+)?#(\w+)', t)
        if m_tag_q:
            processar_consulta_tag(phone_raw, usuario, m_tag_q.group(1)); return

        m_dividir = re.search(r'dividir\s+(?:a\s+)?conta\s+(?:de\s+)?(\d+(?:[.,]\d+)?)\s*(?:€|eur|euros)?\s*(?:por|entre)\s+(\d+)', t)
        if not m_dividir:
            m_dividir = re.search(r'dividir\s+(\d+(?:[.,]\d+)?)\s*(?:€|eur|euros)?\s*(?:por|entre)\s+(\d+)', t)
        if m_dividir:
            processar_dividir_conta(phone_raw, usuario, m_dividir.group(1), m_dividir.group(2)); return

        m_fixo_set = re.search(r'(?:mudar |alterar |trocar )?fixo\s+([a-zà-ú0-9]+(?:\s+[a-zà-ú0-9]+)?)\s+(?:para\s+)?([\d]+(?:[.,]\d+)?)', t)
        if m_fixo_set:
            processar_alterar_fixo(phone_raw, usuario, m_fixo_set.group(1), m_fixo_set.group(2)); return

        if any(p in t for p in ['devo ','deve-me','devem-me','me deve']) and tem_numero(texto):
            processar_dividas(phone_raw, usuario, texto); return

        # ── MODO DISCRETO ──
        if any(p in t for p in ['limpa conversa','apaga mensagens','modo discreto','limpar chat']):
            modo_discreto(phone_raw); return

        # ── VW TAIGO — calcula km ao abastecer ──
        if any(p in t for p in ['abasteci','meti gasolina','pus gasolina']) and tem_numero(texto) and not re.search(r'\d+\s*km', t):
            valor_gas = extrair_valor(texto)
            if valor_gas > 5:
                consumo = getattr(usuario, 'carro_consumo_l100', None) or 6.0
                carro = getattr(usuario, 'carro_nome', None) or 'carro'
                litros = round(valor_gas / 1.9, 1)
                km_est = round(litros / consumo * 100)
                processar_despesa(phone_raw, usuario, texto)
                enviar_mensagem(phone_raw, f"🚗 Com {valor_gas:.0f}€ tens ~{litros:.1f}L\n📍 Dá para ~{km_est} km no {carro}!\nManda foto do odómetro 📸")
                return

        # ── RESPOSTAS CURTAS ──
        respostas_curtas = {'obrigada','obrigado','obg','thanks','fixe','ok','okay','boa','top','perfeito','ótimo','otimo','valeu','grato','grata'}
        if t.strip() in respostas_curtas:
            import random
            eh_ruben_rc = usuario.phone == PHONE_RUBEN
            trat_rc = "mano" if eh_ruben_rc else "querida"
            resps = ["😊", "De nada! 💪", "Boa! 😎", "Sempre às ordens! 🙌", "👍",
                     f"Tamos juntos, {trat_rc}! 🤝", "Conta sempre comigo 💚", "É para isso que cá ando 😎"]
            enviar_mensagem(phone_raw, random.choice(resps)); return

        # ── GASTO (texto/sem keyword) ──
        # ── OBJETIVO / META (antes do eh_gasto para não virar gasto) ──
        m_obj = re.match(r'objetivo\s+(.+?)\s+(\d+)', t)
        if m_obj:
            nome_obj = m_obj.group(1).strip()
            valor_obj = float(m_obj.group(2))
            processar_objetivo_poupanca(phone_raw, usuario, f"quero poupar {valor_obj} para {nome_obj}"); return
        m_meta = re.match(r'meta\s+([a-zà-ú]+)\s+(\d+)', t)
        if m_meta:
            cat_meta = m_meta.group(1).strip()
            valor_meta = float(m_meta.group(2))
            processar_meta_categoria(phone_raw, usuario, f"limite {cat_meta} {valor_meta}"); return
        # ── PAGAMENTOS AGENDADOS / PRESTAÇÕES ────────────────────────
        if any(p in t for p in ['klarna','via verde','viaverde','cofidis','prestaç','prestac']) or \
           (re.search(r'dia\s+\d{1,2}', t) and any(w in t for w in ['sai','débito','debito','paga','mensal','todo mes','todos os meses','luz','agua','água','renda','internet'])):
            registar_pagamento_agendado(phone_raw, usuario, texto); return
        if t.strip() in ['agenda','calendario','calendário','agenda financeira','pagamentos','proximos pagamentos','próximos pagamentos','o que vou pagar']:
            ver_agenda(phone_raw, usuario); return
        # ── BANCOS REAIS (Enable Banking) ────────────────────────────
        if re.search(r'renovar\s+(\w+)', t):
            m_banco = re.search(r'renovar\s+(\w+)', t)
            banco_r = m_banco.group(1) if m_banco else 'revolut'
            enable_ligar_banco(phone_raw, usuario, f"ligar banco {banco_r}"); return
        if any(p in t for p in ['ligar banco','conectar banco','adicionar banco','ligar conta']):
            enable_ligar_banco(phone_raw, usuario, texto); return
        if any(p in t for p in ['saldos reais','saldo real','saldo do banco','saldos do banco','atualizar saldos','sincronizar banco']):
            enviar_saldos_reais(phone_raw, usuario); return
        # ── BANCOS REAIS (Enable Banking) ────────────────────────────
        if any(p in t for p in ['ligar banco','conectar banco','adicionar banco','ligar conta']):
            enable_ligar_banco(phone_raw, usuario, texto); return
        if any(p in t for p in ['saldos reais','saldo real','saldo do banco','saldos do banco','atualizar saldos','sincronizar banco']):
            enviar_saldos_reais(phone_raw, usuario); return
        # ── MODO VIAGEM (agrupar gastos) ─────────────────────────────
        if re.search(r'(?:iniciar|começar|come[çc]ar|nova|ativar|modo)\s+viagem', t) or t.strip() in ['viagem nova','nova viagem']:
            iniciar_viagem(phone_raw, usuario, texto); return
        if any(p in t for p in ['terminar viagem','acabar viagem','fim da viagem','terminei a viagem','voltei da viagem','fechar viagem']):
            terminar_viagem(phone_raw, usuario); return
        if t.strip() in ['viagem atual','resumo viagem','como vai a viagem']:
            v_at = viagem_ativa(usuario.id)
            if v_at:
                g_at = db.session.execute(text("SELECT COALESCE(SUM(valor),0) FROM despesas WHERE usuario_id=:u AND data >= :i"), {'u':usuario.id,'i':v_at[2]}).scalar() or 0
                enviar_mensagem(phone_raw, f"✈️ *{v_at[1]}* em curso\n💰 Já gastaste {g_at:.0f}€\n\n_Diz_ *terminar viagem* _para o total_")
            else:
                enviar_mensagem(phone_raw, "Não tens viagem ativa 🤔")
            return
        # ── HISTÓRICO COMBUSTÍVEL ────────────────────────────────────
        if any(p in t for p in ['consumo carro','consumo do carro','historico gasolina','histórico gasolina','ver abastecimentos','gastos gasolina','quanto gastei em gasolina']):
            ver_historico_combustivel(phone_raw, usuario); return
        # ── CALCULADORA DE VIAGENS ───────────────────────────────────
        _t_viagem = t.replace('ã','a').replace('é','e').replace('í','i').replace('ó','o')
        _gatilho_viagem = any(p in _t_viagem for p in ['vamos a','vamos ao','viagem a','viagem ao','viagem para','quanto custa ir','custo da viagem','ir ate','ir a ','fim de semana a','passar a','fui a'])
        _destino_conhecido = any(c in _t_viagem.split() for c in DISTANCIAS_MOITA.keys())
        if _gatilho_viagem or (_destino_conhecido and len(t.split()) <= 3 and not eh_gasto(texto)):
            calcular_viagem(phone_raw, usuario, texto); return
        # ── MULTI-GASTOS NUMA MENSAGEM ───────────────────────────────
        if eh_gasto(texto):
            multi = detetar_multi_gastos(texto)
            if multi:
                msg_multi = f"🧾 Detetei {len(multi)} gastos:\n\n"
                for g in multi:
                    cat_m, emoji_m, _ = categorizar(g.get('descricao',''))
                    d = Despesa(usuario_id=usuario.id, valor=float(g['valor']), categoria=cat_m,
                        descricao=g.get('descricao','Gasto')[:50].capitalize(),
                        data=agora().replace(tzinfo=None))
                    db.session.add(d)
                    msg_multi += f"{emoji_m} {g.get('descricao','Gasto').capitalize()} — {float(g['valor']):.2f}€\n"
                db.session.commit()
                disp_m, _ = calcular_disponivel(usuario)
                msg_multi += f"\n💚 Disponível: {disp_m:.2f}€"
                enviar_mensagem(phone_raw, msg_multi); return
        # ── LEMBRETES ────────────────────────────────────────────────
        if re.search(r'\blembra(?:-me)?\b', t) and any(p in t for p in ['amanhã','amanha','hoje','segunda','terça','quarta','quinta','sexta','sábado','sabado','domingo']):
            processar_lembrete(phone_raw, usuario, texto); return
        # ── SALDOS / PATRIMÔNIO ──────────────────────────────────────
        if re.search(r'\bsaldo\b', t) and tem_numero(texto):
            if registar_saldo_conta(phone_raw, usuario, texto): return
        if any(p in t for p in ['patrimonio','patrimônio','ver saldos','os meus saldos','quanto tenho no banco']):
            ver_patrimonio(phone_raw, usuario); return
        # ── JÁ PAGUEI AO PARCEIRO (sem valor = splits) ───────────────
        if re.search(r'\bj[áa] paguei\b', t) and not tem_numero(texto):
            if processar_ja_paguei(phone_raw, usuario, texto): return
        # ── RECIBO DE LOJA / EMAIL DE CONFIRMAÇÃO ──────────────────
        m_recibo = (re.search(r'\(([A-Za-záàâãéêíóôõúçÁ][\w\s&.]{2,30})\)\s*[\u2014\-\u2013]\s*(\d+[.,]\d{2})', texto)
                    or re.search(r'\b([A-Za-záàâãéêíóôõúç][\w\s&.]{3,25})\s+[\u2014\-\u2013]\s*(\d+[.,]\d{2})\s*€', texto))
        if m_recibo and not any(p in t for p in ['wishlist','quero','curtia','ando a ver','entidade']):
            nome_loja_r = m_recibo.group(1).strip()
            valor_r = float(m_recibo.group(2).replace(',','.'))
            if 0.5 < valor_r < 5000 and len(nome_loja_r) > 2:
                cat_r, emoji_r, nome_r = categorizar(nome_loja_r)
                db.session.add(Despesa(usuario_id=usuario.id, valor=valor_r,
                    descricao=nome_r, categoria=cat_r, data=agora().replace(tzinfo=None)))
                db.session.commit()
                codigo_r = id_para_codigo(db.session.execute(text("SELECT MAX(id) FROM despesas WHERE usuario_id=:u"),{'u':usuario.id}).scalar() or 0)
                msg_r = f"{emoji_r} *{nome_r}*\n━━━━━━━━━━━━━━\n💸 Valor:  *{valor_r:.2f}€*\n🏷️ Categoria:  {cat_r.capitalize()}\n📅 Data:  {agora().strftime('%a %d/%m/%Y')}\n🆔 Código:  {codigo_r}\n━━━━━━━━━━━━━━"
                enviar_mensagem(phone_raw, msg_r); return
        # ── FATURA COM ENTIDADE/REFERÊNCIA ───────────────────────────
        if 'entidade' in t and 'refer' in t:
            if processar_fatura_referencia(phone_raw, usuario, texto): return
        # "fatura da X" com valor mas SEM entidade MB → é um gasto normal
        if 'fatura' in t and tem_numero(texto) and 'entidade' not in t:
            processar_despesa(phone_raw, usuario, texto); return
        if any(p in t for p in ['paguei a fatura','fatura paga','ja paguei a fatura']):
            est_f, dados_f = get_estado(phone)
            if est_f == 'fatura_pendente':
                limpar_estado(phone)
                v_f = dados_f.get('valor', 0)
                if v_f > 0:
                    d = Despesa(usuario_id=usuario.id, valor=v_f, categoria='casa',
                        descricao=f"Fatura ent.{dados_f.get('entidade','')}", data=agora().replace(tzinfo=None))
                    db.session.add(d); db.session.commit()
                    enviar_mensagem(phone_raw, f"✅ Fatura de {v_f:.2f}€ registada em Casa 🏠"); return
            enviar_mensagem(phone_raw, "Não tenho nenhuma fatura pendente. Cola o texto da fatura primeiro 🧾"); return
        # ── VER TRANSAÇÕES / EXTRATO ─────────────────────────────────
        if any(p in t for p in ['ver transacoes','ver transações','extrato','ultimas transacoes','últimas transações','ver gastos todos','todas as transacoes','todas as transações','historico de gastos','lista de gastos','ver despesas']):
            ver_transacoes(phone_raw, usuario); return
        # ── APAGAR POR CÓDIGO: "apaga G4X" / "excluir G4X" ───────────
        m_cod = re.search(r'(?:apaga|apagar|elimina|eliminar|exclui|excluir|remove|remover)\s+(?:a\s+|o\s+|transa[çc][ãa]o\s+|gasto\s+)?([0-9a-zA-Z]{2,4})\b', texto, re.IGNORECASE)
        if m_cod and not any(p in t for p in ['ultimo','último','isso']):
            cod = m_cod.group(1).upper()
            tx_id = codigo_para_id(cod)
            if tx_id:
                try:
                    r = db.session.execute(text(
                        "DELETE FROM despesas WHERE id=:i AND usuario_id=:u RETURNING descricao, valor"),
                        {'i': tx_id, 'u': usuario.id}).fetchone()
                    db.session.commit()
                    if r:
                        enviar_mensagem(phone_raw, f"🗑️ Apagado: {r[0]} — {r[1]:.2f}€ (código {cod})")
                    else:
                        enviar_mensagem(phone_raw, f"Não encontrei a transação {cod} 🤔")
                    return
                except Exception as e:
                    db.session.rollback(); log.error(f"apagar codigo: {e}")
        # ── APAGAR / CORRIGIR ÚLTIMO GASTO ───────────────────────────
        if any(p in t for p in ['apaga o ultimo','apaga ultimo','apagar ultimo','remove o ultimo','apaga o último','apagar último','enganei-me apaga','apaga isso']):
            apagar_ultimo_gasto(phone_raw, usuario); return
        if any(p in t for p in ['corrige para','altera para','corrige o ultimo','altera o ultimo','muda para','afinal foi','afinal era','afinal foram']) and tem_numero(texto):
            corrigir_ultimo_gasto(phone_raw, usuario, texto); return
        # ── SMS/NOTIFICAÇÃO DO BANCO ─────────────────────────────────
        if eh_sms_banco(texto):
            if processar_sms_banco(phone_raw, usuario, texto): return
        # ── ABASTECIMENTO COM KM (antes do eh_gasto) ────────────────
        if re.search(r'\b(tinha|estava)\b', t) and any(p in t for p in ['meti','gastei','paguei']) and re.search(r'\d+\s*km', t):
            registar_abastecimento(phone_raw, usuario, texto); return
        # ─────────────────────────────────────────────────────────────
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
        try: db.session.rollback()
        except Exception: pass
        log.error(f'processar_texto ERRO: {type(e).__name__}: {e} | texto="{texto[:50]}"', exc_info=True)
        enviar_mensagem(phone_raw, "Ocorreu um erro 😕 Tenta de novo!")

# ─── PROCESSAR DESPESA ───────────────────────────────────────

def id_para_codigo(id_num):
    """Converte ID numérico em código curto tipo 'G4X' (base36)."""
    chars = "0123456789ABCDEFGHJKLMNPQRSTUVWXYZ"  # sem I/O para não confundir
    n = id_num
    codigo = ""
    if n == 0: return "0"
    base = len(chars)
    while n > 0:
        codigo = chars[n % base] + codigo
        n //= base
    return codigo.rjust(3, '0')

def codigo_para_id(codigo):
    """Converte código curto de volta para ID numérico."""
    chars = "0123456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    base = len(chars)
    n = 0
    for c in codigo.upper():
        if c not in chars: return None
        n = n * base + chars.index(c)
    return n


def detetar_gasto_anomalo(usuario, categoria, valor):
    """Deteta se um gasto e' anormalmente alto comparado com o historico
    do utilizador NAQUELA categoria. Devolve uma mensagem ou None.
    So avisa em gastos significativos (>30EUR) e quando ha historico suficiente."""
    if valor < 30:
        return None
    try:
        # Media e maximo dos ultimos 3 meses nesta categoria (excluindo este gasto)
        from datetime import timedelta as _td
        ha_3_meses = (agora() - _td(days=90)).replace(tzinfo=None)
        rows = db.session.execute(text(
            "SELECT valor FROM despesas WHERE usuario_id=:u AND categoria=:c "
            "AND data >= :d AND descricao NOT LIKE '[conjunta]%' "
            "ORDER BY valor DESC"),
            {'u': usuario.id, 'c': categoria, 'd': ha_3_meses}).fetchall()
        valores = [float(r[0]) for r in rows if r[0]]
        if len(valores) < 4:
            return None  # historico insuficiente para julgar
        # Excluir o proprio gasto que acabou de entrar (o maior, provavelmente)
        media = sum(valores) / len(valores)
        if media <= 0:
            return None
        racio = valor / media
        # So avisa se for claramente fora do padrao (2.5x a media)
        if racio >= 2.5:
            return (f"\n👀 Costumas gastar ~{media:.0f}EUR em {categoria} de cada vez — "
                    f"este foi {racio:.0f}x mais. Tudo certo?")
    except Exception as e:
        log.error(f"detetar_gasto_anomalo: {e}")
    return None

def processar_despesa(phone_raw, usuario, texto):
    # Data relativa: "ontem gastei...", "sábado atestei..."
    _d_rel = extrair_data_relativa(texto)
    _data_gasto = (datetime.combine(_d_rel, datetime.min.time().replace(hour=12)) if _d_rel
                   else agora().replace(tzinfo=None))
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
    tags_encontradas = re.findall(r'#(\w+)', texto)
    tags_str = ' '.join(f'#{t.lower()}' for t in tags_encontradas) if tags_encontradas else None
    despesa = Despesa(usuario_id=usuario.id, valor=valor, categoria=categoria,
                      descricao=descricao, data=_data_gasto)
    db.session.add(despesa); db.session.commit()
    if tags_str:
        try:
            db.session.execute(text("UPDATE despesas SET tags=:tg WHERE id=:i"),
                                {'tg': tags_str, 'i': despesa.id})
            db.session.commit()
        except Exception as e:
            log.error(f"guardar tags: {e}"); db.session.rollback()

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
        # Total depositado na conjunta este mês (ambos os utilizadores)
        parceiro_phone_c = get_parceiro_phone(usuario.phone)
        parceiro_c = Usuario.query.filter_by(phone=parceiro_phone_c).first() if parceiro_phone_c else None
        ids_c = [usuario.id] + ([parceiro_c.id] if parceiro_c else [])
        total_dep = 0
        for uid_c in ids_c:
            total_dep += db.session.execute(text(
                "SELECT COALESCE(SUM(valor),0) FROM conjunta_depositos "
                "WHERE usuario_id=:u AND EXTRACT(month FROM data)=:m AND EXTRACT(year FROM data)=:y"
            ), {'u': uid_c, 'm': mes, 'y': ano}).scalar() or 0
        # Se não há depósitos usa 0 como referência
        resta_conj = total_dep - gc
        pessoa_txt = f" (com {pessoa})" if pessoa else ""
        msg = f"{emoji} {nome_loja} — {valor:.2f}€ 💑{pessoa_txt}\n"
        if total_dep > 0:
            msg += f"📊 Conjunta: {gc:.2f}€ gastos de {total_dep:.0f}€\n"
            if resta_conj >= 0:
                msg += f"💚 Resta: {resta_conj:.2f}€"
            else:
                msg += f"⚠️ Passaram {abs(resta_conj):.2f}€ do que meteram!"
        else:
            msg += f"📊 Gastos conjunta este mês: {gc:.2f}€\n"
            msg += f"💡 Ainda não meteram dinheiro este mês"
        enviar_mensagem(phone_raw, msg)

        # Notifica o parceiro
        meu_nome = NOMES_CASAL.get(usuario.phone, 'O parceiro')
        # Round up → coisasnossas
        import math
        arredondado = math.ceil(valor)
        diferenca = round(arredondado - valor, 2)
        if diferenca > 0:
            try:
                r_obj = db.session.execute(text(
                    "SELECT id, valor_atual FROM objetivos_poupanca WHERE usuario_id=:u AND LOWER(descricao)='coisasnossas' AND concluido=FALSE"
                ), {'u': usuario.id}).fetchone()
                if r_obj:
                    db.session.execute(text(
                        "UPDATE objetivos_poupanca SET valor_atual=valor_atual+:d WHERE id=:id"
                    ), {'d': diferenca, 'id': r_obj[0]})
                else:
                    db.session.execute(text(
                        "INSERT INTO objetivos_poupanca (usuario_id, descricao, valor_objetivo, valor_atual) VALUES (:u, 'coisasnossas', 9999, :d)"
                    ), {'u': usuario.id, 'd': diferenca})
                db.session.commit()
                msg += f"\n🪙 +{diferenca:.2f}€ → coisasnossas"
            except Exception as e:
                log.error(f"roundup: {e}"); db.session.rollback()
        notif_msg = (f"💑 *{meu_nome}* gastou {valor:.2f}€ na conjunta\n"
                     f"📍 {nome_loja}\n"
                     f"💚 Resta: {max(resta_conj,0):.2f}€ de {total_dep:.0f}€")
        notificar_parceiro(usuario.phone, notif_msg)
        return

    disp, p = calcular_disponivel(usuario)
    gastar = p['gastar']
    pct_usado = ((gastar-disp)/gastar*100) if gastar>0 else 0

    # Código curto da transação (a partir do ID)
    codigo_tx = id_para_codigo(despesa.id)

    # Data formatada
    mes_i = agora().month; ano_i = agora().year
    _dias_n = ['Seg','Ter','Qua','Qui','Sex','Sáb','Dom']
    if _d_rel:
        data_txt = f"{_dias_n[_d_rel.weekday()]} {_d_rel.strftime('%d/%m/%Y')}"
    else:
        hoje_d = agora()
        data_txt = f"{_dias_n[hoje_d.weekday()]} {hoje_d.strftime('%d/%m/%Y')}"

    # Insights
    mes_ant_i = mes_i - 1 if mes_i > 1 else 12
    ano_ant_i = ano_i if mes_i > 1 else ano_i - 1
    total_cat_ant = db.session.execute(text(
        "SELECT COALESCE(SUM(valor),0) FROM despesas WHERE usuario_id=:u AND categoria=:c "
        "AND EXTRACT(month FROM data)=:m AND EXTRACT(year FROM data)=:y"),
        {'u':usuario.id,'c':categoria,'m':mes_ant_i,'y':ano_ant_i}).scalar() or 0
    total_mes_geral = db.session.execute(text(
        "SELECT COALESCE(SUM(valor),0) FROM despesas WHERE usuario_id=:u "
        "AND EXTRACT(month FROM data)=:m AND EXTRACT(year FROM data)=:y "
        "AND descricao NOT LIKE '[conjunta]%%'"),
        {'u':usuario.id,'m':mes_i,'y':ano_i}).scalar() or 1
    pct_cat = round(total_cat / total_mes_geral * 100) if total_mes_geral > 0 else 0

    # ── CARTÃO VISUAL estilo GranaZen ──
    pessoa_txt = f" · com {pessoa}" if pessoa else ""
    tipo_conta = "conjunta 💑" if na_conjunta else "pessoal"
    msg = f"{emoji} *{nome_loja}*{pessoa_txt}\n"
    msg += f"───────────────────────\n"
    msg += f"💸 Valor:  *{valor:.2f}€*\n"
    msg += f"🔄 Tipo:  🟥 Despesa\n"
    msg += f"🏷️ Categoria:  {categoria.capitalize()}\n"
    if tags_str:
        msg += f"🔖 Tags:  {tags_str}\n"
    msg += f"🏦 Conta:  {tipo_conta}\n"
    msg += f"🗓️ Data:  {data_txt}\n"
    msg += f"🆔 Código:  `{codigo_tx}`\n"
    msg += f"───────────────────────\n"
    msg += f"📊 {categoria.capitalize()} este mês: {total_cat:.0f}€ ({pct_cat}% dos gastos)\n"
    msg += f"❌ Para anular: *anular {codigo_tx}*\n"
    msg += f"📊 Ver em zedasfinancas.netlify.app/#movimentos"
    try:
        _lembrar_nif = db.session.execute(text(
            "SELECT lembrar_nif FROM usuarios WHERE id=:u"), {'u': usuario.id}).scalar()
        if _lembrar_nif and valor >= 5 and categoria not in ('combustivel','subscricoes'):
            msg += f"\n\n💳 _Deste o NIF?_"
    except Exception:
        pass
    # Aviso se passou a meta definida para esta categoria
    try:
        meta_cat = db.session.execute(text(
            "SELECT limite FROM metas_categoria WHERE usuario_id=:u AND categoria=:c AND mes=:m AND ano=:a"),
            {'u':usuario.id,'c':categoria,'m':mes_i,'a':ano_i}).scalar()
        if meta_cat and total_cat > meta_cat:
            excesso_m = total_cat - meta_cat
            msg += f"\n🚨 *Passaste o orçamento de {categoria}!* ({meta_cat:.0f}€) — {excesso_m:.0f}€ acima"
        elif meta_cat and total_cat > meta_cat * 0.8:
            resta_m = meta_cat - total_cat
            msg += f"\n⚠️ Cuidado: {total_cat:.0f}€ de {meta_cat:.0f}€ (resta {resta_m:.0f}€)"
    except Exception:
        pass
    if total_cat_ant > 0:
        diff_i = (total_cat - total_cat_ant) / total_cat_ant * 100
        sinal_i = "+" if diff_i > 0 else ""
        msg += f"\n📈 {sinal_i}{diff_i:.0f}% vs mês passado"
        if diff_i > 50:
            poupanca_anual = total_cat_ant * 0.2 * 12
            msg += f"\n💡 Se reduzires 20% poupas {poupanca_anual:.0f}€/ano"

    # Comentário de padrão
    inicio_semana = agora().replace(tzinfo=None) - timedelta(days=agora().weekday())
    vezes_semana = db.session.query(db.func.count(Despesa.id)).filter(
        Despesa.usuario_id==usuario.id, Despesa.categoria==categoria,
        Despesa.data>=inicio_semana).scalar() or 0

    _viagem_em_curso = viagem_ativa(usuario.id)
    if _viagem_em_curso:
        msg += f"\n✈️ _{_viagem_em_curso[1]} em curso — sem alarmes, aproveita!_"
    elif categoria=='fastfood' and vezes_semana>=3:
        msg += f"\n😏 Já é a {vezes_semana}.ª vez de fast food esta semana!"
    elif categoria=='gota' and total_cat>30:
        msg += f"\n🧃 {total_cat:.0f}€ em bebidas este mês... abranda!"
    elif categoria=='combustivel' and total_cat>BASE_COMBUSTIVEL:
        excesso_g = total_cat - BASE_COMBUSTIVEL
        msg += f"\n⚠️ Já gastaste {total_cat:.0f}€ em gasolina (orçamento {BASE_COMBUSTIVEL}€) — *{excesso_g:.0f}€ acima!*"
    elif categoria=='pessoal':
        orcamento_unhas = get_fixos_usuario(usuario.phone, agora().month, usuario.id).get('unhas', 0)
        if orcamento_unhas > 0 and total_cat > orcamento_unhas:
            excesso_u = total_cat - orcamento_unhas
            msg += f"\n⚠️ Foi {excesso_u:.0f}€ a mais que o previsto nos fixos ({orcamento_unhas:.0f}€) — só esse valor saiu do para gastar!"
        elif orcamento_unhas > 0 and total_cat <= orcamento_unhas:
            msg += f"\n✅ Dentro do orçamento dos fixos ({orcamento_unhas:.0f}€) — não saiu nada do para gastar!"
    elif agora().weekday() in [4,5] and agora().hour>=20 and categoria in ['restaurante','fastfood','cafe'] and not _d_rel and 'almoc' not in texto.lower() and 'almoç' not in texto.lower():
        msg += "\n🍻 Noite de fim de semana! Aproveita 😎"
    elif total_cat_ant>0 and total_cat>total_cat_ant*1.3:
        msg += f"\n⚠️ Já gastaste mais em {categoria} que o mês passado todo!"
    elif total_cat_ant>0 and total_cat<total_cat_ant*0.6:
        msg += f"\n✅ Bem menos em {categoria} que o mês passado!"

    # Deteção de gasto anómalo (único gasto muito acima do padrão pessoal)
    if not _viagem_em_curso:
        aviso_anomalo = detetar_gasto_anomalo(usuario, categoria, valor)
        if aviso_anomalo:
            msg += aviso_anomalo

    # Aviso orçamento (suprimido durante viagem — é normal gastar mais)
    if not _viagem_em_curso:
        if pct_usado >= 100:
            msg += f"\n⚠️ Passaste o orçamento em {abs(disp):.0f}€"
        elif pct_usado >= 80:
            msg += f"\n🔔 Usaste {pct_usado:.0f}% do orçamento"

    enviar_mensagem(phone_raw, msg)
    verificar_badges(usuario, phone_raw)

    # Verificar se completou TODOS os fixos do mês
    try:
        _, p_check = calcular_disponivel(usuario)
        if verificar_fixos_completos(usuario, p_check):
            enviar_mensagem(phone_raw, "🎉 *Pagaste todos os fixos deste mês!* Tás em dia com tudo 💪")
    except Exception as e:
        log.error(f"check fixos completos: {e}")

# ─── RECEITA / PLANO ─────────────────────────────────────────

def eh_dinheiro_extra(t):
    """Deteta dinheiro avulso (prendas, avós, amigos) que NÃO é o salário."""
    fontes_extra = ['avo','avó','avô','avos','avós','padrinho','madrinha','tio','tia',
                     'presente','prenda','rifa','sorteio','reembolso de','devolveram',
                     'amigo','amiga','colega','emprestimo','empréstimo','vendi','venda']
    fontes_salario = ['ordenado','salario','salário','vencimento','duodecimo','duodécimo',
                       'subsidio','subsídio','entidade','empresa','trabalho','patrao','patrão']
    tem_extra = any(f in t for f in fontes_extra)
    tem_salario = any(f in t for f in fontes_salario)
    return tem_extra and not tem_salario

def processar_dinheiro_extra(phone_raw, usuario, texto):
    """Regista dinheiro avulso (não-salário) — soma ao disponível sem tocar no plano mensal."""
    valor = extrair_valor(texto)
    if valor == 0:
        enviar_mensagem(phone_raw, "Quanto recebeste? 💰"); return
    try:
        db.session.add(Receita(usuario_id=usuario.id, valor=valor, descricao='Extra',
                                data=agora().replace(tzinfo=None)))
        db.session.commit()
        enviar_mensagem(phone_raw,
            f"🎁 +{valor:.0f}€ registado!\n"
            f"Já está disponível para gastar — não afeta o teu plano do salário 😊")
    except Exception as e:
        log.error(f"processar_dinheiro_extra: {e}"); db.session.rollback()
        enviar_mensagem(phone_raw, "Erro 😕")

def processar_receita(phone_raw, usuario, texto):
    valor = extrair_valor(texto)
    limpar_salarios_pendentes(usuario.id)  # evita duplo registo pelo job das 9h
    if valor == 0:
        enviar_mensagem(phone_raw, "Quanto recebeste? 💰"); return
    usuario.salario_liquido = valor
    # Marcar que recebeu salário ESTE mês (para o disponível contar)
    try:
        mes_atual_pr = f"{agora().year}-{agora().month:02d}"
        usuario.ultimo_salario_mes = mes_atual_pr
        db.session.execute(text("UPDATE usuarios SET ultimo_salario_mes=:m WHERE id=:u"),
                            {'m': mes_atual_pr, 'u': usuario.id})
    except Exception as e:
        log.error(f"ultimo_salario_mes write: {e}")
    db.session.add(Receita(usuario_id=usuario.id, valor=valor, descricao='Salario', data=agora().replace(tzinfo=None)))
    db.session.commit()
    enviar_plano_salario(phone_raw, usuario, valor)
    try:
        verificar_aniversarios_proximo_mes(phone_raw, usuario, valor)
    except Exception as e:
        log.error(f"aniv_proximo: {e}")


def sugerir_meta_inteligente(usuario):
    """Analisa gastos e sugere uma meta de categoria se gastar muito numa."""
    try:
        hoje = agora()
        mes_ant = hoje.month - 1 if hoje.month > 1 else 12
        ano_ant = hoje.year if hoje.month > 1 else hoje.year - 1
        # Categoria onde gastou mais (excluindo essenciais)
        rows = db.session.execute(text(
            "SELECT categoria, SUM(valor) as t FROM despesas WHERE usuario_id=:u "
            "AND EXTRACT(month FROM data)=:m AND EXTRACT(year FROM data)=:y "
            "AND categoria IN ('fastfood','restaurante','cafe','roupa','lazer','gota') "
            "GROUP BY categoria ORDER BY t DESC LIMIT 1"),
            {'u':usuario.id,'m':mes_ant,'y':ano_ant}).fetchone()
        if not rows or rows[1] < 60:
            return None  # só sugere se gastou 60€+ numa categoria supérflua
        cat, total = rows[0], rows[1]
        # Já tem meta nesta categoria?
        ja_tem = db.session.execute(text(
            "SELECT 1 FROM metas_categoria WHERE usuario_id=:u AND categoria=:c AND mes=:m AND ano=:y"),
            {'u':usuario.id,'c':cat,'m':hoje.month,'y':hoje.year}).fetchone()
        if ja_tem:
            return None
        meta_sug = round(total * 0.7 / 5) * 5  # 30% menos, arredondado a 5
        nomes_cat = {'fastfood':'fast food','restaurante':'restaurantes','cafe':'cafés',
                     'roupa':'roupa','lazer':'lazer','gota':'bebidas'}
        nome_c = nomes_cat.get(cat, cat)
        emoji_c = EMOJI_CAT.get(cat, '💳')
        poupado_ano = round((total - meta_sug) * 12)
        return (f"💡 *Sugestão do Zé*\n"
                f"Gastaste {total:.0f}€ em {emoji_c} {nome_c} no mês passado.\n"
                f"Que tal uma meta de *{meta_sug:.0f}€* este mês?\n"
                f"Poupavas ~{poupado_ano}€/ano! 📈\n\n"
                f"_Diz_ *meta {cat} {meta_sug:.0f}* _para ativar_")
    except Exception as e:
        log.error(f"sugerir_meta: {e}")
        return None

def enviar_plano_salario(phone_raw, usuario, salario):
    modo = get_modo(usuario.id)
    futuras = DespesaFutura.query.filter(DespesaFutura.usuario_id==usuario.id, DespesaFutura.pago==False).all()
    total_fut = sum(d.valor_reserva_mensal for d in futuras)
    p = calcular_plano(salario, modo, total_fut, phone=usuario.phone, usuario_id=usuario.id)
    m = MODOS_POUPANCA[modo]

    # ── Quanto sobrou do orçamento do mês ANTERIOR ──
    sobra_anterior = 0
    try:
        hoje_s = agora()
        mes_ant_s = hoje_s.month - 1 if hoje_s.month > 1 else 12
        ano_ant_s = hoje_s.year if hoje_s.month > 1 else hoje_s.year - 1
        rec_ant = db.session.execute(text(
            "SELECT COALESCE(SUM(valor),0) FROM receitas WHERE usuario_id=:u "
            "AND EXTRACT(month FROM data)=:m AND EXTRACT(year FROM data)=:y"),
            {'u':usuario.id,'m':mes_ant_s,'y':ano_ant_s}).scalar() or 0
        gas_ant = db.session.execute(text(
            "SELECT COALESCE(SUM(valor),0) FROM despesas WHERE usuario_id=:u "
            "AND EXTRACT(month FROM data)=:m AND EXTRACT(year FROM data)=:y "
            "AND descricao NOT LIKE '[conjunta]%%'"),
            {'u':usuario.id,'m':mes_ant_s,'y':ano_ant_s}).scalar() or 0
        if rec_ant > 0:
            p_ant = calcular_plano(rec_ant, modo, 0, phone=usuario.phone, usuario_id=usuario.id)
            # O que estava destinado a gastar menos o que gastou
            sobra_anterior = round(p_ant.get('gastar', 0) - gas_ant, 0)
    except Exception as e:
        log.error(f"sobra anterior: {e}")

    msg  = f"💰 *Recebeste {salario:.2f}€!* {m['emoji']}\n\n"
    msg += f"━━━━━━━━━━━━\n"
    msg += f"*📋 O teu plano*\n\n"
    msg += f"🏠 *Fixos:* {p['total_fixos']:.0f}€\n"
    fixos_display = []
    if p.get('mae'):         fixos_display.append(f"   👩 Dinheiro mãe — {p['mae']:.0f}€")
    if p.get('carro'):       fixos_display.append(f"   🚗 Carro — {p['carro']:.0f}€")
    if p.get('credito1'):    fixos_display.append(f"   💳 Crédito BPI — {p['credito1']:.0f}€")
    if p.get('credito2'):    fixos_display.append(f"   💳 Crédito Revolut — {p['credito2']:.0f}€")
    if p.get('ordem'):       fixos_display.append(f"   💼 Ordem — {p['ordem']:.0f}€")
    if p.get('unhas'):       fixos_display.append(f"   💅 Unhas — {p['unhas']:.0f}€")
    if p.get('conjunta'):    fixos_display.append(f"   💑 Conta conjunta — {p['conjunta']:.0f}€")
    if p.get('combustivel'): fixos_display.append(f"   ⛽ Gasolina — {p['combustivel']:.0f}€")
    if p.get('divida_luana'):fixos_display.append(f"   💸 Dívida Luana — {p['divida_luana']:.0f}€")
    if fixos_display:
        msg += "\n".join(fixos_display) + "\n"
    # Pagamentos agendados / prestações em curso
    try:
        pags_ag = db.session.execute(text(
            "SELECT nome, valor, dia_mes, prestacoes_total, prestacoes_pagas FROM pagamentos_agendados "
            "WHERE usuario_id=:u AND ativo=TRUE ORDER BY dia_mes"), {'u': usuario.id}).fetchall()
        if pags_ag:
            msg += "\n💳 *Pagamentos este mês:*\n"
            for nome_pa, val_pa, dia_pa, pt_pa, pp_pa in pags_ag:
                prest_pa = f" ({pp_pa+1}/{pt_pa})" if pt_pa < 999 else ""
                msg += f"   {nome_pa}{prest_pa} — {val_pa:.0f}€ (dia {dia_pa})\n"
    except Exception:
        pass
    if total_fut > 0:
        msg += f"\n   📅 Despesas mes: {total_fut:.0f}€"
        for d in futuras: msg += f"\n     {d.descricao}: {d.valor_reserva_mensal:.0f}€"
    msg += f"\n🛡️ Fundo: {p['fundo']:.2f}€ (Revolut!)\n"
    msg += f"💳 Para gastar: {p['gastar']:.0f}€\n"
    msg += f"💎 Poupanca: {p['poupanca']:.0f}€"
    if p['subsidio']:
        mes_sub = agora().month
        if mes_sub == 6:
            msg += "\n\n🌴 *Subsídio de férias!* 😎\n💡 Aproveita para reservar uns 100€ para roupa de verão ou uma escapadinha"
        elif mes_sub in [11, 12]:
            msg += "\n\n🎄 *Subsídio de Natal!* 🎁\n💡 Guarda algo para as prendas e a ceia"
        else:
            msg += "\n\n🌴 *Mês de subsídio!* 😉"
        # Verifica se tem wishlist
        try:
            rows = db.session.execute(text(
                "SELECT descricao, preco FROM wishlist WHERE usuario_id=:id AND comprado=FALSE ORDER BY criado_em DESC LIMIT 3"),
                {'id': usuario.id}).fetchall()
            if rows:
                _eh_ruben = usuario.phone == PHONE_RUBEN
                msg += ("\n\n🛍️ Mes de subsidio, mano! Tens na wishlist:\n" if _eh_ruben
                        else "\n\n🛍️ Mes de subsidio = mes de mimar! Tens na wishlist:\n")
                for r in rows:
                    preco_txt = f" — {r[1]:.2f}€" if r[1] else ""
                    msg += f"• {r[0]}{preco_txt}\n"
                msg += "\nTu mereces! 💪" if _eh_ruben else "\nTu mereces! 💕"
            else:
                msg += ("\n\nAproveita para comprar algo para ti, mereces! 🛍️💪" if usuario.phone == PHONE_RUBEN
                        else "\n\nAproveita para comprar umas roupas para ti, tu mereces! 🛍️💕")
        except Exception:
            msg += ("\n\nAproveita para comprar algo para ti! 🛍️💪" if usuario.phone == PHONE_RUBEN
                    else "\n\nAproveita para comprar umas roupas para ti, tu mereces! 🛍️💕")
    if agora().month == 11 and usuario.phone == PHONE_LUANA:
        msg += "\n\n🎂 Este mês é o teu aniversário!! 100€ só para ti! 🎁"
    # Lembrete dos objetivos ativos (não te esqueças de poupar para X)
    try:
        objs_ativos = db.session.execute(text(
            "SELECT descricao, valor_objetivo, valor_atual FROM objetivos_poupanca "
            "WHERE usuario_id=:u AND concluido=FALSE ORDER BY id DESC LIMIT 3"),
            {'u': usuario.id}).fetchall()
        if objs_ativos:
            msg += "\n\n🎯 *Não te esqueças dos objetivos:*\n"
            for desc_o, val_o, at_o in objs_ativos:
                emoji_o = emoji_objetivo(desc_o)
                falta_o = (val_o or 0) - (at_o or 0)
                msg += f"{emoji_o} {desc_o} — falta {falta_o:.0f}€\n"
    except Exception as e:
        log.error(f"lembrete objetivos: {e}")
    # ── Mostrar sobra do mês anterior + sugestão ──
    if sobra_anterior >= 10:
        msg += f"\n\n💰 *Sobraram {sobra_anterior:.0f}€ do mês passado!*\n"
        try:
            objs_s = db.session.execute(text(
                "SELECT descricao FROM objetivos_poupanca WHERE usuario_id=:u AND concluido=FALSE ORDER BY id DESC LIMIT 1"),
                {'u': usuario.id}).fetchone()
            if objs_s:
                em_s = emoji_objetivo(objs_s[0])
                msg += f"Mete na reserva 🛡️ ou no {em_s} {objs_s[0]}:\n"
                msg += f"_Diz_ *reserva {sobra_anterior:.0f}* _ou_ *guardei {sobra_anterior:.0f} para {objs_s[0].lower()}*"
            else:
                msg += f"Aproveita para reforçar a reserva: _diz_ *reserva {sobra_anterior:.0f}* 🛡️"
        except Exception:
            msg += f"Aproveita para reforçar a reserva: _diz_ *reserva {sobra_anterior:.0f}* 🛡️"
    msg += "\n━━━━━━━━━━━━\n💡 *onde vai o dinheiro* · 📊 zedasfinancas.netlify.app/#plano"
    enviar_mensagem(phone_raw, msg)
    # Sugestão de meta inteligente (mensagem separada, só às vezes)
    try:
        sugestao = sugerir_meta_inteligente(usuario)
        if sugestao:
            enviar_mensagem(phone_raw, sugestao)
    except Exception:
        pass

    reserva_atual = get_reserva(usuario.id)
    if reserva_atual > 0:
        enviar_mensagem(phone_raw, f"🛡️ Reserva de emergência: {reserva_atual:.2f}€ 💪")

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
    p = calcular_plano(usuario.salario_liquido, modo, total_fut, phone=usuario.phone)
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

def enviar_orcamento_hoje(phone_raw, usuario):
    """Responde a 'quanto posso gastar hoje' com um numero simples e claro."""
    if not usuario.salario_liquido:
        enviar_mensagem(phone_raw, "Ainda nao sei o teu salario 🤔 Diz 'recebi X' primeiro!")
        return
    try:
        disp, p = calcular_disponivel(usuario)
        dias = dias_para_salario(usuario)
        nome = NOMES_CASAL.get(usuario.phone, '')
        if disp < 0:
            enviar_mensagem(phone_raw,
                f"😬 {nome}, estas *{abs(disp):.0f}EUR acima* do orcamento este mes.\n"
                f"O ideal hoje e gastar *0EUR* e segurar ate ao salario ({dias} dias) 🤏")
            return
        por_dia = disp / dias if dias > 0 else disp
        # Quanto ja gastou hoje
        from datetime import datetime as _dt
        hoje_inicio = agora().replace(hour=0, minute=0, second=0, tzinfo=None)
        gasto_hoje = db.session.query(db.func.sum(Despesa.valor)).filter(
            Despesa.usuario_id==usuario.id, Despesa.data >= hoje_inicio,
            ~Despesa.descricao.like('[conjunta]%'),
            ~Despesa.descricao.like('[reserva]%')).scalar() or 0
        resta_hoje = por_dia - gasto_hoje

        msg = f"📅 *Orcamento de hoje, {nome}:*\n\n"
        msg += f"💰 Podes gastar ~*{por_dia:.0f}EUR* por dia\n"
        if gasto_hoje > 0:
            msg += f"🛒 Ja gastaste *{gasto_hoje:.0f}EUR* hoje\n"
            if resta_hoje > 0:
                msg += f"✅ Ainda tens *{resta_hoje:.0f}EUR* para hoje\n"
            else:
                msg += f"⚠️ Ja passaste {abs(resta_hoje):.0f}EUR do dia de hoje\n"
        msg += f"\n_{disp:.0f}EUR no total para {dias} dias ate ao salario_"
        enviar_mensagem(phone_raw, msg)
    except Exception as e:
        log.error(f"enviar_orcamento_hoje: {e}")
        enviar_mensagem(phone_raw, "Tive um problema a calcular 😕")

def enviar_quanto_tenho(phone_raw, usuario, foco=None):
    """Responde quanto tens — usa saldo real Revolut quando disponível."""
    disp, p = calcular_disponivel(usuario)
    reserva = get_reserva(usuario.id)
    mes=agora().month; ano=agora().year
    dias = dias_para_salario(usuario)
    # Tentar saldo real do Revolut via API
    saldo_rev_real = None
    saldo_conj_real = None
    try:
        import requests as _r
        h = _enable_headers()
        if h:
            # Revolut pessoal
            acc_rev = db.session.execute(text(
                "SELECT account_id FROM bancos_ligados WHERE usuario_id=:u AND banco='revolut_pessoal' AND ativo=TRUE LIMIT 1"),
                {'u': usuario.id}).scalar()
            if acc_rev:
                r = _r.get(f"{ENABLE_BASE}/accounts/{acc_rev}/balances", headers=h, timeout=8)
                if r.status_code == 200:
                    bals = r.json().get('balances', [])
                    if bals:
                        saldo_rev_real = float(bals[0].get('balance_amount',{}).get('amount', 0))
            # Conjunta
            acc_conj = db.session.execute(text(
                "SELECT account_id FROM bancos_ligados WHERE usuario_id=:u AND banco='revolut_conjunta' AND ativo=TRUE LIMIT 1"),
                {'u': usuario.id}).scalar()
            if acc_conj:
                r2 = _r.get(f"{ENABLE_BASE}/accounts/{acc_conj}/balances", headers=h, timeout=8)
                if r2.status_code == 200:
                    bals2 = r2.json().get('balances', [])
                    if bals2:
                        saldo_conj_real = float(bals2[0].get('balance_amount',{}).get('amount', 0))
    except Exception as e:
        log.error(f"quanto_tenho API: {e}")

    # Conjunta
    gc = db.session.query(db.func.sum(Despesa.valor)).filter(
        Despesa.usuario_id==usuario.id,
        db.extract('month',Despesa.data)==mes, db.extract('year',Despesa.data)==ano,
        Despesa.descricao.like('[conjunta]%')).scalar() or 0
    parceiro_phone_q = get_parceiro_phone(usuario.phone)
    parceiro_q = Usuario.query.filter_by(phone=parceiro_phone_q).first() if parceiro_phone_q else None
    ids_q = [usuario.id] + ([parceiro_q.id] if parceiro_q else [])
    total_dep_q = sum(
        db.session.execute(text("SELECT COALESCE(SUM(valor),0) FROM conjunta_depositos WHERE usuario_id=:u AND EXTRACT(month FROM data)=:m AND EXTRACT(year FROM data)=:y"),
        {'u': uid_q, 'm': mes, 'y': ano}).scalar() or 0 for uid_q in ids_q)
    resta_conj = max(total_dep_q - gc, 0)

    # Foco específico: "quanto tenho na conjunta"
    if foco == 'conjunta':
        msg = f"💑 *Conta conjunta*\n"
        msg += f"━━━━━━━━━━━━━━\n"
        msg += f"💶 Depositado:  {total_dep_q:.0f}€\n"
        msg += f"🛒 Gasto:  {gc:.0f}€\n"
        msg += f"💚 Disponível:  *{resta_conj:.0f}€*"
        enviar_mensagem(phone_raw, msg); return

    if foco == 'reserva':
        msg = f"🛡️ *Fundo de emergência:* {reserva:.0f}€"
        enviar_mensagem(phone_raw, msg); return

    if foco == 'poupanca':
        msg = f"💎 *Poupança prevista este mês:* {p['poupanca']:.0f}€"
        enviar_mensagem(phone_raw, msg); return

    # Resposta principal
    # Usar saldo real do Revolut se disponível, senão o calculado
    # Formato completo com todas as contas
    import datetime as _dt
    # Usar o DISPONÍVEL CALCULADO para "para gastar" — nunca substituir pelo saldo real
    # (o saldo real é mostrado à parte, são dois números com significados diferentes)
    saldo_variavel = disp
    is_ruben = usuario.phone == PHONE_RUBEN

    msg = ""

    # Ruben: saldo real Revolut + calculado
    if is_ruben:
        if saldo_rev_real is not None:
            msg += f"💜 Revolut: *{saldo_rev_real:.2f}€* _(saldo real)_\n"
        if saldo_variavel >= 0:
            cor = "😊" if saldo_variavel > 200 else ("😬" if saldo_variavel < 50 else "👍")
            msg += f"💳 Para gastar: *{saldo_variavel:.0f}€* _(com o que me disseste)_ {cor}\n"
        else:
            msg += f"⚠️ *Estás {abs(saldo_variavel):.0f}€ no vermelho!*\n"
    else:
        # Luana: só calculado
        if saldo_variavel < 0:
            msg += f"⚠️ *Estás {abs(saldo_variavel):.0f}€ no vermelho!*\n"
        else:
            cor = "😊" if saldo_variavel > 200 else ("😬" if saldo_variavel < 50 else "👍")
            msg += f"💳 Para gastar: *{saldo_variavel:.0f}€* {cor}\n"

    # Conjunta — saldo real Revolut (mostra sempre, mesmo a 0€, para nunca desaparecer)
    if saldo_conj_real is not None:
        msg += f"💑 Conjunta: *{saldo_conj_real:.2f}€*\n"
    else:
        msg += f"💑 Conjunta: *{resta_conj:.0f}€*\n"

    # Reserva
    if reserva > 0:
        msg += f"🛡️ Reserva: *{reserva:.0f}€*\n"

    # Poupança
    if p.get('poupanca', 0) > 0:
        msg += f"💎 Poupança: *{p['poupanca']:.0f}€*\n"

    # Dias até salário — no fim
    if dias > 0 and saldo_variavel > 0:
        import datetime as _dt
        dia_pag = agora().date() + _dt.timedelta(days=dias)
        nome_dia = ['Segunda','Terça','Quarta','Quinta','Sexta','Sábado','Domingo'][dia_pag.weekday()]
        msg += f"\n📅 {dias} dias · {nome_dia} {dia_pag.day} · ~{saldo_variavel/dias:.0f}€/dia\n"

    msg += f"\n📊 zedasfinancas.netlify.app/#saldos"
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
    # Verificar se é o depósito mensal fixo (perto do valor fixo configurado)
    fixo_conj = 50  # valor fixo da conjunta
    is_fixo = abs(valor - fixo_conj) <= 5
    meu_nome_curto = meu_nome.replace('O ','').replace('A ','')
    outro_nome = 'Luana' if usuario.phone == PHONE_RUBEN else 'Ruben'
    
    # Notificar parceiro
    if is_fixo:
        notificar_parceiro(usuario.phone,
            f"💑 *{meu_nome_curto} já meteu os {valor:.0f}€ na conjunta!*\n"
            f"Já meteste os teus? 😊")
    else:
        notificar_parceiro(usuario.phone,
            f"💑 {meu_nome_curto} adicionou {valor:.0f}€ à conjunta\n"
            f"📌 {desc}")
    
    # Confirmar ao próprio
    enviar_mensagem(phone_raw,
        f"💑 *+{valor:.0f}€ na conjunta!* ✅\n"
        f"{'📌 '+desc+chr(10) if not is_fixo else ''}"
        f"A avisar o {outro_nome}… 🔔")

    # Verificar se completou TODOS os fixos do mês
    try:
        _, p_check2 = calcular_disponivel(usuario)
        if verificar_fixos_completos(usuario, p_check2):
            enviar_mensagem(phone_raw, "🎉 *Pagaste todos os fixos deste mês!* Tás em dia com tudo 💪")
    except Exception as e:
        log.error(f"check fixos completos conjunta: {e}")


def nomes_mes_curto(mes):
    nomes = ['Jan','Fev','Mar','Abr','Mai','Jun','Jul','Ago','Set','Out','Nov','Dez']
    return nomes[mes-1] if 1 <= mes <= 12 else '?'

def enviar_conjunta(phone_raw, usuario):
    mes = agora().month; ano = agora().year
    parceiro_phone = get_parceiro_phone(usuario.phone)
    parceiro = Usuario.query.filter_by(phone=parceiro_phone).first() if parceiro_phone else None
    ids = [usuario.id] + ([parceiro.id] if parceiro else [])

    # Depósitos por pessoa
    depositos = []; total = 0
    for uid in ids:
        val = db.session.execute(text(
            "SELECT COALESCE(SUM(valor),0) FROM conjunta_depositos "
            "WHERE usuario_id=:u AND EXTRACT(month FROM data)=:m AND EXTRACT(year FROM data)=:y"
        ), {'u': uid, 'm': mes, 'y': ano}).scalar() or 0
        if val > 0:
            u_obj = Usuario.query.get(uid)
            nome = NOMES_CASAL.get(u_obj.phone, u_obj.nome or 'Parceiro') if u_obj else 'Parceiro'
            depositos.append(f"💸 {nome} meteu: {val:.0f}€")
            total += val

    # Gastos conjunta de ambos este mês
    gastos_rows = []
    gasto_total = 0
    for uid in ids:
        u_obj = Usuario.query.get(uid)
        nome_u = NOMES_CASAL.get(u_obj.phone, u_obj.nome or 'Parceiro') if u_obj else 'Parceiro'
        rows = db.session.query(Despesa).filter(
            Despesa.usuario_id==uid,
            db.extract('month',Despesa.data)==mes,
            db.extract('year',Despesa.data)==ano,
            Despesa.descricao.like('[conjunta]%')
        ).order_by(Despesa.data.desc()).all()
        for d in rows:
            desc = d.descricao.replace('[conjunta] ','').replace('[conjunta]','').strip()
            emoji_c = EMOJI_CAT.get(d.categoria,'💳')
            gastos_rows.append(f"  {emoji_c} {desc[:25]} — {d.valor:.2f}€ ({nome_u})")
            gasto_total += d.valor

    resta = total - gasto_total

    if total == 0 and gasto_total == 0:
        enviar_mensagem(phone_raw,
            f"💑 Conta conjunta\n"
            f"📭 Ainda nao meteram dinheiro nem gastaram este mes.\n\n"
            f"Para meter: 'metemos 80 na conjunta'")
        return

    pct_gasto = round(gasto_total/total*100) if total > 0 else 0  # % de uso da conjunta
    barra = '▓'*(pct_gasto//10) + '░'*(10-pct_gasto//10)

    msg = f"💑 *Conta conjunta* — {nomes_mes_curto(mes)}\n"
    msg += f"━━━━━━━━━━━━━━\n"
    msg += f"_Para jantares, lanches, cinema e saídas a dois_ 🍽️🎬\n\n"

    # Quem meteu quanto + equilíbrio
    if depositos:
        msg += "\n".join(depositos) + "\n"
    msg += f"💰 *Total depositado:* {total:.0f}€\n"

    # Barra de uso
    msg += f"\n*Já usaram:* {gasto_total:.0f}€ ({pct_gasto}%)\n"
    msg += f"{barra}\n"

    if gastos_rows:
        msg += f"\n🛒 *Onde foi:*\n"
        msg += "\n".join(gastos_rows[:6]) + "\n"
        if len(gastos_rows) > 6:
            msg += f"  _... e mais {len(gastos_rows)-6}_\n"

    msg += f"\n💚 *Resta:* {max(resta,0):.0f}€"
    if resta < 0:
        msg += f"\n⚠️ Passaram {abs(resta):.0f}€ — metam mais!"

    # Equilíbrio entre os dois
    if len(depositos) == 2 and total > 0:
        vals = []
        for uid in ids:
            v = db.session.execute(text(
                "SELECT COALESCE(SUM(valor),0) FROM conjunta_depositos "
                "WHERE usuario_id=:u AND EXTRACT(month FROM data)=:m AND EXTRACT(year FROM data)=:y"),
                {'u': uid, 'm': mes, 'y': ano}).scalar() or 0
            vals.append(v)
        if abs(vals[0] - vals[1]) > 10:
            quem_menos = NOMES_CASAL.get(Usuario.query.get(ids[0]).phone) if vals[0] < vals[1] else NOMES_CASAL.get(Usuario.query.get(ids[1]).phone)
            dif = abs(vals[0] - vals[1])
            msg += f"\n\n⚖️ {quem_menos} meteu menos {dif:.0f}€ este mês"
        else:
            msg += f"\n\n⚖️ Estão equilibrados! 👏"

    msg += f"\n\n💡 _Diz_ *metemos X na conjunta* _para reforçar_"
    enviar_mensagem(phone_raw, msg)

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
    p = calcular_plano(receita or 0, modo, total_fut, phone=usuario.phone)
    disp = p['gastar'] - gp
    nomes = ['Janeiro','Fevereiro','Marco','Abril','Maio','Junho','Julho','Agosto','Setembro','Outubro','Novembro','Dezembro']

    saldo_mes = receita - gp
    msg = f"📊 *{nomes[mes-1]} {ano}*\n"
    msg += f"━━━━━━━━━━━━━━\n"
    msg += f"💰 Receita:  {receita:.0f}€\n"
    msg += f"🛒 Gastos:  {gp:.0f}€\n"
    if gc > 0:
        msg += f"💑 Conjunta:  {gc:.0f}€\n"
    sinal = "🟢" if saldo_mes >= 0 else "🔴"
    msg += f"{sinal} Saldo:  {saldo_mes:+.0f}€\n"
    msg += f"━━━━━━━━━━━━━━\n"

    if por_cat_sorted:
        msg += f"*Para onde foi o dinheiro:*\n"
        for cat, total in por_cat_sorted[:6]:
            pct = round(total/gp*100) if gp > 0 else 0
            barra = '▓' * (pct // 10) + '░' * (10 - pct // 10)
            msg += f"{EMOJI_CAT.get(cat,'💳')} {cat.capitalize()}\n"
            msg += f"   {barra} {total:.0f}€ · {pct}%\n"

    if not mes_override and agora().day > 3 and gp > 0:
        ritmo = gp/agora().day*30
        msg += f"\n🔮 Ao ritmo atual: ~{ritmo:.0f}€ até fim do mês"
        if por_cat_sorted:
            top_cat, top_val = por_cat_sorted[0]
            if top_val > 50:
                msg += f"\n💡 Cortar 30% em {top_cat} = +{top_val*0.3*12:.0f}€/ano"

    msg += "\n\n💡 Diz o *número* da categoria para detalhes\n📊 zedasfinancas.netlify.app"

    # Guarda as categorias no estado para o utilizador poder selecionar
    cats_estado = {str(i+1): cat for i, (cat, _) in enumerate(por_cat_sorted)}
    phone = phone_raw.replace('@lid','').replace('@c.us','').split('@')[0]
    set_estado(phone, 'resumo_categoria', {'cats': cats_estado, 'mes': mes, 'ano': ano})
    # Comparação com mês anterior
    try:
        mes_ant = mes - 1 if mes > 1 else 12
        ano_ant = ano if mes > 1 else ano - 1
        gp_ant = db.session.query(db.func.sum(Despesa.valor)).filter(
            Despesa.usuario_id==usuario.id,
            db.extract('month',Despesa.data)==mes_ant,
            db.extract('year',Despesa.data)==ano_ant).scalar() or 0
        if gp_ant > 0 and gp > 0:
            diff_pct = round((gp - gp_ant) / gp_ant * 100)
            nomes_m = ['Jan','Fev','Mar','Abr','Mai','Jun','Jul','Ago','Set','Out','Nov','Dez']
            nome_ant = nomes_m[mes_ant-1]
            if diff_pct <= -20:
                msg += f"\n\n🎉 Gastaste *{abs(diff_pct)}% menos* que em {nome_ant}! Boa!"
            elif diff_pct <= -5:
                msg += f"\n\n👏 Gastaste *{abs(diff_pct)}% menos* que em {nome_ant}"
            elif diff_pct >= 30:
                msg += f"\n\n⚠️ Gastaste *{diff_pct}% mais* que em {nome_ant} ({gp_ant:.0f}€)"
            elif diff_pct >= 10:
                msg += f"\n\n📈 Gastaste *{diff_pct}% mais* que em {nome_ant}"
            # Categoria que mais subiu
            for cat_c, total_c in por_cat_sorted[:3]:
                ant_c = db.session.query(db.func.sum(Despesa.valor)).filter(
                    Despesa.usuario_id==usuario.id, Despesa.categoria==cat_c,
                    db.extract('month',Despesa.data)==mes_ant,
                    db.extract('year',Despesa.data)==ano_ant).scalar() or 0
                if ant_c > 0 and total_c > 0:
                    diff_c = round((total_c - ant_c) / ant_c * 100)
                    em_c = EMOJI_CAT.get(cat_c,'💳')
                    if diff_c <= -30:
                        msg += f"\n{em_c} {cat_c.capitalize()} -30% vs {nome_ant} 🔥"
                    elif diff_c >= 50:
                        msg += f"\n{em_c} {cat_c.capitalize()} +{diff_c}% vs {nome_ant} ⚠️"
    except Exception as e:
        log.error(f"comparacao resumo: {e}")
    enviar_mensagem(phone_raw, msg)

def enviar_plano_mes(phone_raw, usuario):
    _dias_sal = dias_para_salario(usuario)
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
    dias = dias_para_salario(usuario)
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
                enviar_mensagem(phone_raw, f"Não encontrei '{chave}'. Tens:\n{lista}")
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
            enviar_mensagem(phone_raw, f"Não encontrei gastos com {nome} 🤔"); return
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
        else: enviar_mensagem(phone_raw, f"Não encontrei '{chave}' 🤔 Diz 'wishlist' para ver.")
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
            # Se a pessoa é o parceiro -> notificar diretamente
            meu_nome_d2 = NOMES_CASAL.get(usuario.phone, 'Alguem')
            parceiro_phone_d2 = get_parceiro_phone(usuario.phone)
            nome_parceiro_d2 = NOMES_CASAL.get(parceiro_phone_d2, '').lower() if parceiro_phone_d2 else ''
            if parceiro_phone_d2 and pessoa.lower() == nome_parceiro_d2:
                notificar_parceiro(usuario.phone,
                    f"💸 {meu_nome_d2} deve-te {valor:.2f}€!\n"
                    f"📝 Motivo: {desc}\n\n"
                    f"Quando ele(a) pagar, ele(a) diz 'já paguei ao {nome_parceiro_d2}' 😉")
        else:
            db.session.execute(text("INSERT INTO splitting (usuario_id,descricao,valor_total,valor_cada,pessoa,pago) VALUES (:u,:d,:vt,:vc,:p,FALSE)"),
                {'u':usuario.id,'d':desc,'vt':valor,'vc':valor,'p':pessoa})
            db.session.commit()
            enviar_mensagem(phone_raw, f"📝 Anotado! {pessoa} deve-te {valor:.2f}€\n😏 Vamos ver quando aparece com o dinheiro...")
            # Se a pessoa é o parceiro → notificar diretamente
            meu_nome_d = NOMES_CASAL.get(usuario.phone, 'Alguém')
            parceiro_phone_d = get_parceiro_phone(usuario.phone)
            nome_parceiro_d = NOMES_CASAL.get(parceiro_phone_d, '').lower() if parceiro_phone_d else ''
            if parceiro_phone_d and pessoa.lower() == nome_parceiro_d:
                notificar_parceiro(usuario.phone,
                    f"💸 Deves {valor:.2f}€ ao(à) {meu_nome_d}!\n"
                    f"📝 Motivo: {desc}\n\n"
                    f"Quando pagares diz 'já paguei ao {meu_nome_d.lower()}' 😉")
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

    meus = g(usuario); deles = g(parceiro)

    # Comparação justa: % do orçamento "para gastar" usado (não valor bruto)
    try:
        _, p_meu = calcular_disponivel(usuario)
        _, p_par = calcular_disponivel(parceiro)
        orc_meu = p_meu.get('gastar', 0)
        orc_par = p_par.get('gastar', 0)
        pct_meu = (meus / orc_meu * 100) if orc_meu > 0 else 0
        pct_par = (deles / orc_par * 100) if orc_par > 0 else 0
    except Exception:
        pct_meu = pct_par = 0

    msg = f"⚔️ *Batalha do mês!*\n_(quem gere melhor o seu orçamento)_\n\n"
    if pct_meu > 0 and pct_par > 0:
        msg += f"{'🏆' if pct_meu<=pct_par else '😅'} {meu_nome}: {meus:.0f}€ _({pct_meu:.0f}% do orçamento)_\n"
        msg += f"{'🏆' if pct_par<=pct_meu else '😅'} {par_nome}: {deles:.0f}€ _({pct_par:.0f}% do orçamento)_\n\n"
        diff_pct = abs(pct_meu - pct_par)
        if pct_meu < pct_par:
            msg += f"Estás a gerir melhor! Tens mais margem que o {par_nome} 👑"
        elif pct_par < pct_meu:
            msg += f"O {par_nome} está a gerir melhor este mês 😬 Dá a volta!"
        else:
            msg += f"Empate técnico! 🤝"
    else:
        # Fallback para valor bruto se não houver orçamentos
        diff = abs(meus-deles)
        msg += f"{'🏆' if meus<=deles else '😅'} {meu_nome}: {meus:.0f}€\n"
        msg += f"{'🏆' if deles<=meus else '😅'} {par_nome}: {deles:.0f}€\n\n"
        if meus < deles: msg += f"Gastaste menos! 👑"
        elif deles < meus: msg += f"O {par_nome} gastou menos 😬"
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


def interpretar_objetivo_ia(texto):
    """Usa IA (Llama 70B) para extrair nome, prazo, valor inicial e se é conjunto."""
    try:
        from groq import Groq
        import json as _json
        hoje = agora()
        resp = Groq(api_key=GROQ_API_KEY).chat.completions.create(
            model='llama-3.3-70b-versatile', max_tokens=250, temperature=0,
            messages=[
                {'role':'system','content':
                 f'Extrai dados de um objetivo de poupança a partir do texto do utilizador. '
                 f'Hoje é {hoje.day}/{hoje.month}/{hoje.year}. '
                 f'Responde APENAS JSON válido, sem texto extra, com esta estrutura: '
                 f'{{"nome": "nome curto e claro do objetivo (ex: PC novo, Viagem a Roma, Prenda da Taty)", '
                 f'"mes_alvo": número 1-12 ou null, '
                 f'"valor_inicial": número ou 0, '
                 f'"conjunto": true se mencionar a Luana/casal/juntos/nós/conjunto, senão false}}. '
                 f'O nome deve ser natural e específico, não genérico. IGNORA perguntas retóricas como "sabes o que quero?". '
                 f'Extrai SÓ o objeto da poupança. Se disser "pc novo" o nome é "PC novo". Se "ir a roma" é "Viagem a Roma". '
                 f'Se NÃO houver objeto claro, usa "Poupança". Nunca uses frases ou perguntas como nome.'},
                {'role':'user','content': texto}
            ])
        txt = resp.choices[0].message.content.strip()
        txt = re.sub(r'^```(?:json)?|```$', '', txt, flags=re.MULTILINE).strip()
        dados = _json.loads(txt)
        return {
            'nome': str(dados.get('nome', ''))[:50] or 'Poupança',
            'mes_alvo': dados.get('mes_alvo') if isinstance(dados.get('mes_alvo'), int) and 1 <= dados.get('mes_alvo',0) <= 12 else None,
            'valor_inicial': float(dados.get('valor_inicial', 0)) if isinstance(dados.get('valor_inicial'), (int,float)) else 0,
            'conjunto': bool(dados.get('conjunto', False))
        }
    except Exception as e:
        log.error(f"interpretar_objetivo_ia: {e}")
        return None

def emoji_objetivo(nome):
    """Escolhe emoji(s) temático(s) para o nome do objetivo (estilo GranaZen)."""
    n = nome.lower()
    mapa = {
        ('paris','franca','frança'): '✈️🇫🇷',
        ('londres','inglaterra','reino unido'): '✈️🇬🇧',
        ('lisboa','porto','portugal'): '✈️🇵🇹',
        ('espanha','madrid','barcelona'): '✈️🇪🇸',
        ('italia','itália','roma','milao'): '✈️🇮🇹',
        ('viagem','viajar','ferias','férias','voo'): '✈️',
        ('praia','mar','verao','verão'): '🏖️',
        ('ps5','playstation','xbox','consola','jogos','jogo'): '🎮',
        ('pc','computador','portatil','portátil','laptop'): '💻',
        ('telemovel','telemóvel','iphone','android','samsung'): '📱',
        ('carro','automovel','automóvel','mota'): '🚗',
        ('casa','apartamento','renda','imovel','imóvel'): '🏠',
        ('movel','móvel','mobilia','mobília','sofa','sofá'): '🛋️',
        ('emergencia','emergência','fundo','seguranca','segurança'): '🛡️',
        ('casamento','anel','noivado'): '💍',
        ('bebe','bebé','filho','filha'): '👶',
        ('curso','formacao','formação','escola','estudo'): '📚',
        ('ginasio','ginásio','gym','fitness'): '💪',
        ('relogio','relógio','watch'): '⌚',
        ('camara','câmara','fotografia','foto'): '📷',
        ('bicicleta','bike','trotinete'): '🚲',
        ('natal','presente','prenda'): '🎁',
    }
    for chaves, emoji in mapa.items():
        if any(c in n for c in chaves):
            return emoji
    return '🎯'

def processar_objetivo_poupanca(phone_raw, usuario, texto):
    t = texto.lower()
    if any(p in t for p in ['ver','lista','objetivos','metas','mostrar']):
        try:
            rows = db.session.execute(text(
                "SELECT descricao, valor_objetivo, valor_atual, data_meta FROM objetivos_poupanca WHERE usuario_id=:id AND concluido=FALSE"),
                {'id':usuario.id}).fetchall()
            if not rows:
                enviar_mensagem(phone_raw, "Nao tens objetivos ainda 🎯\nCria: 'quero poupar 500€ para ferias'"); return
            msg = "🎯 *Objetivos:*\n\n"
            for desc_r, val_r, at_r, data_meta_r in rows:
                at_real = at_r or 0
                # Tentar saldo real do cofre
                saldo_cofre = saldo_cofre_objetivo(usuario.id, desc_r)
                if saldo_cofre is not None:
                    at_real = saldo_cofre
                    fonte = "🔄 ao vivo"
                else:
                    fonte = ""
                pct = int(at_real/val_r*100) if val_r>0 else 0
                barra = '█'*(pct//10) + '░'*(10-pct//10)
                em = emoji_objetivo(desc_r)
                msg += f"{em} *{desc_r}*\n"
                msg += f"{barra} {pct}% · {at_real:.0f}€ de {val_r:.0f}€ {fonte}\n"
                # Prazo e ritmo necessário (só se houver data_meta)
                if data_meta_r:
                    dias_falta = (data_meta_r - agora().replace(tzinfo=None).date()).days
                    falta_valor = max(val_r - at_real, 0)
                    if dias_falta > 0 and falta_valor > 0:
                        meses_falta_disp = max(dias_falta / 30.4, 0.5)
                        por_mes_necessario = falta_valor / meses_falta_disp
                        msg += f"📅 {dias_falta} dias · precisas de ~{por_mes_necessario:.0f}€/mês\n"
                    elif dias_falta <= 0 and falta_valor > 0:
                        msg += f"⚠️ Prazo passado! Faltam {falta_valor:.0f}€\n"
                msg += "\n"
            enviar_mensagem(phone_raw, msg)
        except Exception as e:
            log.error(f"objetivos: {e}"); enviar_mensagem(phone_raw, "Erro 😕")
        return

    valor = extrair_valor(texto)
    desc = 'Objetivo'
    m_para = re.search(r'para\s+(?:ir a |um |uma |o |a |uns |umas )?([a-zà-ú][a-zà-úA-ZÀ-Ú]+(?:\s+[a-zà-ú][a-zà-úA-ZÀ-Ú]+)?)(?=\s+(?:em|no|na|com|daqui|ate|para)\b|[.,!?]|$)', texto, re.IGNORECASE)
    if m_para and '?' not in texto:
        candidato = m_para.group(1).strip()
        stop_frase = ['sabes','quero','que ','isso','aquilo','isto','ver eles','poupar']
        if not any(s in candidato.lower() for s in stop_frase):
            desc = candidato[:30].capitalize()
    eh_conjunto_pre = any(p in texto.lower() for p in ['luana','ruben','casal','juntos','conjunto','nós','nos dois','comigo'])

    if valor == 0:
        # Guardar o que já sabemos e esperar o valor na próxima mensagem
        phone_pend = phone_raw.replace('@lid','').replace('@c.us','').split('@')[0]
        set_estado(phone_pend, 'objetivo_valor_pendente',
                   {'desc': desc if desc != 'Objetivo' else '', 'conjunto': eh_conjunto_pre})
        desc_q = f' para *{desc}*' if desc != 'Objetivo' else ''
        enviar_mensagem(phone_raw, f"💰 Quanto queres poupar{desc_q}?")
        return

    # Perguntar TUDO de uma vez (estilo GranaZen) — depois parseia a resposta completa
    phone = phone_raw.replace('@lid','').replace('@c.us','').split('@')[0]
    emoji_d = emoji_objetivo(desc) if desc != 'Objetivo' else '🎯'
    set_estado(phone, 'objetivo_tudo', {'valor': valor, 'desc': desc if desc != 'Objetivo' else '', 'emoji': emoji_d})

    desc_txt = f" para *{desc}*" if desc != 'Objetivo' else ""
    msg = f"{emoji_d} *Novo objetivo: {valor:.0f}€*{desc_txt}\n\n"
    msg += f"Conta-me numa mensagem:\n"
    if desc == 'Objetivo':
        msg += f"📝 *Para quê* (ex: Viagem, PS5)\n"
    msg += f"📅 *Até quando* (ex: dezembro, daqui a 6 meses)\n"
    msg += f"💰 *Quanto já tens* (ou diz que não tens)\n\n"
    msg += f"_Ex: \"para o Algarve em dezembro, já tenho 200\"_\n"
    msg += f"_Se for a dois, diz \"com a Luana\" 💑_"
    enviar_mensagem(phone_raw, msg)

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
    msg = "Escolhe o modo de poupança:\n\n"
    for i, (k, m) in enumerate(MODOS_POUPANCA.items(), 1):
        msg += f"{i}. {m['emoji']} {m['nome']}\n{m['desc']}\n\n"
    msg += "Responde com o nome: 'modo maximo', 'modo equilibrado' ou 'modo relaxado' 😊"
    enviar_mensagem(phone_raw, msg)

def enviar_boas_vindas(phone_raw, usuario=None, phone=None):
    dias = dias_para_salario(usuario)
    perfil = get_perfil(phone or (usuario.phone if usuario else ''))
    nome = perfil['nome']
    eh_ruben = (phone or (usuario.phone if usuario else '')) == PHONE_RUBEN
    trat = "mano" if eh_ruben else "querida"
    tem_salario = usuario and usuario.salario_liquido

    if not tem_salario:
        if phone: set_estado(phone, 'escolher_modo', {})
        msg = (f"Olá {nome}! 👋 Eu sou o *Zé das Finanças*!\n\n"
               f"Sou o teu assistente de dinheiro aqui no WhatsApp. Comigo podes registar gastos, "
               f"controlar o salário e poupar — é só falares comigo normalmente 😊\n\n"
               f"Mas primeiro, {trat}, escolhe como queres poupar:\n\n"
               f"💎 *Máximo* — poupas tudo, só o essencial\n"
               f"⚖️ *Equilibrado* — poupas bem mas ainda vives 😊\n"
               f"😎 *Relaxado* — vives a vida mas ainda poupas\n\n"
               f"Responde: *modo equilibrado* (ou máximo / relaxado)\n\n"
               f"_Podes mudar quando quiseres!_ 🚀")
    else:
        disp, p = calcular_disponivel(usuario)
        m = MODOS_POUPANCA[get_modo(usuario.id)]
        reserva = get_reserva(usuario.id)
        emoji_cumpr = "🤙" if eh_ruben else "👋"
        msg = (f"Olá de volta, {nome}! {emoji_cumpr}\n\n"
               f"💳 Para gastar: *{disp:.0f}€*\n"
               f"💎 Poupança prevista: {p['poupanca']:.0f}€\n"
               f"🛡️ Reserva: {reserva:.0f}€\n"
               f"📅 Faltam {dias} dia{'s' if dias!=1 else ''} para o salário\n\n"
               f"Diz *ajuda* para veres tudo o que sei fazer 😎")
    enviar_mensagem(phone_raw, msg)

def enviar_ajuda(phone_raw):
    enviar_mensagem(phone_raw, """😎 *Tudo o que sei fazer:*

💸 *Registar gastos*
• 15 bk · 25 conti · 50 galp · jantar 30
• ontem gastei 10 no almoço
• foto do talão · áudio · PDF
• cola SMS do banco → regista sozinho

🗂️ *Ver e gerir*
• ver transações → extrato com códigos
• apaga ABC → apaga pelo código
• afinal foi 25 → corrige o último
• como estou → saúde financeira
• quanto posso gastar hoje → orçamento do dia
• resumo · plano · quanto tenho

💰 *Salário e contas*
• recebi 1500 → plano automático
• recebi 40 dos avós → dinheiro extra
• onde vai o dinheiro → distribuição
• saldo BPI 1200 → guarda saldo
• fixo carro 380 → muda um fixo
• património → total nas contas

🎯 *Objetivos e metas*
• objetivo PS5 500 → meta de poupança
• meta restaurante 100 → limite mensal
• objetivos → progresso

✂️ *A dois*
• luana deve-me 15 do jantar
• dividi 60 do jantar com a Luana
• já paguei ao ruben
• jantar 30 na conjunta

⛽ *Carro*
• tinha 80km meti 20€ fiquei com 200km
• vamos ao Algarve → custo da viagem
• consumo carro

🧾 *Faturas*
• cola texto da fatura → entidade/ref
• paguei a fatura

⏰ *Outros*
• lembra-me de X amanhã às 10h
• wishlist · quero sapatilhas nike 89€
• assinatura netflix 12
• aniversário da Ana 15/3
• gasolina mais barata no barreiro

🔧 muda modo · aprende que X é roupa · limpa conversa

💡 Sugestões? Manda! 🚀""")

def filtrar_resposta(txt, phone=None):
    """Filtra respostas da IA: remove zuca (PT-BR) e ajusta ao perfil."""
    # ── Limpar brasileirismos (zuca) → PT-PT ──
    zuca_subs = [
        (r'\bvocê\b','tu'),(r'\bvc\b','tu'),(r'\bvocês\b','vocês'),
        (r'\bgrana\b','dinheiro'),(r'\bmassa\b','dinheiro'),
        (r'\blegal\b','fixe'),(r'\bshow\b','top'),(r'\bmaneiro\b','fixe'),
        (r'\bbacana\b','fixe'),(r'\bda hora\b','fixe'),
        (r'\bpra\b','para'),(r'\bpro\b','para o'),
        (r'\ba gente\b','nós'),(r'\bgalera\b','pessoal'),
        (r'\bcafezinho\b','café'),(r'\bdinheirinho\b','dinheiro'),
        (r'\bgraninha\b','dinheiro'),(r'\bnenê\b','bebé'),
        (r'\bbabaca\b','parvo'),(r'\bvaleu\b','obrigado'),
        (r'\bfalou\b','combinado'),(r'\bbeleza\b','tudo bem'),
        (r'\btá\b','está'),(r'\btô\b','estou'),(r'\btá bom\b','está bem'),
        (r'\bcomprinhas\b','compras'),(r'\bbar(?:a|à)to\b','barato'),
        (r'\bcelular\b','telemóvel'),(r'\bgeladeira\b','frigorífico'),
        (r'\bônibus\b','autocarro'),(r'\btrem\b','comboio'),
        (r'\bcafé da manhã\b','pequeno-almoço'),(r'\bsorvete\b','gelado'),
        (r'\bbanana\b,','calma,'),(r'\bcurtir\b','aproveitar'),
        (r'\bnossa\b!','fogo!'),(r'\baí ó\b','olha'),
    ]
    for p, s in zuca_subs:
        txt = re.sub(p, s, txt, flags=re.IGNORECASE)
    # ── Ajuste de género (perfil feminino) ──
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
Usa: {perfil['expressoes']}. PORTUGUÊS DE PORTUGAL (PT-PT) obrigatório, NUNCA brasileiro.
PROIBIDO escrever: você, vc, grana, legal, massa (=dinheiro), pra, bacana, show, a gente, tá, tô, celular, ônibus, curtir, nossa!, bora demais.
USA: tu, fixe, dinheiro, para, está, telemóvel, giro, porreiro. Max 2 linhas + 1 emoji.
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

def proximo_dia_util(d):
    """Devolve o próximo dia útil a partir de d (exclusive)."""
    p = d + timedelta(days=1)
    while p.weekday() >= 5:
        p += timedelta(days=1)
    return p

def dia_util_anterior(d):
    """Devolve o dia útil anterior a d (exclusive)."""
    p = d - timedelta(days=1)
    while p.weekday() >= 5:
        p -= timedelta(days=1)
    return p

def guardar_salario_pendente(usuario_id, valor, data_pagamento):
    """Guarda salário para confirmar no dia de pagamento."""
    try:
        # Apaga pendentes antigos não processados do mesmo user
        db.session.execute(text(
            "DELETE FROM salarios_pendentes WHERE usuario_id=:u AND processado=FALSE"),
            {'u': usuario_id})
        db.session.execute(text(
            "INSERT INTO salarios_pendentes (usuario_id, valor, data_pagamento) VALUES (:u,:v,:d)"),
            {'u': usuario_id, 'v': valor, 'd': data_pagamento})
        db.session.commit()
        return True
    except Exception as e:
        log.error(f"guardar_salario_pendente: {e}"); db.session.rollback()
        return False

def limpar_salarios_pendentes(usuario_id):
    """Remove pendentes (quando o user regista manualmente)."""
    try:
        db.session.execute(text(
            "UPDATE salarios_pendentes SET processado=TRUE WHERE usuario_id=:u AND processado=FALSE"),
            {'u': usuario_id})
        db.session.commit()
    except Exception:
        db.session.rollback()


def processar_salarios_pendentes():
    """Às 9h do dia de pagamento, confirma a entrada e manda o plano completo."""
    with app.app_context():
        hoje = agora()
        if hoje.hour != 9: return
        try:
            rows = db.session.execute(text(
                "SELECT id, usuario_id, valor FROM salarios_pendentes "
                "WHERE processado=FALSE AND data_pagamento<=:d"),
                {'d': hoje.date()}).fetchall()
        except Exception as e:
            log.error(f"salarios_pendentes query: {e}"); db.session.rollback(); return

        for row_id, uid, valor in rows:
            try:
                u = db.session.get(Usuario, uid)
                if not u or not u.phone: continue
                phone_raw = f"{u.phone}@lid"
                enviar_mensagem(phone_raw,
                    f"💰 Bom dia! O salário caiu na conta!\n"
                    f"Recebeste *{valor:.2f}€* 🎉")
                processar_receita(phone_raw, u, f"recebi {valor}")
                try:
                    u.ultimo_salario_mes = f"{hoje.year}-{hoje.month:02d}"
                    db.session.commit()
                except Exception: pass
                db.session.execute(text(
                    "UPDATE salarios_pendentes SET processado=TRUE WHERE id=:i"), {'i': row_id})
                db.session.commit()
                log.info(f"Salário pendente processado: user {uid} = {valor}€")
            except Exception as e:
                log.error(f"processar pendente {row_id}: {e}"); db.session.rollback()

        # Fallback: utilizadores cujo dia de pagamento é hoje mas não puseram valor
        try:
            for u in Usuario.query.all():
                if not u.phone or u.phone == PHONE_RUBEN: continue
                pag = dia_pagamento_usuario(u, hoje.year, hoje.month)
                if pag.date() != hoje.date(): continue
                # Verificar se já processámos este mês
                mes_atual = f"{hoje.year}-{hoje.month:02d}"
                try:
                    ultimo_mes_real = db.session.execute(text(
                        "SELECT ultimo_salario_mes FROM usuarios WHERE id=:u"), {'u': u.id}).scalar()
                except Exception:
                    ultimo_mes_real = None
                if ultimo_mes_real == mes_atual: continue
                # Verificar se tem salário pendente (já respondeu)
                pendente = db.session.execute(text(
                    "SELECT id FROM salarios_pendentes WHERE usuario_id=:u AND processado=FALSE"),
                    {'u': u.id}).fetchone()
                if pendente: continue
                # Não respondeu — perguntar agora
                set_estado(u.phone, 'aguardar_recibo', {'data_pagamento': pag.strftime('%Y-%m-%d')})
                enviar_mensagem(f"{u.phone}@lid",
                    f"Bom dia! 💰 Hoje é dia de receber o salário!\n"
                    f"Já caiu na conta? Manda o valor ou o recibo 📄")
                log.info(f"Fallback recibo enviado para {u.phone}")
        except Exception as e:
            log.error(f"fallback salarios: {e}")


def alertas_preditivos():
    """Avisa quando uma meta de categoria está >80% gasta antes do dia 20."""
    with app.app_context():
        hoje = agora()
        if hoje.hour != 19 or hoje.day > 20 or hoje.day < 8: return
        import calendar as _cal
        _, ultimo = _cal.monthrange(hoje.year, hoje.month)
        dias_restantes = ultimo - hoje.day
        try:
            metas = db.session.execute(text(
                "SELECT m.usuario_id, m.categoria, m.limite, u.phone FROM metas_categoria m "
                "JOIN usuarios u ON u.id=m.usuario_id "
                "WHERE m.mes=:m AND m.ano=:a"), {'m': hoje.month, 'a': hoje.year}).fetchall()
        except Exception:
            db.session.rollback(); return

        for uid, cat, limite, phone in metas:
            if not phone or not limite: continue
            try:
                gasto = db.session.execute(text(
                    "SELECT COALESCE(SUM(valor),0) FROM despesas WHERE usuario_id=:u AND categoria=:c "
                    "AND EXTRACT(month FROM data)=:m AND EXTRACT(year FROM data)=:a"),
                    {'u': uid, 'c': cat, 'm': hoje.month, 'a': hoje.year}).scalar() or 0
                pct = gasto / limite * 100
                if 80 <= pct < 100:
                    # 1 alerta por categoria/mês — usar estado como flag
                    est, dados = get_estado(phone)
                    flag_key = f"alerta_{cat}_{hoje.month}"
                    if dados and dados.get(flag_key): continue
                    emoji_c = EMOJI_CAT.get(cat, '💳')
                    excesso_proj = gasto / (hoje.day) * (ultimo) - limite
                    msg_al = f"⚠️ *{emoji_c} {cat.capitalize()}*\n\n"
                    msg_al += f"🚨 {pct:.0f}% do orçamento utilizado\n\n"
                    msg_al += f"Orçamento: {limite:.0f}€\n"
                    msg_al += f"Gasto: {gasto:.0f}€\n"
                    msg_al += f"Disponível: {limite-gasto:.0f}€\n\n"
                    msg_al += f"📅 Faltam {dias_restantes} dias"
                    if excesso_proj > 0:
                        msg_al += f"\n💡 Ao ritmo atual vais exceder em ~{excesso_proj:.0f}€"
                    enviar_mensagem(f"{phone}@lid", msg_al)
                    novos_dados = dados or {}
                    novos_dados[flag_key] = True
                    set_estado(phone, est or 'alertas', novos_dados)
            except Exception as e:
                log.error(f"alerta preditivo {cat}: {e}"); db.session.rollback()


def streak_gasto_zero():
    """Celebra streaks de dias sem gastos supérfluos — só nos marcos (sem spam)."""
    with app.app_context():
        hoje = agora()
        if hoje.hour != 21: return
        SUPERFLUAS = ['fastfood','restaurante','cafe','roupa','lazer','tecnologia']
        for u in Usuario.query.all():
            if not u.phone: continue
            try:
                # Conta dias consecutivos (até ontem) sem gastos supérfluos
                streak = 0
                for i in range(1, 31):
                    dia = (hoje - timedelta(days=i)).date()
                    g = db.session.execute(text(
                        "SELECT COUNT(*) FROM despesas WHERE usuario_id=:u AND DATE(data)=:d AND categoria = ANY(:cats)"),
                        {'u': u.id, 'd': dia, 'cats': SUPERFLUAS}).scalar() or 0
                    if g > 0: break
                    streak += 1
                if streak in [3, 7, 14, 30]:
                    medalhas = {3:'🥉', 7:'🥈', 14:'🥇', 30:'🏆'}
                    enviar_mensagem(f"{u.phone}@lid",
                        f"{medalhas[streak]} Streak de {streak} dias sem gastos supérfluos!\n"
                        f"🔥 A carteira agradece — continua assim!")
            except Exception as e:
                log.error(f"streak: {e}"); db.session.rollback()



def enviar_lembretes_gerais():
    """A cada 5 min: envia lembretes cuja hora chegou."""
    with app.app_context():
        agora_dt = agora().replace(tzinfo=None)
        try:
            rows = db.session.execute(text(
                "SELECT l.id, l.usuario_id, l.texto, u.phone FROM lembretes l "
                "JOIN usuarios u ON u.id=l.usuario_id "
                "WHERE l.enviado=FALSE AND l.quando<=:agora"),
                {'agora': agora_dt}).fetchall()
        except Exception:
            db.session.rollback(); return
        for lid, uid, texto_l, phone in rows:
            try:
                enviar_mensagem(f"{phone}@lid", f"⏰ Lembrete:\n{texto_l}")
                db.session.execute(text("UPDATE lembretes SET enviado=TRUE WHERE id=:i"), {'i': lid})
                db.session.commit()
            except Exception as e:
                log.error(f"lembrete {lid}: {e}"); db.session.rollback()


def lembrete_contas_receber():
    """Segunda-feira 12h: lembra splitting não pago há mais de 7 dias."""
    with app.app_context():
        hoje = agora()
        if hoje.weekday() != 0: return  # só segunda
        semana_atras = (hoje - __import__('datetime').timedelta(days=7)).replace(tzinfo=None)
        for u in Usuario.query.all():
            if not u.phone: continue
            try:
                rows = db.session.execute(text(
                    "SELECT pessoa, SUM(valor_cada), COUNT(*) FROM splitting "
                    "WHERE usuario_id=:u AND pago=FALSE AND data<:d "
                    "GROUP BY pessoa"),
                    {'u': u.id, 'd': semana_atras}).fetchall()
                for pessoa, total, n in rows:
                    enviar_mensagem(f"{u.phone}@lid",
                        f"👀 {pessoa} ainda te deve {total:.0f}€ "
                        f"({n} conta{'s' if n>1 else ''} há mais de 7 dias)\n"
                        f"Queres mandar-lhe um aviso?")
            except Exception as e:
                log.error(f"contas_receber: {e}"); db.session.rollback()


def relatorio_mensal_automatico():
    """No último dia do mês às 10h30, manda o link do relatório."""
    with app.app_context():
        import calendar as _cal
        hoje = agora()
        _, ultimo = _cal.monthrange(hoje.year, hoje.month)
        if hoje.day != ultimo or hoje.hour != 10: return
        for u in Usuario.query.all():
            if not u.phone: continue
            try:
                url = (f"https://luanabot-production.up.railway.app/api/relatorio"
                       f"?phone={u.phone}&token={u.phone[:8]}zef"
                       f"&mes={hoje.month}&ano={hoje.year}")
                nomes_m_r = ['Janeiro','Fevereiro','Março','Abril','Maio','Junho',
                             'Julho','Agosto','Setembro','Outubro','Novembro','Dezembro']
                enviar_mensagem(f"{u.phone}@lid",
                    f"📊 *Relatório de {nomes_m_r[hoje.month-1]}*\n\nO mês acabou! Aqui está o teu resumo 👇")
                enviar_pdf_whatsapp(f"{u.phone}@lid", u, hoje.month, hoje.year)
            except Exception as e:
                log.error(f"relatorio_mensal: {e}")


def lembrete_poupanca_mensal():
    """Início do mês: lembra de poupar para os objetivos ativos."""
    with app.app_context():
        hoje = agora()
        if hoje.day != 2 or hoje.hour != 10: return  # dia 2 às 10h
        for u in Usuario.query.all():
            if not u.phone: continue
            try:
                objs = db.session.execute(text(
                    "SELECT descricao, valor_objetivo, valor_atual FROM objetivos_poupanca "
                    "WHERE usuario_id=:u AND concluido=FALSE ORDER BY id DESC LIMIT 3"),
                    {'u': u.id}).fetchall()
                if not objs: continue
                msg = "🎯 *Novo mês, novos objetivos!*\n\n"
                msg += "Não te esqueças de poupar para:\n"
                for desc_o, val_o, at_o in objs:
                    emoji_o = emoji_objetivo(desc_o)
                    falta_o = (val_o or 0) - (at_o or 0)
                    pct_o = round((at_o or 0)/val_o*100) if val_o else 0
                    msg += f"{emoji_o} {desc_o} — {pct_o}% (falta {falta_o:.0f}€)\n"
                msg += "\n💡 Diz *guardei X para [objetivo]* quando puseres dinheiro de lado 💪"
                enviar_mensagem(f"{u.phone}@lid", msg)
            except Exception as e:
                log.error(f"lembrete_poupanca: {e}"); db.session.rollback()


def aviso_debitos_fixos():
    """Avisa nos dias típicos de débito de fixos (carro, créditos, etc)."""
    with app.app_context():
        hoje = agora()
        if hoje.hour != 9: return  # uma vez por dia, de manhã
        # Dias típicos de débito (configurável por utilizador no futuro)
        DEBITOS = {
            PHONE_RUBEN: {
                8: ('🚗 Carro', 200), 10: ('💳 Crédito BPI', 50),
                12: ('💳 Crédito Revolut', 50), 5: ('👩 Dinheiro à mãe', 100),
            },
            PHONE_LUANA: {
                8: ('🚗 Carro', 350), 10: ('💼 Ordem', 20),
            }
        }
        for phone, debitos in DEBITOS.items():
            if hoje.day in debitos:
                nome_d, valor_d = debitos[hoje.day]
                u = Usuario.query.filter_by(phone=phone).first()
                if u:
                    enviar_mensagem(f"{phone}@lid",
                        f"📅 *Hoje sai um fixo!*\n\n"
                        f"{nome_d} — {valor_d}€\n\n"
                        f"Confirma que tens saldo na conta 💳")

def repergunta_recibo():
    """Às 15h30, repergunta a quem disse 'não' de manhã."""
    with app.app_context():
        hoje = agora()
        if hoje.hour != 15: return
        for u in Usuario.query.all():
            if not u.phone: continue
            est, dados = get_estado(u.phone)
            if est == 'recibo_repergunta':
                set_estado(u.phone, 'aguardar_recibo', dados)
                enviar_mensagem(f"{u.phone}@lid",
                    "Olá outra vez! 📄 Já chegou o recibo?\nManda o PDF/foto ou diz o valor 😊")

def lembrete_recibo():
    with app.app_context():
        hoje = agora()
        if hoje.hour != 11: return
        for u in Usuario.query.all():
            if not u.phone: continue
            # Ruben recebe recibo automático via Apps Script — não precisa lembrete
            if u.phone == PHONE_RUBEN: continue
            # Véspera (dia útil anterior) do pagamento deste utilizador
            pag = dia_pagamento_usuario(u, hoje.year, hoje.month)
            vespera = dia_util_anterior(pag)
            if hoje.date() == vespera.date():
                set_estado(u.phone, 'aguardar_recibo', {'data_pagamento': pag.strftime('%Y-%m-%d')})
                enviar_mensagem(f"{u.phone}@lid",
                    f"Olá! 📄 Amanhã deves receber o salário!\n"
                    f"Já chegou o recibo? Manda o PDF/foto ou diz o valor 😊")



def verificar_ligacoes_expiradas():
    """Avisa quando a ligação ao banco via Enable Banking está prestes a expirar."""
    with app.app_context():
        hoje = agora()
        if hoje.hour != 9 or hoje.minute >= 30: return
        try:
            expiram = db.session.execute(text(
                "SELECT b.usuario_id, b.banco, b.id, u.phone "
                "FROM bancos_ligados b JOIN usuarios u ON b.usuario_id=u.id "
                "WHERE b.ativo=TRUE AND b.expira IS NOT NULL "
                "AND b.expira BETWEEN NOW() AND NOW() + INTERVAL '7 days'")).fetchall()
            for uid, banco, bid, phone in expiram:
                dias = db.session.execute(text(
                    "SELECT EXTRACT(day FROM expira - NOW()) FROM bancos_ligados WHERE id=:i"),
                    {'i': bid}).scalar() or 0
                dias = int(dias)
                if dias in [7, 3, 1]:
                    enviar_mensagem(f"{phone}@lid",
                        f"⚠️ *A ligação ao {banco.upper()} expira em {dias} dia{'s' if dias>1 else ''}!*\n\n"
                        f"Para renovar e continuar a ter os valores automáticos:\n"
                        f"_Diz_ *renovar {banco}* _para gerar o link_")
        except Exception as e:
            log.error(f"verificar_ligacoes: {e}")

def atualizar_saldos_bancarios():
    """Job diário: atualiza saldos reais de todos os utilizadores (1x/dia)."""
    with app.app_context():
        hoje = agora()
        if hoje.hour != 8 or hoje.minute >= 30: return  # 1x de manhã
        for u in Usuario.query.all():
            try:
                enable_atualizar_saldos(u, silencioso=True)
            except Exception as e:
                log.error(f"atualizar_saldos {u.phone}: {e}")



def enable_verificar_debitos_variaveis(usuario):
    """No dia X, vai buscar transações ao Revolut e faz match com débitos variáveis."""
    import requests as _r
    from difflib import SequenceMatcher
    headers = _enable_headers()
    if not headers:
        return []
    hoje = agora()
    resultados = []
    try:
        # Buscar débitos variáveis que saem hoje
        debitos_hoje = db.session.execute(text(
            "SELECT id, nome, categoria, valor_medio FROM pagamentos_agendados "
            "WHERE usuario_id=:u AND ativo=TRUE AND variavel=TRUE AND dia_mes=:d"),
            {'u': usuario.id, 'd': hoje.day}).fetchall()
        if not debitos_hoje:
            return []
        # Buscar account_id do Revolut
        acc = db.session.execute(text(
            "SELECT account_id FROM bancos_ligados "
            "WHERE usuario_id=:u AND ativo=TRUE AND banco='revolut' AND account_id IS NOT NULL LIMIT 1"),
            {'u': usuario.id}).fetchone()
        if not acc:
            return []
        acc_id = acc[0]
        # Ler transações de hoje
        data_ini = hoje.strftime('%Y-%m-%dT00:00:00')
        data_fim = hoje.strftime('%Y-%m-%dT23:59:59')
        r = _r.get(f"{ENABLE_BASE}/accounts/{acc_id}/transactions",
            headers=headers, params={'date_from': data_ini, 'date_to': data_fim}, timeout=20)
        if r.status_code != 200:
            log.error(f"transacoes enable: {r.status_code}")
            return []
        transacoes = r.json().get('transactions', [])
        # Fazer match por nome (fuzzy)
        for pid, nome_d, cat_d, media_d in debitos_hoje:
            melhor_match = None; melhor_score = 0
            for tx in transacoes:
                desc_tx = (tx.get('creditor_name') or tx.get('remittance_information') or '').lower()
                nome_lower = nome_d.lower()
                score = SequenceMatcher(None, nome_lower, desc_tx).ratio()
                # Também aceita se o nome está contido na descrição
                if nome_lower in desc_tx or any(w in desc_tx for w in nome_lower.split() if len(w) > 3):
                    score = max(score, 0.8)
                if score > melhor_score:
                    melhor_score = score
                    melhor_match = tx
            if melhor_match and melhor_score > 0.5:
                valor_tx = abs(float(melhor_match.get('transaction_amount', {}).get('amount', 0)))
                if valor_tx > 0:
                    resultados.append((pid, nome_d, cat_d, valor_tx, melhor_score))
        return resultados
    except Exception as e:
        log.error(f"enable_debitos_variaveis: {e}")
        return []

def aviso_pagamentos_agendados():
    """Avisa 3 dias antes de cada pagamento e processa prestações no dia."""
    with app.app_context():
        hoje = agora()
        if hoje.hour != 9 or hoje.minute >= 30: return
        for u in Usuario.query.all():
            if not u.phone: continue
            try:
                pags = db.session.execute(text(
                    "SELECT id, nome, valor, dia_mes, prestacoes_total, prestacoes_pagas, categoria "
                    "FROM pagamentos_agendados WHERE usuario_id=:u AND ativo=TRUE"),
                    {'u': u.id}).fetchall()
                for pid, nome, valor, dia, p_tot, p_pag, cat in pags:
                    falta = dia - hoje.day
                    em = EMOJI_CAT.get(cat, '💳')
                    # Aviso 3 dias antes
                    if falta == 3:
                        enviar_mensagem(f"{u.phone}@lid",
                            f"📅 *Lembrete de pagamento*\n\n{em} {nome}\n💰 {valor:.0f}€\n📆 Sai dia {dia} (faltam 3 dias)\n\nConfirma que tens saldo 💳")
                    # No dia: se variável, perguntar; senão registar
                    elif falta == 0:
                        eh_variavel = db.session.execute(text(
                            "SELECT variavel, valor_medio FROM pagamentos_agendados WHERE id=:i"),
                            {'i': pid}).fetchone()
                        if eh_variavel and eh_variavel[0]:
                            media = eh_variavel[1] or 0
                            # Tentar ler valor real da API do Revolut
                            api_ok = False
                            try:
                                matches = enable_verificar_debitos_variaveis(u)
                                for m_pid, m_nome, m_cat, m_valor, m_score in matches:
                                    if m_pid == pid:
                                        # Encontrou! Registar automático
                                        db.session.add(Despesa(usuario_id=u.id, valor=m_valor,
                                            descricao=nome, categoria=cat, data=hoje.replace(tzinfo=None)))
                                        nova_media = m_valor if media == 0 else round((media + m_valor) / 2, 2)
                                        db.session.execute(text(
                                            "UPDATE pagamentos_agendados SET valor_medio=:m WHERE id=:i"),
                                            {'m': nova_media, 'i': pid})
                                        db.session.commit()
                                        em_d = EMOJI_CAT.get(cat, '💳')
                                        enviar_mensagem(f"{u.phone}@lid",
                                            f"🤖 *Vi no Revolut:*\n{em_d} {nome} — *{m_valor:.2f}€*\n✅ Registado automaticamente!")
                                        api_ok = True
                                        break
                            except Exception as e_api:
                                log.error(f"api debito variavel: {e_api}")
                            # Fallback: perguntar manualmente
                            if not api_ok:
                                media_txt = f" (média: {media:.0f}€)" if media > 0 else ""
                                set_estado(u.phone, 'confirmar_debito_variavel', {'pid': pid, 'nome': nome, 'cat': cat})
                                enviar_mensagem(f"{u.phone}@lid",
                                    f"📅 *Hoje sai o {nome}!*{media_txt}\n\nSabes quanto foi? Diz só o valor (ex: _24_)\nou _média_ para usar a estimativa")
                            continue
                        db.session.add(Despesa(usuario_id=u.id, valor=valor,
                            descricao=nome, categoria=cat, data=hoje.replace(tzinfo=None)))
                        if p_tot < 999:  # parcelado
                            nova_pag = p_pag + 1
                            if nova_pag >= p_tot:
                                db.session.execute(text("UPDATE pagamentos_agendados SET ativo=FALSE, prestacoes_pagas=:p WHERE id=:i"),
                                    {'p': nova_pag, 'i': pid})
                                msg_fim = f"\n\n🎉 Última prestação! {nome} está pago por completo!"
                            else:
                                db.session.execute(text("UPDATE pagamentos_agendados SET prestacoes_pagas=:p WHERE id=:i"),
                                    {'p': nova_pag, 'i': pid})
                                msg_fim = f"\n\n📊 Prestação {nova_pag}/{p_tot} — faltam {p_tot-nova_pag}"
                        else:
                            msg_fim = ""
                        db.session.commit()
                        enviar_mensagem(f"{u.phone}@lid",
                            f"💳 *Saiu hoje:*\n{em} {nome} — {valor:.0f}€{msg_fim}")
            except Exception as e:
                log.error(f"aviso_pagamentos {u.phone}: {e}"); db.session.rollback()


def _verificar_saldo_baixo_usuario(u, forcar=False):
    """Lógica central do alerta de saldo baixo — reutilizada pelo cron diário
    e pelo sync em tempo real do Revolut (após detetar uma transação)."""
    try:
        hoje = agora()
        saldo_rev = db.session.execute(text(
            "SELECT saldo FROM bancos_ligados WHERE usuario_id=:u AND banco='revolut' AND ativo=TRUE"),
            {'u': u.id}).scalar()
        if saldo_rev is None or saldo_rev >= 50:
            return
        # Evitar repetir o aviso várias vezes no mesmo dia (exceto se forçado)
        if not forcar:
            estado_atual, dados_atual = get_estado(u.phone)
            ja_avisado_hoje = dados_atual.get('saldo_baixo_avisado_em') == hoje.strftime('%Y-%m-%d')
            if ja_avisado_hoje:
                return
        pendentes = db.session.execute(text(
            "SELECT nome, valor, dia_mes FROM pagamentos_agendados "
            "WHERE usuario_id=:u AND ativo=TRUE AND dia_mes BETWEEN :d1 AND :d2 ORDER BY dia_mes"),
            {'u': u.id, 'd1': hoje.day, 'd2': hoje.day+7}).fetchall()
        msg = f"⚠️ *Saldo baixo no Revolut!*\n💳 Tens apenas {saldo_rev:.2f}€"
        if pendentes:
            msg += f"\n\n📅 E tens pagamentos a sair:\n"
            for n_p, v_p, d_p in pendentes:
                msg += f"  • {n_p} — {v_p:.0f}€ (dia {d_p})\n"
            total_p = sum(v for _,v,_ in pendentes)
            if total_p > saldo_rev:
                msg += f"\n🚨 *Não tens saldo suficiente!* Faltam {total_p-saldo_rev:.0f}€"
        enviar_mensagem(f"{u.phone}@lid", msg)
        try:
            db.session.execute(text(
                "INSERT INTO estado_utilizador (phone, estado, dados, atualizado) "
                "VALUES (:p, 'normal', jsonb_build_object('saldo_baixo_avisado_em', :d), NOW()) "
                "ON CONFLICT (phone) DO UPDATE SET dados = COALESCE(estado_utilizador.dados,'{}'::jsonb) || jsonb_build_object('saldo_baixo_avisado_em', :d)"),
                {'p': u.phone, 'd': hoje.strftime('%Y-%m-%d')})
            db.session.commit()
        except Exception:
            db.session.rollback()
    except Exception as e:
        log.error(f"_verificar_saldo_baixo_usuario {u.phone}: {e}")

def alerta_saldo_baixo():
    """Avisa se o saldo do Revolut cair abaixo de 50€ após atualização diária."""
    with app.app_context():
        hoje = agora()
        if hoje.hour != 8 or hoje.minute >= 30: return
        for u in Usuario.query.all():
            if not u.phone: continue
            _verificar_saldo_baixo_usuario(u, forcar=True)

def balanco_pre_salario():
    """No dia antes de receber, faz o balanço do que sobrou e sugere onde meter."""
    with app.app_context():
        hoje = agora()
        if hoje.hour != 19: return  # uma vez por dia, à noite
        for u in Usuario.query.all():
            if not u.phone: continue
            try:
                dias = dias_para_salario(u)
                if dias != 1:  # só no dia ANTES do salário
                    continue
                disp, p = calcular_disponivel(u)
                if disp <= 5:  # não vale a pena se sobrou pouco
                    if disp >= 0:
                        enviar_mensagem(f"{u.phone}@lid",
                            f"🌙 *Amanhã é dia de salário!*\n\n"
                            f"Este mês gastaste tudo o que tinhas para gastar — sem sobras.\n"
                            f"Amanhã recomeças com o orçamento novo 💪")
                    continue
                # Sobrou dinheiro — sugerir onde meter
                emoji_o = "💰"
                msg = f"🌙 *Amanhã é dia de salário!*\n"
                msg += f"━━━━━━━━━━━━━━\n"
                msg += f"Sobraram-te *{disp:.0f}€* do orçamento deste mês! 🎉\n\n"
                msg += f"Não deixes esse dinheiro parado — sugestões:\n"
                # Reserva
                try:
                    reserva = get_reserva(u.id)
                    if reserva < 2500:
                        msg += f"🛡️ Reforçar a *reserva* (tens {reserva:.0f}€)\n"
                except Exception:
                    pass
                # Objetivos ativos
                try:
                    objs = db.session.execute(text(
                        "SELECT descricao, valor_objetivo, valor_atual FROM objetivos_poupanca "
                        "WHERE usuario_id=:u AND concluido=FALSE ORDER BY id DESC LIMIT 2"),
                        {'u': u.id}).fetchall()
                    for desc_o, val_o, at_o in objs:
                        falta_o = (val_o or 0) - (at_o or 0)
                        em_o = emoji_objetivo(desc_o)
                        msg += f"{em_o} Meter no objetivo *{desc_o}* (falta {falta_o:.0f}€)\n"
                except Exception:
                    pass
                msg += f"\n💡 Diz *guardei {disp:.0f} para [objetivo]* ou *reserva {disp:.0f}*"
                enviar_mensagem(f"{u.phone}@lid", msg)
            except Exception as e:
                log.error(f"balanco_pre_salario {u.phone}: {e}")
                db.session.rollback()

def lembrete_salario():
    with app.app_context():
        hoje = agora()
        if hoje.hour != 9: return
        for u in Usuario.query.all():
            if not u.phone: continue
            pag = dia_pagamento_usuario(u, hoje.year, hoje.month)
            if hoje.day == pag.day:
                enviar_mensagem(f"{u.phone}@lid", "💰 Hoje é dia de salário! Manda o recibo ou diz o valor 🚀")

def fecho_mes():
    with app.app_context():
        hoje = agora()
        if hoje.hour == 10:
            mes_ant = hoje.month-1 if hoje.month>1 else 12
            ano_ant = hoje.year if hoje.month>1 else hoje.year-1
            nomes = ['Janeiro','Fevereiro','Marco','Abril','Maio','Junho','Julho','Agosto','Setembro','Outubro','Novembro','Dezembro']
            for u in Usuario.query.all():
                if not u.phone: continue
                pag_u = dia_pagamento_usuario(u, hoje.year, hoje.month)
                if hoje.day != pag_u.day: continue
                estado, dados = get_estado(u.phone)
                if estado=='fecho_feito' and dados.get('mes')==hoje.month and dados.get('ano')==hoje.year: continue

                # Calcular quanto gastou vs quanto tinha para gastar no mês anterior
                msg_fecho = f"📅 Novo mês! Diz 'resumo anterior' p/ veres {nomes[mes_ant-1]} 📊"
                try:
                    if u.salario_liquido:
                        gasto_ant = db.session.query(db.func.sum(Despesa.valor)).filter(
                            Despesa.usuario_id==u.id,
                            db.extract('month',Despesa.data)==mes_ant,
                            db.extract('year',Despesa.data)==ano_ant,
                            ~Despesa.descricao.like('[conjunta]%'),
                            ~Despesa.descricao.like('[reserva]%')).scalar() or 0
                        _, p_ant = calcular_disponivel(u)
                        orcamento = p_ant.get('gastar', 0)
                        if orcamento > 0 and gasto_ant < orcamento:
                            poupou_extra = orcamento - gasto_ant
                            if poupou_extra >= 30:
                                nome_u = NOMES_CASAL.get(u.phone, '')
                                msg_fecho = (f"🎉 *Boa, {nome_u}!* Em {nomes[mes_ant-1]} gastaste "
                                             f"{gasto_ant:.0f}€ dos {orcamento:.0f}€ que tinhas — "
                                             f"sobraram *{poupou_extra:.0f}€*! 💪\n\n"
                                             f"Isso é dinheiro a mais para a poupança ou um miminho. "
                                             f"Diz 'resumo anterior' para os detalhes 📊")
                except Exception as e:
                    log.error(f"fecho_mes celebracao: {e}")

                enviar_mensagem(f"{u.phone}@lid", msg_fecho)


def aviso_fim_subsidio():
    """No mes a seguir ao subsidio, lembra que o proximo salario e' normal (sem extra).
    Ruben: subsidio em jun/dez -> avisa em jul/jan. Luana: jun/nov -> avisa em jul/dez."""
    with app.app_context():
        hoje = agora()
        if hoje.day != 1 or hoje.hour != 10:
            return
        for u in Usuario.query.all():
            if not u.phone or not u.salario_liquido:
                continue
            meses_sub = [6, 12] if u.phone == PHONE_RUBEN else [6, 11]
            mes_anterior = hoje.month - 1 if hoje.month > 1 else 12
            if mes_anterior in meses_sub:
                nome = NOMES_CASAL.get(u.phone, '')
                enviar_mensagem(f"{u.phone}@lid",
                    f"📋 Atencao {nome}! O mes passado teve subsidio, mas este mes "
                    f"o salario volta ao normal — sem o extra.\n\n"
                    f"Se gastaste a contar com o subsidio, este mes convem apertar um "
                    f"bocadinho o cinto 😉 Eu ajudo-te a manter o rumo!")

def aviso_meio_mes():
    with app.app_context():
        hoje = agora()
        if hoje.day == 15 and hoje.hour == 10:
            for u in Usuario.query.all():
                if u.phone and u.salario_liquido:
                    disp, p = calcular_disponivel(u)
                    gastar = p['gastar']
                    pct = (gastar-disp)/gastar*100 if gastar>0 else 0

                    # Projeção: "a este ritmo" — quanto vai sobrar/faltar no fim do mês
                    import calendar as _cal
                    dias_no_mes = _cal.monthrange(hoje.year, hoje.month)[1]
                    dias_passados = hoje.day
                    dias_restantes = dias_no_mes - dias_passados
                    gasto_ate_agora = gastar - disp
                    ritmo_diario = gasto_ate_agora / dias_passados if dias_passados > 0 else 0
                    projecao_gasto_total = gasto_ate_agora + (ritmo_diario * dias_restantes)
                    projecao_sobra = gastar - projecao_gasto_total

                    if pct >= 70:
                        enviar_mensagem(f"{u.phone}@lid", f"⚠️ A meio do mês e já usaste {pct:.0f}% do orçamento! Vai com calma 💪")
                    elif pct >= 50:
                        enviar_mensagem(f"{u.phone}@lid", f"📊 Meio do mês — usaste {pct:.0f}% do orçamento. No bom caminho! 👍")

                    # Projeção de fim de mês (só se houver sinal claro, evita ruído)
                    if abs(projecao_sobra) >= 30:
                        if projecao_sobra > 0:
                            enviar_mensagem(f"{u.phone}@lid",
                                f"🔮 A este ritmo, acabas o mês com ~*{projecao_sobra:.0f}€* de sobra! Boa gestão 🎯")
                        else:
                            enviar_mensagem(f"{u.phone}@lid",
                                f"🔮 A este ritmo, vais ficar ~*{abs(projecao_sobra):.0f}€* curto no fim do mês.\n"
                                f"Ainda dá para ajustar — faltam {dias_restantes} dias 💪")

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
        if hoje.hour != 10: return
        for u in Usuario.query.all():
            if not u.phone or not u.salario_liquido: continue
            pag = dia_pagamento_usuario(u, hoje.year, hoje.month)
            dias_falta = (pag.date() - hoje.date()).days
            if dias_falta == 7:
                disp, _ = calcular_disponivel(u)
                por_dia = round(disp/7, 2) if disp > 0 else 0
                if por_dia > 0:
                    enviar_mensagem(f"{u.phone}@lid",
                        f"📅 Falta 1 semana para o salario!\n"
                        f"💳 Tens {disp:.0f}€ — da ~{por_dia:.2f}€/dia 💪")
                elif disp < 0:
                    enviar_mensagem(f"{u.phone}@lid",
                        f"📅 Falta 1 semana para o salario.\n"
                        f"⚠️ Estas {abs(disp):.0f}€ acima do orcamento — aguenta firme! 😬")
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
                SELECT DISTINCT ON (u.phone, a.nome) u.phone, a.nome, a.data_aniv
                FROM aniversarios a
                JOIN usuarios u ON a.usuario_id=u.id
                WHERE EXTRACT(month FROM a.data_aniv)=:m
                AND EXTRACT(day FROM a.data_aniv) IN (:d0, :d1, :d5)
                ORDER BY u.phone, a.nome, a.id
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


def resumo_domingo_noite():
    """Domingo a noite (20h): panorama caloroso da semana + preparacao para a proxima.
    Tom de coach amigavel, nao de relatorio."""
    with app.app_context():
        hoje = agora()
        if hoje.weekday() != 6 or hoje.hour != 20:
            return
        from datetime import timedelta as _td
        ha_7_dias = (hoje - _td(days=7)).replace(tzinfo=None)
        for u in Usuario.query.all():
            if not u.phone or not u.salario_liquido:
                continue
            try:
                nome = NOMES_CASAL.get(u.phone, '')
                # Gasto da semana (pessoal, sem conjunta)
                gasto_semana = db.session.query(db.func.sum(Despesa.valor)).filter(
                    Despesa.usuario_id==u.id, Despesa.data >= ha_7_dias,
                    ~Despesa.descricao.like('[conjunta]%'),
                    ~Despesa.descricao.like('[reserva]%')).scalar() or 0
                # Quantos dias sem gastar esta semana
                dias_com_gasto = db.session.execute(text(
                    "SELECT COUNT(DISTINCT DATE(data)) FROM despesas WHERE usuario_id=:u "
                    "AND data >= :d AND descricao NOT LIKE '[conjunta]%'"),
                    {'u': u.id, 'd': ha_7_dias}).scalar() or 0
                dias_sem_gasto = 7 - min(dias_com_gasto, 7)
                # Disponivel atual e dias ate salario
                disp, p = calcular_disponivel(u)
                dias_sal = dias_para_salario(u)

                msg = f"🌙 *Domingo à noite, {nome}!*\n\n"
                msg += f"📊 Esta semana gastaste *{gasto_semana:.0f}€*"
                if dias_sem_gasto >= 2:
                    msg += f" e tiveste *{dias_sem_gasto} dias sem gastar nada* 👏"
                msg += "\n"
                if disp >= 0:
                    msg += f"💳 Tens *{disp:.0f}€* para os próximos {dias_sal} dias até ao salário\n\n"
                    por_dia = disp / dias_sal if dias_sal > 0 else disp
                    if por_dia >= 20:
                        msg += f"Estás tranquilo — dá ~{por_dia:.0f}€/dia. Boa semana! 💪"
                    elif por_dia >= 8:
                        msg += f"Dá ~{por_dia:.0f}€/dia. Com cabeça, chegas bem 😊"
                    else:
                        msg += f"Só ~{por_dia:.0f}€/dia — semana de apertar o cinto 🤏"
                else:
                    msg += f"⚠️ Estás *{abs(disp):.0f}€ acima* do orçamento. "
                    msg += f"Esta semana vamos com calma, {nome} 💙"
                enviar_mensagem(f"{u.phone}@lid", msg)
            except Exception as e:
                log.error(f"resumo_domingo {u.phone}: {e}")

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


# ─── WAHA AUTO-RECOVERY ──────────────────────────────────────
def configurar_webhook():
    """Configura o webhook no WAHA. Chamado no arranque e pelo watchdog."""
    import requests as req
    import time
    try:
        # Verifica estado da sessão
        r = req.get(
            f"{WAHA_URL}/api/sessions/{WAHA_SESSION}",
            headers={'X-Api-Key': WAHA_API_KEY},
            timeout=10
        )
        if r.status_code != 200:
            log.warning(f"WAHA sessão não encontrada, a iniciar...")
            req.post(
                f"{WAHA_URL}/api/sessions/{WAHA_SESSION}/start",
                headers={'X-Api-Key': WAHA_API_KEY, 'Content-Type': 'application/json'},
                json={}, timeout=10
            )
            time.sleep(5)

        # Configura webhook
        webhook_url = os.environ.get('BOT_URL', 'https://luanabot-production.up.railway.app') + '/webhook'
        r2 = req.put(
            f"{WAHA_URL}/api/sessions/{WAHA_SESSION}",
            headers={'X-Api-Key': WAHA_API_KEY, 'Content-Type': 'application/json'},
            json={"config": {"webhooks": [{"url": webhook_url, "events": ["message"], "retries": None, "customHeaders": None}]}},
            timeout=10
        )
        if r2.status_code in [200, 201]:
            log.info(f"Webhook configurado: {webhook_url}")
            return True
        else:
            log.error(f"Webhook falhou: {r2.status_code}")
            return False
    except Exception as e:
        log.error(f"configurar_webhook: {e}")
        return False


def watchdog_waha():
    """Verifica WAHA a cada 10 minutos e reconfigura se necessário."""
    with app.app_context():
        import requests as req
        try:
            r = req.get(
                f"{WAHA_URL}/api/sessions/{WAHA_SESSION}",
                headers={'X-Api-Key': WAHA_API_KEY},
                timeout=8
            )
            if r.status_code != 200:
                log.warning("Watchdog: WAHA não responde, a reconfigurar...")
                configurar_webhook()
                return

            data = r.json()
            status = data.get('status', '')
            webhook_ok = False

            # Verifica se webhook está configurado
            config = data.get('config') or {}
            webhooks = config.get('webhooks', []) if config else []
            if webhooks:
                webhook_ok = True

            if status != 'WORKING':
                log.warning(f"Watchdog: status={status}, a reconfigurar...")
                configurar_webhook()
            elif not webhook_ok:
                log.warning("Watchdog: webhook em falta, a reconfigurar...")
                configurar_webhook()
            else:
                log.info(f"Watchdog: WAHA OK ({status})")
        except Exception as e:
            log.error(f"watchdog_waha: {e}")

# ─── ARRANQUE ────────────────────────────────────────────────
with app.app_context():
    try: db.create_all()
    except Exception as e: log.warning(f"db: {e}")
    criar_tabelas()
    # Configura webhook automaticamente no arranque
    import threading
    def _init_webhook():
        import time; time.sleep(8)
        configurar_webhook()
    threading.Thread(target=_init_webhook, daemon=True).start()

import os, fcntl
_scheduler_lock = open('/tmp/zef_scheduler.lock', 'w')
try:
    fcntl.flock(_scheduler_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    _sou_o_scheduler = True
except (IOError, OSError):
    _sou_o_scheduler = False  # outro worker já tem o scheduler

if not _sou_o_scheduler:
    pass  # worker secundário — não inicia scheduler
else:
    scheduler.add_job(lembrete_recibo,            'cron', hour=11, minute=0)
    scheduler.add_job(processar_salarios_pendentes,'cron', hour=9,  minute=0)
    scheduler.add_job(alertas_preditivos,         'cron', hour=19, minute=0)
    scheduler.add_job(streak_gasto_zero,          'cron', hour=21, minute=0)
    scheduler.add_job(repergunta_recibo,          'cron', hour=15, minute=30)
    scheduler.add_job(enviar_lembretes_gerais,    'cron', minute='*/5')
    scheduler.add_job(lembrete_contas_receber,    'cron', hour=12, minute=0, day_of_week='mon')
    scheduler.add_job(relatorio_mensal_automatico,'cron', hour=10, minute=30)
    scheduler.add_job(lembrete_poupanca_mensal,   'cron', hour=10, minute=0)
    scheduler.add_job(aviso_debitos_fixos,        'cron', hour=9, minute=15)
    scheduler.add_job(lembrete_salario,           'cron', hour=9,  minute=0)
    scheduler.add_job(aviso_pagamentos_agendados, 'cron', hour=9,  minute=10)
    scheduler.add_job(atualizar_saldos_bancarios, 'cron', hour=8,  minute=0)
    scheduler.add_job(verificar_ligacoes_expiradas,'cron', hour=9,  minute=5)
    scheduler.add_job(alerta_saldo_baixo,         'cron', hour=8,  minute=30)
    scheduler.add_job(fecho_mes,                  'cron', hour=10, minute=0)
    scheduler.add_job(aviso_fim_subsidio,         'cron', hour=10, minute=0)
    scheduler.add_job(aviso_meio_mes,             'cron', hour=10, minute=0)
    scheduler.add_job(aviso_uma_semana_salario,   'cron', hour=10, minute=0)
    scheduler.add_job(aviso_fim_mes_wishlist,     'cron', hour=11, minute=0)
    scheduler.add_job(resumo_domingo_noite, 'cron', day_of_week='sun', hour=20, minute=0)
    scheduler.add_job(resumo_semanal,             'cron', hour=9,  minute=30, day_of_week='mon')
    scheduler.add_job(verificar_despesas_futuras, 'cron', hour=8,  minute=0)
    scheduler.add_job(verificar_aniversarios,     'cron', hour=9,  minute=0)
    scheduler.add_job(wrapped_anual,              'cron', hour=20, minute=0)
    scheduler.add_job(sincronizar_revolut,        'interval', minutes=30)
    scheduler.add_job(watchdog_waha,              'interval', minutes=10)
    scheduler.start()
log.info("Ze das Financas v7 iniciado")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
