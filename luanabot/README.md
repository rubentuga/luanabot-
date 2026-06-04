# 💰 Luana Finance Bot

Assistente financeiro pessoal via WhatsApp com IA.

## Deploy no Railway

### 1. Cria os serviços no Railway

No projeto Railway, adiciona:
- **PostgreSQL** → Add Service → Database → PostgreSQL
- **Redis** → Add Service → Database → Redis  
- **Backend** → Add Service → GitHub Repo (aponta para este repositório)
- **Evolution API** → Add Service → Docker Image → `atendai/evolution-api:v2.1.1`

### 2. Variáveis de ambiente (Backend)

```
GROQ_API_KEY=gsk_...
ANTHROPIC_API_KEY=sk-ant-...
DATABASE_URL=${{Postgres.DATABASE_URL}}
REDIS_URL=${{Redis.REDIS_URL}}
EVOLUTION_URL=https://evolution-XXXX.up.railway.app
EVOLUTION_API_KEY=chave_secreta_inventada
EVOLUTION_INSTANCE=luana
OWNER_PHONE=351912345678
```

### 3. Variáveis de ambiente (Evolution API)

```
SERVER_URL=https://evolution-XXXX.up.railway.app
AUTHENTICATION_API_KEY=chave_secreta_inventada
QRCODE_LIMIT=30
CACHE_REDIS_ENABLED=true
CACHE_REDIS_URI=${{Redis.REDIS_URL}}
DATABASE_ENABLED=true
DATABASE_PROVIDER=postgresql
DATABASE_CONNECTION_URI=${{Postgres.DATABASE_URL}}
WEBHOOK_GLOBAL_ENABLED=true
WEBHOOK_GLOBAL_URL=https://backend-XXXX.up.railway.app/webhook
WEBHOOK_EVENTS_MESSAGES_UPSERT=true
```

### 4. Liga o WhatsApp

1. Abre `https://evolution-XXXX.up.railway.app/manager`
2. Login com a API key
3. Cria instância `luana`
4. Lê o QR code com o WhatsApp da Luana

### 5. Testa

Envia "olá" para o bot e deves receber as boas vindas!

## O que o bot faz

- Regista despesas por texto, áudio, foto de talão ou PDF de recibo
- No dia 21, lembra que é dia de salário
- Quando recebe o salário, cria plano financeiro completo
- Compara gastos mês a mês por categoria
- Regista despesas futuras (dentista, seguro, etc.)
- Simula se pode fazer uma compra
- Resumos semanais automáticos
- Meses especiais (junho e novembro)
