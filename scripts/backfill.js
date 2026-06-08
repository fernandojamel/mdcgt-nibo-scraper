/**
 * scripts/backfill.js — backfill manual de extratos de folha do Nibo pro Supabase.
 *
 * O que faz (idempotente):
 *  1. Chama o scraper local (POST /run) com lojas + range de datas.
 *  2. Pra cada PDF retornado:
 *      a. Upload no Supabase Storage (bucket folha-documentos), com upsert.
 *      b. Chama RPC upsert_folha_documento pra gravar/atualizar metadados.
 *  3. Imprime resumo no terminal.
 *
 * Pré-requisitos no .env:
 *   - NIBO_EMAIL, NIBO_PASSWORD, NIBO_TOTP_SECRET (mesmas do scraper)
 *   - SCRAPER_TOKEN
 *   - SUPABASE_URL
 *   - SUPABASE_SERVICE_ROLE_KEY
 *
 * Uso:
 *   1. Subir o scraper em outra janela: npm start
 *   2. Rodar este script: node --env-file=.env scripts/backfill.js
 *
 * Args opcionais via env:
 *   - BACKFILL_FROM=2026-01-01   (default: 1º dia do mês anterior)
 *   - BACKFILL_TO=2026-05-31     (default: hoje)
 *   - SCRAPER_URL=http://localhost:3000  (default)
 */

const SCRAPER_URL = process.env.SCRAPER_URL ?? 'http://localhost:3000';
const SCRAPER_TOKEN = req('SCRAPER_TOKEN');
const SUPABASE_URL = req('SUPABASE_URL');
const SERVICE_KEY = req('SUPABASE_SERVICE_ROLE_KEY');

function req(name) {
  const v = process.env[name];
  if (!v || v.startsWith('cole_') || v.startsWith('trocar_')) {
    console.error(`✗ Variável ${name} ausente ou placeholder no .env`);
    process.exit(1);
  }
  return v;
}

// Lojas a backfillar — UUIDs descobertos via HAR + nome/empresa pra storage path.
// Pra adicionar uma loja, edite aqui E adicione nibo_customer_uuid em lojas (migration 0020).
const LOJAS = [
  {
    nome: 'Matriz Tijuca',
    empresa: 'Tijuca',
    codigo: 'TIJ',
    accountantUuid: '46acdb69-e1e8-4f92-861c-98084e1eb1b5',
    customerUuid:   '276dcb80-6463-4bb7-bab1-c82dd9397b93',
  },
  {
    nome: 'Filial 01 Metropolitano',
    empresa: 'Metropolitano',
    codigo: 'MET',
    accountantUuid: '46acdb69-e1e8-4f92-861c-98084e1eb1b5',
    customerUuid:   '133a28e0-bfc5-4cd2-aa89-da5b30659ae2',
  },
];

function dateRange() {
  if (process.env.BACKFILL_FROM && process.env.BACKFILL_TO) {
    return { from: process.env.BACKFILL_FROM, to: process.env.BACKFILL_TO };
  }
  // Default: do dia 1 do mês anterior até hoje
  const hoje = new Date();
  const inicioMesAnterior = new Date(hoje.getFullYear(), hoje.getMonth() - 1, 1);
  const iso = (d) => d.toISOString().slice(0, 10);
  return { from: iso(inicioMesAnterior), to: iso(hoje) };
}

async function callScraper(from, to) {
  const body = {
    lojas: LOJAS.map((l) => ({
      nome: l.nome,
      accountantUuid: l.accountantUuid,
      customerUuid: l.customerUuid,
    })),
    dueDateFrom: from,
    dueDateTo: to,
  };

  console.log(`→ chamando scraper ${SCRAPER_URL}/run (range ${from}..${to})`);
  const t0 = Date.now();
  const res = await fetch(`${SCRAPER_URL}/run`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Scraper-Token': SCRAPER_TOKEN,
    },
    body: JSON.stringify(body),
  });

  if (!res.ok) {
    const txt = await res.text();
    throw new Error(`scraper ${res.status}: ${txt.slice(0, 500)}`);
  }
  const data = await res.json();
  if (!data.ok) {
    throw new Error(`scraper ok=false: ${data.error ?? JSON.stringify(data.errors)}`);
  }
  console.log(`← scraper retornou ${data.itemsCount} PDFs em ${Math.round((Date.now() - t0) / 1000)}s`);
  return data.items;
}

async function uploadPdf(item) {
  const loja = LOJAS.find((l) => l.customerUuid === item.customerUuid);
  if (!loja) throw new Error(`loja não encontrada pro customerUuid ${item.customerUuid}`);

  const [mes, ano] = String(item.accrual ?? '').split('/');
  if (!mes || !ano) throw new Error(`accrual inválido: ${item.accrual}`);
  const competencia = `${ano}-${mes.padStart(2, '0')}-01`;
  const vencimento = String(item.dueDate ?? '').slice(0, 10);
  const storagePath = `${loja.empresa}/${ano}/${mes.padStart(2, '0')}-${loja.codigo}-extrato.pdf`;

  // (1) Upload PDF no Storage (x-upsert: true sobrescreve se já existe)
  const pdfBuffer = Buffer.from(item.pdfBase64, 'base64');
  const uploadRes = await fetch(
    `${SUPABASE_URL}/storage/v1/object/folha-documentos/${storagePath}`,
    {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${SERVICE_KEY}`,
        'Content-Type': item.contentType ?? 'application/pdf',
        'x-upsert': 'true',
        'Cache-Control': '3600',
      },
      body: pdfBuffer,
    }
  );
  if (!uploadRes.ok) {
    const t = await uploadRes.text();
    throw new Error(`storage upload ${uploadRes.status}: ${t.slice(0, 200)}`);
  }

  // (2) Upsert metadados via RPC
  const rpcRes = await fetch(`${SUPABASE_URL}/rest/v1/rpc/upsert_folha_documento`, {
    method: 'POST',
    headers: {
      apikey: SERVICE_KEY,
      Authorization: `Bearer ${SERVICE_KEY}`,
      'Content-Type': 'application/json',
      Prefer: 'return=representation',
    },
    body: JSON.stringify({
      p_nibo_customer_uuid: item.customerUuid,
      p_competencia: competencia,
      p_vencimento: vencimento,
      p_tipo: 'extrato_mensal',
      p_nibo_document_id: item.documentId,
      p_nibo_file_id: item.fileId,
      p_nibo_obligation_name: item.obligationName,
      p_nibo_protocol_id: item.protocolId,
      p_storage_bucket: 'folha-documentos',
      p_storage_path: storagePath,
      p_sha256: item.sha256,
      p_tamanho_bytes: item.sizeBytes,
      p_source: 'backfill-script',
    }),
  });
  if (!rpcRes.ok) {
    const t = await rpcRes.text();
    throw new Error(`rpc ${rpcRes.status}: ${t.slice(0, 200)}`);
  }

  return { storagePath, competencia, loja: loja.nome };
}

async function main() {
  const { from, to } = dateRange();
  console.log('═══════════════════════════════════════════════════════');
  console.log(`  BACKFILL Nibo → Supabase`);
  console.log(`  Range: ${from} a ${to}`);
  console.log(`  Lojas: ${LOJAS.map((l) => l.nome).join(', ')}`);
  console.log('═══════════════════════════════════════════════════════');

  const items = await callScraper(from, to);
  if (items.length === 0) {
    console.log('Nenhum PDF retornado — nada a backfillar.');
    return;
  }

  const sucessos = [];
  const falhas = [];
  for (const item of items) {
    try {
      const r = await uploadPdf(item);
      sucessos.push(r);
      console.log(`  ✓ ${r.loja} | ${r.competencia} → ${r.storagePath}`);
    } catch (err) {
      falhas.push({ item, error: err.message });
      console.error(`  ✗ ${item.lojaNome} | ${item.accrual}: ${err.message}`);
    }
  }

  console.log('═══════════════════════════════════════════════════════');
  console.log(`  Sucesso: ${sucessos.length} / ${items.length}`);
  console.log(`  Falhas:  ${falhas.length}`);
  console.log('═══════════════════════════════════════════════════════');
  if (falhas.length > 0) process.exit(2);
}

main().catch((err) => {
  console.error('✗ Fatal:', err.message);
  process.exit(1);
});
