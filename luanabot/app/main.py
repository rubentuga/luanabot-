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
WAHA_URL = os.environ.get('WAHA_URL', 'https://evolution-api-production-b38f.up.railway.app')
WAHA_API_KEY = os.environ.get('WAHA_API_KEY', 'waha123')
WAHA_SESSION = os.environ.get('WAHA_SESSION', 'default')

scheduler = BackgroundScheduler()


# ─── WEBHOOK PRINCIPAL (WAHA format) ─────────────────────────
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.json
        if not data:
            return jsonify({'status': 'ok'})

        log.info(f"Webhook recebido: {json.dumps(data)[:300]}")

        event = data.get('event', '')
        if event not in ['message', 'messages.upsert', '']:
            return jsonify({'status': 'ok'})

        payload = data.get('payload', data)

        # Ignora mensagens enviadas pelo bot (fromMe=true ou id começa com true_)
        from_me = payload.get('fromMe', False)
        msg_id = payload.get('id', '')
        if from_me or (isinstance(msg_id, str) and msg_id.startswith('true_')):
            return jsonify({'status': 'ok'})

        # Obtém número de telefone
        from_field = payload.get('from', '') or payload.get('chatId', '')
        if not from_field and isinstance(msg_id, str) and '_' in msg_id:
            parts = msg_id.split('_')
            if len(parts) >= 2:
                from_field = parts[1]
        phone = from_field.replace('@c.us', '').replace('@s.whatsapp.net', '').replace('@g.us', '').split('@')[0]

        if not phone:
            log.warning("Phone vazio — ignorando")
            return jsonify({'status': 'ok'})

        if OWNER_PHONE and phone != OWNER_PHONE:
            log.info(f"Phone {phone} != OWNER {OWNER_PHONE} — ignorando")
            return jsonify({'status': 'ok'})

        # Obtém texto
        body = payload.get('body', '')
        if isinstance(body, dict):
            texto = body.get('text', '') or body.get('conversation', '')
        else:
            texto = str(body) if body else ''

        if not texto:
            texto = payload.get('text', '') or payload.get('content', '')

        if not texto:
            log.info("Mensagem sem texto — ignorando")
            return jsonify({'status': 'ok'})

        log.info(f"Mensagem de {phone}: {texto}")

        with app.app_context():
            processar_texto(phone, texto, data)

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

    if re.search(r'[0-9]+[.,]?[0-9]*\s*(€|euros|eur)', texto_lower) or re.search(r'(€|euros)\s*[0-9]', texto_lower):
        processar_despesa(phone, usuario, texto)
        return

    resposta = processar_mensagem_ia(texto, usuario, 'geral')
    enviar_mensagem(phone, resposta)


# ─── PROCESSAR DESPESA ────────────────────────────────────────
def processar_despesa(phone, usuario, texto):
    matches = re.findall(r'[0-9]+[.,]?[0-9]*', texto)
    valor = float(matches[0].replace(',', '.')) if matches else 0

    if valor == 0:
        enviar_mensagem(phone, "Nao percebi o valor. Exemplo: Gastei 25 euros Continente")
        return

    texto_lower = texto.lower()
    if any(p in texto_lower for p in ['gota', 'agua', 'bebida']):
        categoria = 'gota'; emoji = '🧃'
    elif any(p in texto_lower for p in ['continente', 'pingo', 'lidl', 'aldi', 'mercado', 'supermercado', 'comida', 'compras']):
        categoria = 'supermercado'; emoji = '🛒'
    elif any(p in texto_lower for p in ['farmacia', 'farmácia', 'remedio', 'medico', 'saude', 'dentista']):
        categoria = 'saude'; emoji = '💊'
    elif any(p in texto_lower for p in ['restaurante', 'jantar', 'almoco', 'cafe', 'pizza', 'sushi', 'kebab', 'burger']):
        categoria = 'restaurante'; emoji = '🍽️'
    elif any(p in texto_lower for p in ['roupa', 'sapatos', 'sapatilhas', 'zara', 'hm', 'shein', 'moda', 'unhas', 'cabelo']):
        categoria = 'pessoal'; emoji = '👗'
    elif any(p in texto_lower for p in ['carro', 'oficina', 'mecanico']):
        categoria = 'carro'; emoji = '🚗'
    elif any(p in texto_lower for p in ['lazer', 'cinema', 'concerto', 'viagem', 'hotel']):
        categoria = 'lazer'; emoji = '🎭'
    else:
        categoria = 'outros'; emoji = '💳'

    despesa = Despesa(usuario_id=usuario.id, valor=valor, categoria=categoria, descricao=texto[:100], data=datetime.now())
    db.session.add(despesa)
    db.session.commit()

    mes_atual = datetime.now().month
    ano_atual = datetime.now().year
    mes_ant = mes_atual - 1 if mes_atual > 1 else 12
    ano_ant = ano_atual if mes_atual > 1 else ano_atual - 1

    total_mes = db.session.query(db.func.sum(Despesa.valor)).filter(
        Despesa.usuario_id == usuario.id, Despesa.categoria == categoria,
        db.extract('month', Despesa.data) == mes_atual, db.extract('year', Despesa.data) == ano_atual
    ).scalar() or 0

    total_ant = db.session.query(db.func.sum(Despesa.valor)).filter(
        Despesa.usuario_id == usuario.id, Despesa.categoria == categoria,
        db.extract('month', Despesa.data) == mes_ant, db.extract('year', Despesa.data) == ano_ant
    ).scalar() or 0

    comparacao = ''
    if total_ant > 0:
        diff = total_mes - total_ant
        if diff > total_ant * 0.2:
            comparacao = f'\nJa gastaste {total_mes:.0f} euros em {categoria} este mes vs {total_ant:.0f} euros no mes passado!'
        elif diff < -total_ant * 0.2:
            comparacao = f'\nGastaste menos em {categoria} que o mes passado. Boa!'

    receita_mes = db.session.query(db.func.sum(Receita.valor)).filter(
        Receita.usuario_id == usuario.id,
        db.extract('month', Receita.data) == mes_atual, db.extract('year', Receita.data) == ano_atual
    ).scalar() or usuario.salario_liquido or 0

    total_gastos = db.session.query(db.func.sum(Despesa.valor)).filter(
        Despesa.usuario_id == usuario.id,
        db.extract('month', Despesa.data) == mes_atual, db.extract('year', Despesa.data) == ano_atual
    ).scalar() or 0

    fixos = (usuario.fixo_carro or 0) + (usuario.fixo_ordem or 0) + (usuario.fixo_unhas or 0) + (usuario.fixo_conjunta or 0)
    disponivel = receita_mes - fixos - total_gastos

    mensagem = f"{emoji} Registado - {valor:.2f} euros ({categoria})\n{categoria.capitalize()} este mes: {total_mes:.2f} euros{comparacao}\nDisponivel: {disponivel:.2f} euros"
    enviar_mensagem(phone, mensagem)


# ─── PROCESSAR COMBUSTÍVEL ────────────────────────────────────
def processar_despesa_combustivel(phone, usuario, texto):
    matches = re.findall(r'[0-9]+[.,]?[0-9]*', texto)
    valor = float(matches[0].replace(',', '.')) if matches else 0

    despesa = Despesa(usuario_id=usuario.id, valor=valor, categoria='combustivel', descricao=texto[:100], data=datetime.now())
    db.session.add(despesa)
    db.session.commit()

    mes_atual = datetime.now().month
    ano_atual = datetime.now().year
    mes_ant = mes_atual - 1 if mes_atual > 1 else 12
    ano_ant = ano_atual if mes_atual > 1 else ano_atual - 1

    total_mes = db.session.query(db.func.sum(Despesa.valor)).filter(
        Despesa.usuario_id == usuario.id, Despesa.categoria == 'combustivel',
        db.extract('month', Despesa.data) == mes_atual, db.extract('year', Despesa.data) == ano_atual
    ).scalar() or 0

    total_ant = db.session.query(db.func.sum(Despesa.valor)).filter(
        Despesa.usuario_id == usuario.id, Despesa.categoria == 'combustivel',
        db.extract('month', Despesa.data) == mes_ant, db.extract('year', Despesa.data) == ano_ant
    ).scalar() or 0

    comparacao = ''
    if total_ant > 0:
        diff = total_mes - total_ant
        if diff > 10:
            comparacao = f'\nMais {diff:.0f} euros em combustivel que o mes passado!'
        elif diff < -10:
            comparacao = f'\nMenos {abs(diff):.0f} euros em combustivel vs mes passado. Boa!'

    mensagem = f"Registado - {valor:.2f} euros (combustivel)\nVW Taigo 1.0 gasolina\nTotal combustivel este mes: {total_mes:.2f} euros{comparacao}\n\nEnvia a tua localizacao para ver o posto mais barato perto de ti!"
    enviar_mensagem(phone, mensagem)


# ─── PROCESSAR RECEITA ────────────────────────────────────────
def processar_receita(phone, usuario, texto):
    matches = re.findall(r'[0-9]+[.,]?[0-9]*', texto)
    valor = float(matches[0].replace(',', '.')) if matches else 0

    if valor == 0:
        enviar_mensagem(phone, "Qual foi o valor exato que recebeste?")
        return

    usuario.salario_liquido = valor
    receita = Receita(usuario_id=usuario.id, valor=valor, descricao='Salario', data=datetime.now())
    db.session.add(receita)
    db.session.commit()
    enviar_plano_salario(phone, usuario, valor)


def enviar_plano_salario(phone, usuario, salario):
    mes_atual = datetime.now().month

    fixo_carro = usuario.fixo_carro or 350
    fixo_ordem = usuario.fixo_ordem or 20
    fixo_conjunta = usuario.fixo_conjunta or 50
    fixo_unhas = usuario.fixo_unhas or (50 if mes_atual <= 9 else 25)
    total_fixos = fixo_carro + fixo_ordem + fixo_conjunta + fixo_unhas

    despesas_futuras = DespesaFutura.query.filter(DespesaFutura.usuario_id == usuario.id, DespesaFutura.pago == False).all()
    total_futuras = sum(d.valor_reserva_mensal for d in despesas_futuras)

    fundo_emergencia = round(salario * 0.05, 2)

    fundo = FundoEmergencia.query.filter_by(usuario_id=usuario.id).first()
    sobrou_mes_ant = 0
    if fundo and fundo.sobrou_mes_anterior > 0:
        sobrou_mes_ant = fundo.sobrou_mes_anterior

    disponivel_gastar = salario - total_fixos - total_futuras - fundo_emergencia
    poupanca = round(disponivel_gastar * 0.15, 2)
    para_gastar = disponivel_gastar - poupanca

    mes_especial = ''
    if mes_atual == 6:
        mes_especial = '\n\nMes do subsidio de ferias! Tens margem extra para roupa de verao ou ferias!'
    elif mes_atual == 11:
        mes_especial = '\n\nMes do subsidio de natal! Separa uma parte para prendas e celebracoes.'

    mensagem = f"Recebeste {salario:.2f} euros!\n\nPlano do mes:\nGastos fixos: {total_fixos:.2f} euros\n  Carro: {fixo_carro:.0f} euros\n  Ordem assistentes: {fixo_ordem:.0f} euros\n  Unhas: {fixo_unhas:.0f} euros\n  Conjunta (Ruben): {fixo_conjunta:.0f} euros\nFundo emergencia: {fundo_emergencia:.2f} euros - mete no Revolut pessoal\nPoupanca: {poupanca:.2f} euros\nPara gastar: {para_gastar:.2f} euros"

    if total_futuras > 0:
        mensagem += f"\nReserva despesas futuras: {total_futuras:.2f} euros"
        for df in despesas_futuras:
            mensagem += f"\n  {df.descricao}: {df.valor_reserva_mensal:.0f} euros/mes"

    if sobrou_mes_ant > 0:
        mensagem += f"\n\nNo mes passado sobrou-te {sobrou_mes_ant:.2f} euros do fundo - mete em poupanca!"
        if fundo:
            fundo.sobrou_mes_anterior = 0
            db.session.commit()

    mensagem += mes_especial
    enviar_mensagem(phone, mensagem)


# ─── DESPESA FUTURA ──────────────────────────────────────────
def processar_despesa_futura(phone, usuario, texto):
    matches = re.findall(r'[0-9]+[.,]?[0-9]*', texto)
    valor = float(matches[0].replace(',', '.')) if matches else 0

    if valor == 0:
        enviar_mensagem(phone, "Exemplo: Mes que vem tenho dentista 40 euros")
        return

    texto_lower = texto.lower()
    if 'dentista' in texto_lower: desc = 'Dentista'
    elif 'seguro' in texto_lower and 'carro' in texto_lower: desc = 'Seguro do carro'
    elif 'inspe' in texto_lower: desc = 'Inspecao do carro'
    elif 'renda' in texto_lower: desc = 'Renda'
    else:
        palavras = [w for w in texto.split() if not re.match(r'[0-9€,.]', w) and len(w) > 2]
        desc = ' '.join(palavras[:3]).capitalize() if palavras else 'Despesa futura'

    meses = 1
    if '2 meses' in texto_lower or 'dois meses' in texto_lower: meses = 2
    elif '3 meses' in texto_lower or 'tres meses' in texto_lower: meses = 3

    reserva_mensal = round(valor / meses, 2)
    despesa_futura = DespesaFutura(
        usuario_id=usuario.id, descricao=desc, valor_total=valor,
        valor_reserva_mensal=reserva_mensal, meses=meses,
        data_prevista=datetime.now() + timedelta(days=30 * meses)
    )
    db.session.add(despesa_futura)
    db.session.commit()

    mensagem = f"Despesa futura registada!\n\n{desc}: {valor:.2f} euros\nDaqui a {meses} mes{'es' if meses > 1 else ''}\nReserva: {reserva_mensal:.2f} euros/mes\n\nVou incluir no proximo plano mensal!"
    enviar_mensagem(phone, mensagem)


# ─── SIMULAÇÃO ───────────────────────────────────────────────
def simular_compra(phone, usuario, texto):
    matches = re.findall(r'[0-9]+[.,]?[0-9]*', texto)
    valor = float(matches[0].replace(',', '.')) if matches else 0

    mes_atual = datetime.now().month
    ano_atual = datetime.now().year

    receita_mes = db.session.query(db.func.sum(Receita.valor)).filter(
        Receita.usuario_id == usuario.id,
        db.extract('month', Receita.data) == mes_atual, db.extract('year', Receita.data) == ano_atual
    ).scalar() or usuario.salario_liquido or 0

    total_gastos = db.session.query(db.func.sum(Despesa.valor)).filter(
        Despesa.usuario_id == usuario.id,
        db.extract('month', Despesa.data) == mes_atual, db.extract('year', Despesa.data) == ano_atual
    ).scalar() or 0

    fixos = (usuario.fixo_carro or 0) + (usuario.fixo_ordem or 0) + (usuario.fixo_unhas or 0) + (usuario.fixo_conjunta or 0)
    disponivel = receita_mes - fixos - total_gastos

    if valor == 0:
        enviar_mensagem(phone, f"Tens {disponivel:.2f} euros disponiveis este mes.")
        return

    pct = valor / disponivel * 100 if disponivel > 0 else 999
    if pct <= 30: resp = f"Sim podes! {valor:.2f} euros e so {pct:.0f}% do disponivel ({disponivel:.2f} euros). Vai!"
    elif pct <= 60: resp = f"Podes mas vai pesar. {valor:.2f} euros de {disponivel:.2f} euros disponiveis ({pct:.0f}%). Tens a certeza?"
    elif pct <= 100: resp = f"Tecnicamente sim mas ficavas com so {disponivel-valor:.2f} euros para o resto do mes. Cuidado!"
    else: resp = f"Nao aconselho. {valor:.2f} euros e mais do que tens disponivel ({disponivel:.2f} euros)."
    enviar_mensagem(phone, resp)


# ─── RESUMO ──────────────────────────────────────────────────
def enviar_resumo(phone, usuario):
    mes_atual = datetime.now().month
    ano_atual = datetime.now().year

    receita_mes = db.session.query(db.func.sum(Receita.valor)).filter(
        Receita.usuario_id == usuario.id,
        db.extract('month', Receita.data) == mes_atual, db.extract('year', Receita.data) == ano_atual
    ).scalar() or usuario.salario_liquido or 0

    total_gastos = db.session.query(db.func.sum(Despesa.valor)).filter(
        Despesa.usuario_id == usuario.id,
        db.extract('month', Despesa.data) == mes_atual, db.extract('year', Despesa.data) == ano_atual
    ).scalar() or 0

    por_categoria = db.session.query(Despesa.categoria, db.func.sum(Despesa.valor)).filter(
        Despesa.usuario_id == usuario.id,
        db.extract('month', Despesa.data) == mes_atual, db.extract('year', Despesa.data) == ano_atual
    ).group_by(Despesa.categoria).all()

    fixos = (usuario.fixo_carro or 0) + (usuario.fixo_ordem or 0) + (usuario.fixo_unhas or 0) + (usuario.fixo_conjunta or 0)
    disponivel = receita_mes - fixos - total_gastos
    nomes_mes = ['Janeiro','Fevereiro','Marco','Abril','Maio','Junho','Julho','Agosto','Setembro','Outubro','Novembro','Dezembro']

    mensagem = f"Resumo - {nomes_mes[mes_atual-1]}\n\nReceita: {receita_mes:.2f} euros\nFixos: {fixos:.2f} euros\nGastos: {total_gastos:.2f} euros\nDisponivel: {disponivel:.2f} euros\n\nPor categoria:"
    for cat, total in por_categoria:
        mensagem += f"\n{cat.capitalize()}: {total:.2f} euros"
    if disponivel < 0: mensagem += "\n\nAtencao - estas a gastar mais do que recebes!"
    elif disponivel < 100: mensagem += "\n\nPouca margem - cuidado!"
    else: mensagem += "\n\nEstas bem!"
    enviar_mensagem(phone, mensagem)


# ─── PLANO ───────────────────────────────────────────────────
def enviar_plano_mes(phone, usuario):
    salario = usuario.salario_liquido or 0
    if salario == 0:
        enviar_mensagem(phone, "Ainda nao registei o teu salario. Diz-me: Recebi X euros")
        return
    enviar_plano_salario(phone, usuario, salario)


# ─── BOAS VINDAS ─────────────────────────────────────────────
def enviar_boas_vindas(phone):
    mensagem = "Ola Luana! Sou o teu assistente financeiro pessoal.\n\nO que podes fazer:\nGastei 25 euros Continente - registo automatico\n50 euros BP - regista combustivel\nRecebi 1200 euros - plano do mes completo\nMes que vem dentista 40 euros - guarda despesa futura\nPosso comprar sapatilhas 90 euros? - simulacao\nResumo - ver tudo do mes\n\nAprendo os teus padroes ao longo do tempo!"
    enviar_mensagem(phone, mensagem)


# ─── LEMBRETES ───────────────────────────────────────────────
def verificar_dia_salario():
    with app.app_context():
        hoje = datetime.now()
        dia_pagamento = hoje.replace(day=21)
        if dia_pagamento.weekday() == 5: dia_pagamento -= timedelta(days=1)
        elif dia_pagamento.weekday() == 6: dia_pagamento -= timedelta(days=2)
        if hoje.day == dia_pagamento.day:
            for u in Usuario.query.all():
                if u.phone:
                    enviar_mensagem(u.phone, "Hoje e dia de salario! Quando receberes envia o recibo ou diz Recebi X euros!")

def resumo_semanal():
    with app.app_context():
        if datetime.now().weekday() == 0:
            for u in Usuario.query.all():
                if u.phone:
                    enviar_resumo(u.phone, u)

def verificar_despesas_futuras():
    with app.app_context():
        amanha = datetime.now() + timedelta(days=1)
        for d in DespesaFutura.query.filter(DespesaFutura.pago == False).all():
            if d.data_prevista and d.data_prevista.date() <= amanha.date():
                u = Usuario.query.get(d.usuario_id)
                if u and u.phone:
                    enviar_mensagem(u.phone, f"Lembrete: {d.descricao} - {d.valor_total:.2f} euros previsto para amanha!")


# ─── INICIALIZAÇÃO ───────────────────────────────────────────
with app.app_context():
    try:
        db.create_all()
    except Exception as e:
        log.warning(f"db.create_all: {e}")

scheduler.add_job(verificar_dia_salario, 'cron', hour=12, minute=0)
scheduler.add_job(resumo_semanal, 'cron', hour=9, minute=30, day_of_week='mon')
scheduler.add_job(verificar_despesas_futuras, 'cron', hour=8, minute=0)
scheduler.start()
log.info("Luana Finance Bot iniciado")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
