import os
import json
import logging
import re
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from models import db, Usuario, Despesa, Receita, DespesaFutura, ObjetivoFinanceiro, FundoEmergencia
from whatsapp import enviar_mensagem, enviar_mensagem_com_botoes
from claude_ai import processar_mensagem_ia
from pdf_reader import extrair_salario_pdf
import base64
import tempfile

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///luana.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(app)

OWNER_PHONE = os.environ.get('OWNER_PHONE', '')
EVOLUTION_URL = os.environ.get('EVOLUTION_URL', '')
EVOLUTION_API_KEY = os.environ.get('EVOLUTION_API_KEY', '')
EVOLUTION_INSTANCE = os.environ.get('EVOLUTION_INSTANCE', 'luana')

scheduler = BackgroundScheduler()

# ─── WEBHOOK PRINCIPAL ────────────────────────────────────────
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.json
        if not data:
            return jsonify({'status': 'ok'})

        event = data.get('event', '')
        if event != 'messages.upsert':
            return jsonify({'status': 'ok'})

        msgs = data.get('data', {})
        if isinstance(msgs, list):
            msg_data = msgs[0] if msgs else {}
        else:
            msg_data = msgs

        key = msg_data.get('key', {})
        if key.get('fromMe', False):
            return jsonify({'status': 'ok'})

        remote_jid = key.get('remoteJid', '')
        phone = remote_jid.replace('@s.whatsapp.net', '').replace('@g.us', '')

        if OWNER_PHONE and phone != OWNER_PHONE:
            return jsonify({'status': 'ok'})

        msg = msg_data.get('message', {})

        # Texto
        texto = msg.get('conversation', '') or msg.get('extendedTextMessage', {}).get('text', '')

        # Áudio
        if not texto and 'audioMessage' in msg:
            texto = transcrever_audio_whatsapp(msg_data, phone)

        # Imagem/PDF — recibo ou talão
        if not texto and ('imageMessage' in msg or 'documentMessage' in msg):
            resultado = processar_ficheiro(msg_data, phone)
            if resultado:
                return jsonify({'status': 'ok'})

        if not texto:
            return jsonify({'status': 'ok'})

        with app.app_context():
            processar_texto(phone, texto, msg_data)

    except Exception as e:
        log.error(f'Erro webhook: {e}', exc_info=True)

    return jsonify({'status': 'ok'})


@app.route('/webhook/connection-update', methods=['POST'])
def connection_update():
    return jsonify({'status': 'ok'})


@app.route('/', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'bot': 'Luana Finance Bot'})


# ─── PROCESSAR TEXTO ─────────────────────────────────────────
def processar_texto(phone, texto, msg_data=None):
    usuario = Usuario.query.filter_by(phone=phone).first()
    if not usuario:
        usuario = Usuario(phone=phone, nome='Luana')
        db.session.add(usuario)
        db.session.commit()

    texto_lower = texto.lower().strip()

    # Comandos diretos
    if any(p in texto_lower for p in ['ajuda', 'help', '/start', 'ola', 'olá', 'bom dia', 'boa tarde', 'boa noite']):
        enviar_boas_vindas(phone)
        return

    if any(p in texto_lower for p in ['resumo', 'quanto tenho', 'saldo', 'situação', 'como estou', 'quanto gastei']):
        enviar_resumo(phone, usuario)
        return

    if any(p in texto_lower for p in ['plano', 'transferência', 'transferencias', 'distribuição']):
        enviar_plano_mes(phone, usuario)
        return

    if any(p in texto_lower for p in ['objectivo', 'objetivo', 'poupar para', 'quero poupar']):
        resposta = processar_mensagem_ia(texto, usuario, 'objetivo')
        enviar_mensagem(phone, resposta)
        return

    if any(p in texto_lower for p in ['mês que vem', 'mes que vem', 'próximo mês', 'proximo mes', 'para o mês', 'futuro', 'dentista', 'seguro', 'inspeção', 'inspecao']):
        processar_despesa_futura(phone, usuario, texto)
        return

    if any(p in texto_lower for p in ['posso comprar', 'posso gastar', 'vale a pena', 'consigo comprar']):
        simular_compra(phone, usuario, texto)
        return

    if any(p in texto_lower for p in ['recebi', 'ordenado', 'salário', 'salario', 'recibo', 'vencimento']):
        processar_receita(phone, usuario, texto)
        return

    if any(p in texto_lower for p in ['gasolina', 'posto', 'combustível', 'combustivel', 'bp', 'galp', 'repsol', 'shell']):
        processar_despesa_combustivel(phone, usuario, texto)
        return

    # Tenta detetar despesa genérica (número + descrição)
    if re.search(r'[0-9]+[.,]?[0-9]*\s*(€|euros|eur)', texto_lower) or re.search(r'(€|euros)\s*[0-9]', texto_lower):
        processar_despesa(phone, usuario, texto)
        return

    # IA para tudo o resto
    resposta = processar_mensagem_ia(texto, usuario, 'geral')
    enviar_mensagem(phone, resposta)


# ─── PROCESSAR DESPESA ────────────────────────────────────────
def processar_despesa(phone, usuario, texto):
    matches = re.findall(r'[0-9]+[.,]?[0-9]*', texto)
    valor = float(matches[0].replace(',', '.')) if matches else 0

    if valor == 0:
        enviar_mensagem(phone, "❓ Não percebi o valor. Exemplo: \"Gastei 25€ Continente\"")
        return

    texto_lower = texto.lower()
    if any(p in texto_lower for p in ['gota', 'agua', 'bebida', 'continente agua']):
        categoria = 'gota'
        emoji = '🧃'
    elif any(p in texto_lower for p in ['continente', 'pingo', 'lidl', 'aldi', 'mercado', 'supermercado', 'comida', 'compras']):
        categoria = 'supermercado'
        emoji = '🛒'
    elif any(p in texto_lower for p in ['farmacia', 'farmácia', 'remedio', 'remédio', 'medico', 'médico', 'saude', 'saúde', 'dentista']):
        categoria = 'saude'
        emoji = '💊'
    elif any(p in texto_lower for p in ['restaurante', 'jantar', 'almoço', 'almoco', 'cafe', 'café', 'pizza', 'sushi', 'kebab', 'mcd', 'burger']):
        categoria = 'restaurante'
        emoji = '🍽️'
    elif any(p in texto_lower for p in ['roupa', 'sapatos', 'sapatilhas', 'zara', 'hm', 'shein', 'moda', 'unhas', 'cabelo', 'estetica']):
        categoria = 'pessoal'
        emoji = '👗'
    elif any(p in texto_lower for p in ['carro', 'automovel', 'automóvel', 'oficina', 'mecanico']):
        categoria = 'carro'
        emoji = '🚗'
    elif any(p in texto_lower for p in ['lazer', 'cinema', 'concerto', 'viagem', 'hotel', 'airbnb']):
        categoria = 'lazer'
        emoji = '🎭'
    else:
        categoria = 'outros'
        emoji = '💳'

    despesa = Despesa(
        usuario_id=usuario.id,
        valor=valor,
        categoria=categoria,
        descricao=texto[:100],
        data=datetime.now()
    )
    db.session.add(despesa)
    db.session.commit()

    # Compara com mês anterior
    mes_atual = datetime.now().month
    ano_atual = datetime.now().year
    mes_ant = mes_atual - 1 if mes_atual > 1 else 12
    ano_ant = ano_atual if mes_atual > 1 else ano_atual - 1

    total_mes = db.session.query(db.func.sum(Despesa.valor)).filter(
        Despesa.usuario_id == usuario.id,
        Despesa.categoria == categoria,
        db.extract('month', Despesa.data) == mes_atual,
        db.extract('year', Despesa.data) == ano_atual
    ).scalar() or 0

    total_ant = db.session.query(db.func.sum(Despesa.valor)).filter(
        Despesa.usuario_id == usuario.id,
        Despesa.categoria == categoria,
        db.extract('month', Despesa.data) == mes_ant,
        db.extract('year', Despesa.data) == ano_ant
    ).scalar() or 0

    comparacao = ''
    if total_ant > 0:
        diff = total_mes - total_ant
        if diff > total_ant * 0.2:
            comparacao = f'\n⚠️ Já gastaste {total_mes:.0f}€ em {categoria} este mês vs {total_ant:.0f}€ no mês passado — está a subir!'
        elif diff < -total_ant * 0.2:
            comparacao = f'\n✅ Gastaste menos em {categoria} que o mês passado ({total_ant:.0f}€ → {total_mes:.0f}€). Boa!'

    # Saldo disponível
    receita_mes = db.session.query(db.func.sum(Receita.valor)).filter(
        Receita.usuario_id == usuario.id,
        db.extract('month', Receita.data) == mes_atual,
        db.extract('year', Receita.data) == ano_atual
    ).scalar() or usuario.salario_liquido or 0

    total_gastos = db.session.query(db.func.sum(Despesa.valor)).filter(
        Despesa.usuario_id == usuario.id,
        db.extract('month', Despesa.data) == mes_atual,
        db.extract('year', Despesa.data) == ano_atual
    ).scalar() or 0

    fixos = (usuario.fixo_carro or 0) + (usuario.fixo_ordem or 0) + (usuario.fixo_unhas or 0) + (usuario.fixo_conjunta or 0)
    disponivel = receita_mes - fixos - total_gastos

    mensagem = f"""{emoji} Registado — {valor:.2f}€ ({categoria})
💳 {categoria.capitalize()} este mês: {total_mes:.2f}€{comparacao}
💚 Disponível: {disponivel:.2f}€"""

    enviar_mensagem(phone, mensagem)


# ─── PROCESSAR COMBUSTÍVEL ────────────────────────────────────
def processar_despesa_combustivel(phone, usuario, texto):
    matches = re.findall(r'[0-9]+[.,]?[0-9]*', texto)
    valor = float(matches[0].replace(',', '.')) if matches else 0

    despesa = Despesa(
        usuario_id=usuario.id,
        valor=valor,
        categoria='combustivel',
        descricao=texto[:100],
        data=datetime.now()
    )
    db.session.add(despesa)
    db.session.commit()

    mes_atual = datetime.now().month
    ano_atual = datetime.now().year
    mes_ant = mes_atual - 1 if mes_atual > 1 else 12
    ano_ant = ano_atual if mes_atual > 1 else ano_atual - 1

    total_mes = db.session.query(db.func.sum(Despesa.valor)).filter(
        Despesa.usuario_id == usuario.id,
        Despesa.categoria == 'combustivel',
        db.extract('month', Despesa.data) == mes_atual,
        db.extract('year', Despesa.data) == ano_atual
    ).scalar() or 0

    total_ant = db.session.query(db.func.sum(Despesa.valor)).filter(
        Despesa.usuario_id == usuario.id,
        Despesa.categoria == 'combustivel',
        db.extract('month', Despesa.data) == mes_ant,
        db.extract('year', Despesa.data) == ano_ant
    ).scalar() or 0

    comparacao = ''
    if total_ant > 0:
        diff = total_mes - total_ant
        if diff > 10:
            comparacao = f'\n⚠️ Já gastaste mais {diff:.0f}€ em combustível que o mês passado ({total_ant:.0f}€ → {total_mes:.0f}€)'
        elif diff < -10:
            comparacao = f'\n✅ Menos {abs(diff):.0f}€ em combustível vs mês passado. Boa!'

    mensagem = f"""⛽ Registado — {valor:.2f}€ (combustível)
🚗 VW Taigo 1.0 gasolina
⛽ Total combustível este mês: {total_mes:.2f}€{comparacao}

💡 Posto mais barato perto de ti?
Envia a tua localização e procuro!"""

    enviar_mensagem(phone, mensagem)


# ─── PROCESSAR RECEITA / RECIBO ──────────────────────────────
def processar_receita(phone, usuario, texto):
    matches = re.findall(r'[0-9]+[.,]?[0-9]*', texto)
    valor = float(matches[0].replace(',', '.')) if matches else 0

    if valor == 0:
        enviar_mensagem(phone, "💰 Recebi o registo! Qual foi o valor exato que recebeste?")
        return

    usuario.salario_liquido = valor
    receita = Receita(
        usuario_id=usuario.id,
        valor=valor,
        descricao='Salário',
        data=datetime.now()
    )
    db.session.add(receita)
    db.session.commit()

    enviar_plano_salario(phone, usuario, valor)


def enviar_plano_salario(phone, usuario, salario):
    mes_atual = datetime.now().month
    ano_atual = datetime.now().year

    # Gastos fixos
    fixo_carro = usuario.fixo_carro or 350
    fixo_ordem = usuario.fixo_ordem or 20
    fixo_conjunta = usuario.fixo_conjunta or 50
    fixo_unhas = usuario.fixo_unhas or (50 if mes_atual <= 9 else 25)
    total_fixos = fixo_carro + fixo_ordem + fixo_conjunta + fixo_unhas

    # Despesas futuras registadas
    despesas_futuras = DespesaFutura.query.filter(
        DespesaFutura.usuario_id == usuario.id,
        DespesaFutura.pago == False
    ).all()
    total_futuras = sum(d.valor_reserva_mensal for d in despesas_futuras)

    # Fundo emergência (3-6% do salário)
    fundo_emergencia = round(salario * 0.05, 2)

    # Verifica se sobrou fundo do mês passado
    fundo = FundoEmergencia.query.filter_by(usuario_id=usuario.id).first()
    sobrou_mes_ant = 0
    if fundo and fundo.sobrou_mes_anterior > 0:
        sobrou_mes_ant = fundo.sobrou_mes_anterior

    # Poupança (o que sobra)
    disponivel_gastar = salario - total_fixos - total_futuras - fundo_emergencia
    poupanca = round(disponivel_gastar * 0.15, 2)
    para_gastar = disponivel_gastar - poupanca

    # Mês especial
    mes_especial = ''
    margem_extra = 0
    if mes_atual == 6:
        mes_especial = '\n🌊 *Mês do subsídio de férias!*\nRecebeste extra este mês — podes dar-te uma margem adicional para roupa de verão ou férias. Sugestão: separa metade para poupança e a outra metade para gastos especiais!'
        margem_extra = round(salario * 0.3, 2)
    elif mes_atual == 11:
        mes_especial = '\n🎁 *Mês do subsídio de natal!*\nNatal está a chegar — separa uma parte para prendas e celebrações, e guarda o resto para poupança.'
        margem_extra = round(salario * 0.3, 2)

    mensagem = f"""💰 *Recebeste {salario:.2f}€!*

📋 *Plano do mês:*
🏠 Gastos fixos: {total_fixos:.2f}€
   • Carro: {fixo_carro:.0f}€
   • Ordem assistentes: {fixo_ordem:.0f}€
   • Unhas: {fixo_unhas:.0f}€
   • Conjunta (Ruben): {fixo_conjunta:.0f}€
🛡️ Fundo emergência: {fundo_emergencia:.2f}€ → mete no Revolut pessoal para não tocares
💎 Poupança: {poupanca:.2f}€
💳 Para gastar: {para_gastar:.2f}€"""

    if total_futuras > 0:
        mensagem += f"\n📅 Reserva despesas futuras: {total_futuras:.2f}€"
        for df in despesas_futuras:
            mensagem += f"\n   • {df.descricao}: {df.valor_reserva_mensal:.0f}€/mês"

    if sobrou_mes_ant > 0:
        mensagem += f"\n\n💡 No mês passado sobrou-te {sobrou_mes_ant:.2f}€ do fundo de emergência — mete em poupança!"
        fundo.sobrou_mes_anterior = 0
        db.session.commit()

    mensagem += mes_especial

    enviar_mensagem(phone, mensagem)


# ─── DESPESA FUTURA ──────────────────────────────────────────
def processar_despesa_futura(phone, usuario, texto):
    matches = re.findall(r'[0-9]+[.,]?[0-9]*', texto)
    valor = float(matches[0].replace(',', '.')) if matches else 0

    if valor == 0:
        enviar_mensagem(phone, "📅 Que despesa futura queres registar? Exemplo: \"Mês que vem tenho dentista 40€\"")
        return

    texto_lower = texto.lower()
    if 'dentista' in texto_lower:
        desc = 'Dentista'
    elif 'seguro' in texto_lower and 'carro' in texto_lower:
        desc = 'Seguro do carro'
    elif 'inspeção' in texto_lower or 'inspecao' in texto_lower:
        desc = 'Inspeção do carro'
    elif 'renda' in texto_lower:
        desc = 'Renda'
    else:
        palavras = [w for w in texto.split() if not re.match(r'[0-9€,.]', w) and len(w) > 2]
        desc = ' '.join(palavras[:3]).capitalize() if palavras else 'Despesa futura'

    # Quantos meses faltam
    meses = 1
    if 'dois meses' in texto_lower or '2 meses' in texto_lower:
        meses = 2
    elif 'três meses' in texto_lower or '3 meses' in texto_lower:
        meses = 3

    reserva_mensal = round(valor / meses, 2)

    despesa_futura = DespesaFutura(
        usuario_id=usuario.id,
        descricao=desc,
        valor_total=valor,
        valor_reserva_mensal=reserva_mensal,
        meses=meses,
        data_prevista=datetime.now() + timedelta(days=30 * meses)
    )
    db.session.add(despesa_futura)
    db.session.commit()

    mensagem = f"""📅 Despesa futura registada!

📌 {desc}: {valor:.2f}€
📆 Daqui a {meses} mês{'es' if meses > 1 else ''}
💡 Reserva sugerida: {reserva_mensal:.2f}€/mês

Vou incluir isto no teu próximo plano mensal quando receberes o salário! ✅"""

    enviar_mensagem(phone, mensagem)


# ─── SIMULAÇÃO DE COMPRA ─────────────────────────────────────
def simular_compra(phone, usuario, texto):
    matches = re.findall(r'[0-9]+[.,]?[0-9]*', texto)
    valor = float(matches[0].replace(',', '.')) if matches else 0

    mes_atual = datetime.now().month
    ano_atual = datetime.now().year

    receita_mes = db.session.query(db.func.sum(Receita.valor)).filter(
        Receita.usuario_id == usuario.id,
        db.extract('month', Receita.data) == mes_atual,
        db.extract('year', Receita.data) == ano_atual
    ).scalar() or usuario.salario_liquido or 0

    total_gastos = db.session.query(db.func.sum(Despesa.valor)).filter(
        Despesa.usuario_id == usuario.id,
        db.extract('month', Despesa.data) == mes_atual,
        db.extract('year', Despesa.data) == ano_atual
    ).scalar() or 0

    fixos = (usuario.fixo_carro or 0) + (usuario.fixo_ordem or 0) + (usuario.fixo_unhas or 0) + (usuario.fixo_conjunta or 0)
    disponivel = receita_mes - fixos - total_gastos

    if valor == 0:
        enviar_mensagem(phone, f"💚 Tens {disponivel:.2f}€ disponíveis este mês.")
        return

    if valor <= disponivel * 0.3:
        resposta = f"✅ Sim, podes! {valor:.2f}€ é menos de 30% do que tens disponível ({disponivel:.2f}€). Vai!"
    elif valor <= disponivel * 0.6:
        resposta = f"🟡 Podes, mas vai pesar. {valor:.2f}€ de {disponivel:.2f}€ disponíveis ({round(valor/disponivel*100)}%). Tens a certeza que é necessário agora?"
    elif valor <= disponivel:
        resposta = f"🟠 Tecnicamente sim, mas ficarias com apenas {disponivel-valor:.2f}€ para o resto do mês. Cuidado!"
    else:
        resposta = f"🔴 Não aconselho. {valor:.2f}€ é mais do que tens disponível ({disponivel:.2f}€). Faltam {valor-disponivel:.2f}€."

    enviar_mensagem(phone, resposta)


# ─── RESUMO ──────────────────────────────────────────────────
def enviar_resumo(phone, usuario):
    mes_atual = datetime.now().month
    ano_atual = datetime.now().year

    receita_mes = db.session.query(db.func.sum(Receita.valor)).filter(
        Receita.usuario_id == usuario.id,
        db.extract('month', Receita.data) == mes_atual,
        db.extract('year', Receita.data) == ano_atual
    ).scalar() or usuario.salario_liquido or 0

    total_gastos = db.session.query(db.func.sum(Despesa.valor)).filter(
        Despesa.usuario_id == usuario.id,
        db.extract('month', Despesa.data) == mes_atual,
        db.extract('year', Despesa.data) == ano_atual
    ).scalar() or 0

    por_categoria = db.session.query(
        Despesa.categoria,
        db.func.sum(Despesa.valor)
    ).filter(
        Despesa.usuario_id == usuario.id,
        db.extract('month', Despesa.data) == mes_atual,
        db.extract('year', Despesa.data) == ano_atual
    ).group_by(Despesa.categoria).all()

    fixos = (usuario.fixo_carro or 0) + (usuario.fixo_ordem or 0) + (usuario.fixo_unhas or 0) + (usuario.fixo_conjunta or 0)
    disponivel = receita_mes - fixos - total_gastos

    nomes_mes = ['Janeiro','Fevereiro','Março','Abril','Maio','Junho','Julho','Agosto','Setembro','Outubro','Novembro','Dezembro']

    mensagem = f"""📊 *Resumo — {nomes_mes[mes_atual-1]}*

💰 Receita: {receita_mes:.2f}€
🏠 Fixos: {fixos:.2f}€
🛒 Gastos variáveis: {total_gastos:.2f}€
💚 Disponível: {disponivel:.2f}€

📈 *Por categoria:*"""

    emojis = {'combustivel': '⛽', 'supermercado': '🛒', 'gota': '🧃', 'saude': '💊', 'restaurante': '🍽️', 'pessoal': '👗', 'carro': '🚗', 'lazer': '🎭', 'outros': '💳'}
    for cat, total in por_categoria:
        emoji = emojis.get(cat, '💳')
        mensagem += f"\n{emoji} {cat.capitalize()}: {total:.2f}€"

    if disponivel < 0:
        mensagem += "\n\n🔴 Atenção — estás a gastar mais do que recebes!"
    elif disponivel < 100:
        mensagem += "\n\n🟠 Pouca margem — cuidado com os gastos!"
    else:
        mensagem += "\n\n🟢 Estás bem!"

    enviar_mensagem(phone, mensagem)


# ─── PLANO DO MÊS ────────────────────────────────────────────
def enviar_plano_mes(phone, usuario):
    salario = usuario.salario_liquido or 0
    if salario == 0:
        enviar_mensagem(phone, "💡 Ainda não registei o teu salário. Diz-me: \"Recebi X€\"")
        return
    enviar_plano_salario(phone, usuario, salario)


# ─── BOAS VINDAS ─────────────────────────────────────────────
def enviar_boas_vindas(phone):
    mensagem = """👋 Olá Luana! Sou o teu assistente financeiro pessoal.

💬 *O que podes fazer:*
• "Gastei 25€ Continente" → registo automático
• "50€ BP" → regista combustível + posto mais barato
• "Recebi 1200€" → plano do mês completo
• "Mês que vem dentista 40€" → guarda despesa futura
• "Posso comprar sapatilhas 90€?" → simulação
• "Resumo" → ver tudo do mês
• Foto de talão/recibo → leio automaticamente
• Áudio → transcrevo e registo

🤖 Aprendo os teus padrões ao longo do tempo — quanto mais usares, mais preciso fico!"""
    enviar_mensagem(phone, mensagem)


# ─── ÁUDIO ───────────────────────────────────────────────────
def transcrever_audio_whatsapp(msg_data, phone):
    try:
        from groq import Groq
        import requests

        audio_msg = msg_data.get('message', {}).get('audioMessage', {})
        media_url = audio_msg.get('url', '')
        if not media_url:
            return ''

        headers = {'apikey': EVOLUTION_API_KEY}
        r = requests.get(f"{EVOLUTION_URL}/chat/getBase64FromMediaMessage/{EVOLUTION_INSTANCE}",
                        json={'message': msg_data.get('message', {})},
                        headers=headers, timeout=30)

        if r.status_code != 200:
            return ''

        base64_data = r.json().get('base64', '')
        if not base64_data:
            return ''

        audio_bytes = base64.b64decode(base64_data)
        with tempfile.NamedTemporaryFile(suffix='.ogg', delete=False) as f:
            f.write(audio_bytes)
            f.flush()
            client = Groq(api_key=os.environ.get('GROQ_API_KEY', ''))
            with open(f.name, 'rb') as audio_file:
                transcricao = client.audio.transcriptions.create(
                    model='whisper-large-v3',
                    file=audio_file,
                    language='pt'
                )
            return transcricao.text
    except Exception as e:
        log.error(f'Erro transcrição: {e}')
        return ''


# ─── IMAGEM/PDF ──────────────────────────────────────────────
def processar_ficheiro(msg_data, phone):
    try:
        import requests
        headers = {'apikey': EVOLUTION_API_KEY}
        r = requests.post(
            f"{EVOLUTION_URL}/chat/getBase64FromMediaMessage/{EVOLUTION_INSTANCE}",
            json={'message': msg_data.get('message', {})},
            headers=headers, timeout=30
        )
        if r.status_code != 200:
            return False

        base64_data = r.json().get('base64', '')
        mimetype = r.json().get('mimetype', '')

        if not base64_data:
            return False

        with app.app_context():
            usuario = Usuario.query.filter_by(phone=phone).first()
            if not usuario:
                usuario = Usuario(phone=phone, nome='Luana')
                db.session.add(usuario)
                db.session.commit()

            if 'pdf' in mimetype:
                resultado = extrair_salario_pdf(base64_data)
                if resultado and resultado.get('salario'):
                    usuario.salario_liquido = resultado['salario']
                    receita = Receita(
                        usuario_id=usuario.id,
                        valor=resultado['salario'],
                        descricao='Salário (recibo PDF)',
                        data=datetime.now()
                    )
                    db.session.add(receita)
                    db.session.commit()
                    enviar_plano_salario(phone, usuario, resultado['salario'])
                    return True
            else:
                # Imagem de talão — usa IA para ler
                from claude_ai import ler_talao_imagem
                resultado = ler_talao_imagem(base64_data, mimetype)
                if resultado:
                    processar_texto(phone, resultado, msg_data)
                    return True

        return False
    except Exception as e:
        log.error(f'Erro processar ficheiro: {e}')
        return False


# ─── LEMBRETES AUTOMÁTICOS ───────────────────────────────────
def verificar_dia_salario():
    with app.app_context():
        usuarios = Usuario.query.all()
        hoje = datetime.now()
        dia_pagamento = 21

        # Ajusta para dia útil
        data_pagamento = hoje.replace(day=dia_pagamento)
        if data_pagamento.weekday() == 5:
            data_pagamento -= timedelta(days=1)
        elif data_pagamento.weekday() == 6:
            data_pagamento -= timedelta(days=2)

        if hoje.day == data_pagamento.day:
            for u in usuarios:
                if u.phone:
                    enviar_mensagem(u.phone, f"💰 Hoje é dia de salário! Quando receberes, envia o recibo ou diz \"Recebi X€\" que faço o plano do mês para ti!")


def resumo_semanal():
    with app.app_context():
        if datetime.now().weekday() == 0:  # Segunda
            usuarios = Usuario.query.all()
            for u in usuarios:
                if u.phone:
                    enviar_resumo(u.phone, u)


def verificar_despesas_futuras():
    with app.app_context():
        amanha = datetime.now() + timedelta(days=1)
        despesas = DespesaFutura.query.filter(
            DespesaFutura.pago == False,
            db.func.date(DespesaFutura.data_prevista) <= amanha.date()
        ).all()
        for d in despesas:
            usuario = Usuario.query.get(d.usuario_id)
            if usuario and usuario.phone:
                enviar_mensagem(usuario.phone, f"⚠️ Lembrete: {d.descricao} — {d.valor_total:.2f}€ previsto para amanhã!")


# ─── INICIALIZAÇÃO ───────────────────────────────────────────
def criar_instancia_wpp():
    import requests, time
    time.sleep(10)
    try:
        headers = {'apikey': EVOLUTION_API_KEY, 'Content-Type': 'application/json'}
        r = requests.post(
            f"{EVOLUTION_URL}/instance/create",
            json={"instanceName": EVOLUTION_INSTANCE, "qrcode": True, "integration": "WHATSAPP-BAILEYS"},
            headers=headers, timeout=10
        )
        log.info(f"Instância criada: {r.status_code}")
    except Exception as e:
        log.error(f"Erro criar instância: {e}")


with app.app_context():
    db.create_all()

scheduler.add_job(verificar_dia_salario, 'cron', hour=9, minute=0)
scheduler.add_job(resumo_semanal, 'cron', hour=9, minute=30)
scheduler.add_job(verificar_despesas_futuras, 'cron', hour=8, minute=0)
scheduler.start()
log.info("✅ Luana Finance Bot iniciado")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
