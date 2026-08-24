# Recorrência mensal — Consignado (FGTS Digital)

Automatiza a entrada do documento **"GFD E-CONSIGNADO - DEMONSTRATIVO - R.E."**
(Nibo → CONTADOR → "Documentos recebidos") no dashboard de Consignado.

O documento da competência **X** chega no Nibo entre os dias **10 e 17** do mês
**X+1**. Por isso o cron roda diariamente nessa janela.

## Arquitetura (reusa n8n + nibo-scraper, igual à folha)

```
n8n (cron 10-17, 04:00)
   │  POST http://nibo-scraper:3000/sync-consignado   (X-Scraper-Token)
   ▼
nibo-scraper (container)                         <- agora tem Python + pdfplumber
   │  roda scripts/sync_consignado.py:
   │   1. competência-alvo = mês anterior
   │   2. se já está em consignado_resumo_mes -> no-op (não re-baixa)
   │   3. senão -> chama o PRÓPRIO /run (filtro "GFD E-CONSIGNADO", Matriz),
   │      parseia (parse_consignado_pdf) e faz upsert via RPCs
   ▼
Supabase (consignado_resumo_mes / consignado_mes)
```

Idempotente: o n8n manda 1 POST/dia na janela; no dia em que o doc aparece, é
ingerido; nos outros dias é no-op. O n8n **não roda Python** — só faz HTTP.

## Componentes (já no repo)

- `Dockerfile` do nibo-scraper: agora instala **Python3 + pdfplumber** e copia
  `scripts/`. Define `SCRAPER_URL=http://localhost:3000` (sync fala com o próprio
  server).
- `src/server.js`: endpoint **`POST /sync-consignado`** (protegido por token) que
  dispara o `sync_consignado.py`.
- `scripts/{parse_consignado_pdf,backfill_consignado,sync_consignado}.py`.
- `n8n/sync-nibo-consignado.json`: workflow de agendamento (importar no n8n).

## Deploy (EasyPanel + n8n)

1. **Variáveis de ambiente** no serviço **nibo-scraper** (EasyPanel → o serviço →
   Ambiente). Garanta que existem:
   - `SUPABASE_URL`
   - `SUPABASE_SERVICE_ROLE_KEY`
   - `SCRAPER_TOKEN`  (já deve existir — é o mesmo do `/run`)
   - (`SCRAPER_URL` já vem do Dockerfile)
2. **Redeploy do nibo-scraper**: EasyPanel → serviço nibo-scraper → **Implantar**
   (rebuild com o novo Dockerfile). ⚠️ Acompanhe o build/log — se o `pip install
   pdfplumber` falhar, o scraper da folha também para; nesse caso me avise.
3. **Importar o workflow no n8n**: n8n → Import from File →
   `n8n/sync-nibo-consignado.json` → **Active**.

## Testar (sem esperar o dia 10-17)

Force uma competência já conhecida (re-ingere, idempotente). Pela **console do
serviço nibo-scraper** no EasyPanel (ícone de terminal), ou via n8n:

```bash
curl -X POST http://nibo-scraper:3000/sync-consignado \
  -H "X-Scraper-Token: $SCRAPER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"competencia":"2026-04","force":true}'
```

Resposta esperada: `ok:true` + `stdout` com `[OK] 2026-04-01 ... Tijuca=.. Metropolitano=..`.

> Pra testar a recorrência "de verdade" (achar o doc novo), rode com body `{}`
> dentro da janela 10-17 do mês seguinte à competência.

## Troubleshooting

- **`spawn python3` / módulo não encontrado**: o redeploy com o novo Dockerfile
  não rolou — confirme que o build instalou Python+pdfplumber.
- **`Faltam SUPABASE_*`**: adicione as env vars no serviço nibo-scraper (passo 1).
- **`Token do scraper não definido`**: falta `SCRAPER_TOKEN` no ambiente do container.
- **`scraper não encontrou o documento`**: normal antes do dia 10-17; ou o filtro
  não casou — teste `/list` com `obligationName: "GFD E-CONSIGNADO"`.
- **Loja "não atribuído"**: CPF não casou com Folha/Colaboradores daquele mês —
  carregue a Folha da competência antes e rode com `{"competencia":"<X>","force":true}`.
