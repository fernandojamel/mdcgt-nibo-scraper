# nibo-scraper

Scraper de browser automation pra baixar o **extrato mensal da folha de pagamento** do Nibo das lojas da MDCGT. Roda 1x por mês via n8n, retorna os PDFs em base64 pra o n8n persistir no Supabase.

## Por que esse scraper existe

A MDCGT não tem acesso à API oficial do Nibo (precisa de plano Premium + ser admin da conta, nenhum dos quais temos). Mas como o app web do Nibo é uma SPA que conversa com APIs internas (`api-contador.nibo.com.br`, `arquivos.nibo.com.br`), dá pra logar com Playwright, capturar o Bearer token do SPA e chamar essas APIs direto.

Esse repo é o que faz isso.

## Arquitetura

```
┌─ n8n (cron mensal dia 10 03:00) ─────────────────────────────────┐
│                                                                   │
│  Schedule → Code (monta payload c/ lojas + datas)                 │
│            │                                                       │
│            ▼                                                       │
│  HTTP POST → nibo-scraper:3000/run                                 │
│            │                                                       │
│  ┌─────────▼──────────────────────────────────────────────┐       │
│  │  nibo-scraper (Node + Playwright Chromium)             │       │
│  │  1. Login: passport.nibo → MFA TOTP → empresa.nibo     │       │
│  │  2. Captura Bearer JWT via interceptor                 │       │
│  │  3. Pra cada loja:                                     │       │
│  │     - GET api-contador/.../documents/toreceive?$filter │       │
│  │     - Pra cada doc: GET arquivos.nibo/download/{fileId}│       │
│  │  4. Retorna JSON: [{ pdfBase64, sha256, accrual, ... }]│       │
│  └────────────────────────────────────────────────────────┘       │
│            │                                                       │
│            ▼                                                       │
│  Loop por item:                                                    │
│   - Upload PDF pra Supabase Storage (bucket folha-documentos)      │
│   - UPSERT em folha_documentos (UNIQUE loja_id, competencia, tipo) │
└───────────────────────────────────────────────────────────────────┘
```

## Estrutura de pastas

```
nibo-scraper/
├── Dockerfile                  # Imagem com Playwright + Chromium
├── docker-compose.yml          # Pra subir local ou ao lado do n8n
├── package.json
├── .env.example                # Modelo das variáveis (.env real fica no .gitignore)
├── README.md                   # Este arquivo
└── src/
    ├── server.js               # API Express (POST /run, /list, /health)
    ├── nibo/
    │   ├── config.js           # URLs e constantes do Nibo
    │   ├── auth.js             # Playwright + login + captura de Bearer
    │   └── api.js              # listar folhas + baixar PDFs (axios via Playwright)
    └── util/
        ├── logger.js           # pino
        ├── retry.js            # withRetry + sleep
        └── totp.js             # gera código TOTP do secret 2FA
```

## Setup local (dev)

```bash
cd nibo-scraper
cp .env.example .env
# edite .env com credenciais reais

npm install
npx playwright install chromium       # baixa o Chromium do Playwright

# rodar com hot reload
npm run dev

# testar manualmente (em outro terminal)
curl -X POST http://localhost:3000/list \
  -H "Content-Type: application/json" \
  -H "X-Scraper-Token: $SCRAPER_TOKEN" \
  -d '{
    "lojas": [
      {
        "nome": "Matriz Tijuca",
        "accountantUuid": "46acdb69-e1e8-4f92-861c-98084e1eb1b5",
        "customerUuid":   "276dcb80-6463-4bb7-bab1-c82dd9397b93"
      }
    ],
    "dueDateFrom": "2026-01-01",
    "dueDateTo":   "2026-05-31"
  }'
```

Pra ver o navegador trabalhando (debug visual), seta `HEADLESS=false` no `.env` antes de rodar.

## Deploy via Docker

```bash
cd nibo-scraper
# garante que o .env existe e está preenchido
docker compose build
docker compose up -d

# logs
docker compose logs -f nibo-scraper

# teste
curl http://localhost:3030/health
```

Se rodar **no mesmo host que o n8n** (que é o cenário recomendado), coloca os 2 na mesma rede Docker pra o n8n alcançar via `http://nibo-scraper:3000`:

```yaml
# docker-compose.yml do n8n (ajustar conforme seu setup)
services:
  n8n:
    image: n8nio/n8n
    networks: [scraping]
    # ...
  nibo-scraper:
    extends:
      file: ../nibo-scraper/docker-compose.yml
      service: nibo-scraper
    networks: [scraping]
networks:
  scraping:
```

## Variáveis de ambiente (.env)

| Variável | Descrição |
|---|---|
| `NIBO_EMAIL` | E-mail do usuário Nibo |
| `NIBO_PASSWORD` | Senha do usuário Nibo |
| `NIBO_TOTP_SECRET` | Secret TOTP (mesma string que está no 2FAS / Google Authenticator) |
| `SCRAPER_TOKEN` | Token compartilhado entre n8n e scraper (gere com `openssl rand -hex 32`) |
| `HEADLESS` | `true` em prod, `false` pra debug visual |
| `LOG_LEVEL` | `info` em prod, `debug` pra debugging |
| `RETRY_ATTEMPTS` | Tentativas em falha (default 3) |
| `NAV_TIMEOUT_MS` | Timeout de navegação Playwright (default 60s) |
| `WAIT_TIMEOUT_MS` | Timeout de elementos (default 15s) |

## API HTTP

### `GET /health`

Sem auth. Retorna `{ok:true, ts:...}`. Usado pelo Docker healthcheck.

### `POST /run`

Auth: header `X-Scraper-Token: <token>`.

Faz login, lista folhas das lojas pedidas e baixa os PDFs.

**Body:**
```json
{
  "lojas": [
    {
      "nome": "Matriz Tijuca",
      "accountantUuid": "46acdb69-e1e8-4f92-861c-98084e1eb1b5",
      "customerUuid":   "276dcb80-6463-4bb7-bab1-c82dd9397b93"
    }
  ],
  "dueDateFrom": "2026-01-01",
  "dueDateTo": "2026-05-31",
  "obligationName": "FOLHA DE PAGAMENTO - 5"
}
```

**Resposta:**
```json
{
  "ok": true,
  "ranAt": "2026-06-10T03:00:01Z",
  "durationMs": 45000,
  "itemsCount": 5,
  "errorsCount": 0,
  "items": [
    {
      "lojaNome": "Matriz Tijuca",
      "documentId": 124860967,
      "fileId": "c91b0004-...",
      "fileOriginalName": "885-ExtratoMensal-052026.pdf",
      "accrual": "05/2026",
      "dueDate": "2026-05-29T00:00:00Z",
      "contentType": "application/pdf",
      "sizeBytes": 234567,
      "sha256": "abc123...",
      "pdfBase64": "JVBERi0xLj..."
    }
  ],
  "errors": []
}
```

### `POST /list`

Igual a `/run` mas só lista (não baixa PDFs). Útil pra debug e pra ver quais folhas vão entrar antes de processar tudo.

## Como funciona o login (resumo)

Descoberto via captura HAR no DevTools (jun/2026):

1. `GET passport.nibo.com.br/Account/Login` — pega o `__RequestVerificationToken` do HTML
2. Preenche email → submete → form de senha aparece
3. Preenche senha → submete → `POST /Account/Login` → 302 → `/mfa/choosefa`
4. Seleciona radio "Aplicativo de autenticação" (type=4) → `POST /MFA/ChooseFa` → 302 → `/MFA/VerifyCode`
5. Gera código TOTP do secret → preenche → `POST /MFA/VerifyCode` → 302 → cadeia OAuth → `empresa.nibo.com.br/Auth/Callback?code=...` → 302 → `/Organization` (logado!)
6. SPA carrega e faz GET `/organizations/context` com `Authorization: Bearer <JWT>` — o interceptor do Playwright captura esse JWT.

Daí em diante o scraper usa o JWT pra chamar `api-contador.nibo.com.br` direto (via Playwright request context, que preserva cookies + UA + fingerprint).

## Troubleshooting

### "não capturei o Bearer token"
O SPA do Nibo mudou e não dispara a chamada que estávamos interceptando. Atualizar `src/nibo/auth.js` — provavelmente trocar o `fetch('https://api-empresa.nibo.com.br/organizations/context')` por outra URL.

### "form do TOTP não reconhecido"
A tela de código TOTP do Nibo mudou. Subir com `HEADLESS=false` pra ver o que aparece, ajustar os seletores em `src/nibo/auth.js`.

### "código inválido" mesmo com TOTP correto
Relógio do servidor desincronizado. Em Docker, `time sync` do host deve estar OK. TOTP tem janela de 30s — se o servidor estiver mais que 30s atrasado, falha.

### 401 nas chamadas à api-contador
Bearer expirou. O scraper já tenta uma vez, mas se o problema é recorrente, aumentar `RETRY_ATTEMPTS` ou implementar refresh de token.

### Login pede captcha
Se aparecer captcha, é porque o Nibo identificou padrão de bot. Mitigar:
- Reduzir frequência do scraper (já é mensal, então improvável)
- Esperar 24h, IP "esfria"
- Trocar IP de saída
- Se persistir, contato com suporte do Nibo

## Segurança

- **`.env` nunca vai pro git** (`.gitignore` cobre).
- Em produção, usar **Docker Secrets** ou variáveis de ambiente do orquestrador (Coolify/Dokploy/Portainer) em vez de `.env` montado.
- O `SCRAPER_TOKEN` é só uma camada simples pra impedir que qualquer um chame `/run` se a porta for exposta. **Não exponha** a porta `3000` na internet — só na rede Docker interna.
- Cookies de sessão do Nibo ficam **só na memória** do container (Playwright context). Sem persistência.

## Limitações conhecidas

- **Termos de uso do Nibo** provavelmente proíbem automação. Validar com o contador antes de subir em produção.
- **Cookie "trust this device"** dura ~30 dias. O scraper passa por MFA toda execução pra ser conservador (mensal, não custa).
- **Não baixa anexos extras** (recibo de salário individual, CNAB) — só o extrato mensal consolidado.
- **Não detecta novas lojas automaticamente**. Pra adicionar uma loja nova: capturar o `customerUuid` no DevTools, adicionar no `n8n/sync-nibo-folha.json` (Code "Montar payload") e na coluna `lojas.nibo_customer_uuid` (migration 0020).

## Roadmap

- [ ] Adicionar endpoint `/discover-lojas` que retorna todas as lojas do usuário Nibo (parsing de `Organization/GetOrganizations`)
- [ ] Cache de sessão entre runs (`storageState` do Playwright) pra pular MFA
- [ ] Suporte a guias de impostos (DARF INSS, FGTS) se o contador disponibilizar
