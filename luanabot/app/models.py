from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()

class Usuario(db.Model):
    __tablename__ = 'usuarios'
    id = db.Column(db.Integer, primary_key=True)
    phone = db.Column(db.String(20), unique=True, nullable=False)
    nome = db.Column(db.String(100), default='Luana')
    salario_liquido = db.Column(db.Float, default=0)
    fixo_carro = db.Column(db.Float, default=350)
    fixo_ordem = db.Column(db.Float, default=20)
    fixo_unhas = db.Column(db.Float, default=50)
    fixo_conjunta = db.Column(db.Float, default=50)
    criado_em = db.Column(db.DateTime, default=datetime.now)

class Despesa(db.Model):
    __tablename__ = 'despesas'
    id = db.Column(db.Integer, primary_key=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'))
    valor = db.Column(db.Float, nullable=False)
    categoria = db.Column(db.String(50), default='outros')
    descricao = db.Column(db.String(200))
    data = db.Column(db.DateTime, default=datetime.now)

class Receita(db.Model):
    __tablename__ = 'receitas'
    id = db.Column(db.Integer, primary_key=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'))
    valor = db.Column(db.Float, nullable=False)
    descricao = db.Column(db.String(200))
    data = db.Column(db.DateTime, default=datetime.now)

class DespesaFutura(db.Model):
    __tablename__ = 'despesas_futuras'
    id = db.Column(db.Integer, primary_key=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'))
    descricao = db.Column(db.String(200))
    valor_total = db.Column(db.Float, nullable=False)
    valor_reserva_mensal = db.Column(db.Float, nullable=False)
    meses = db.Column(db.Integer, default=1)
    data_prevista = db.Column(db.DateTime)
    pago = db.Column(db.Boolean, default=False)
    criado_em = db.Column(db.DateTime, default=datetime.now)

class ObjetivoFinanceiro(db.Model):
    __tablename__ = 'objetivos'
    id = db.Column(db.Integer, primary_key=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'))
    descricao = db.Column(db.String(200))
    valor_total = db.Column(db.Float, nullable=False)
    valor_poupado = db.Column(db.Float, default=0)
    contribuicao_mensal = db.Column(db.Float, default=0)
    ativo = db.Column(db.Boolean, default=True)
    criado_em = db.Column(db.DateTime, default=datetime.now)

class FundoEmergencia(db.Model):
    __tablename__ = 'fundo_emergencia'
    id = db.Column(db.Integer, primary_key=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), unique=True)
    saldo = db.Column(db.Float, default=0)
    sobrou_mes_anterior = db.Column(db.Float, default=0)
    atualizado_em = db.Column(db.DateTime, default=datetime.now)
