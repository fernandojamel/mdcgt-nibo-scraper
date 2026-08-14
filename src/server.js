import express from 'express';
import crypto from 'crypto';
import fs from 'fs';
import os from 'os';
import path from 'path';
import { spawn } from 'child_process';
import { logger } from './util/logger.js';
import { loginAndCaptureSession, closeSession } from './nibo/auth.js';
import { listarFolhasDoIntervalo, baixarPdf } from './nibo/api.js';
import { baixarFolhetins } from './meumania/scraper.js';

const PORT = Number(process.env.PORT ?? 3000);
const SCRAPER_TOKEN = process.env.SCRAPER_TOKEN;
const SUPABASE_URL = process.env.SUPABASE_URL;
const SUPABASE_SERVICE_ROLE_KEY = process.env.SUPABASE_SERVICE_ROLE_KEY;

// Marcador de build: aparece nos logs no boot E no /health, pra confirmar QUAL
// versão do código está rodando no container (o EasyPanel às vezes serve
// imagem cacheada mesmo após um push+deploy).
const BUILD = '2026-08-14-icms-report-fix';

if (!SCRAPER_TOKEN) {
  logger.error('SCRAPER_TOKEN ausente — defina no .env antes de subir');
  process.exit(1);
}

/**
 * Consulta folha_documentos no Supabase pra descobrir quais nibo_file_ids
 * já foram baixados no intervalo. Usado pelo /run quando skipExisting=true.
 *
 * Filtra por vencimento dentro do range — assume que o caller já está
 * pedindo só folhas desse intervalo. Funciona com a regra de UNIQUE
 * (loja_id, competencia, tipo).
 *
 * Se SUPABASE_URL/KEY não estão setadas (cenário antigo), retorna Set vazio
 * — caller deve tratar como "sem optimização", baixa tudo.
 */
async function getExistingFileIds(dueDateFrom, dueDateTo) {
  if (!SUPABASE_URL || !SUPABASE_SERVICE_ROLE_KEY) {
    logger.warn('SUPABASE_URL/KEY não configurados — skipExisting é no-op');
    return new Set();
  }
  const url = `${SUPABASE_URL}/rest/v1/folha_documentos?select=nibo_file_id&vencimento=gte.${dueDateFrom}&vencimento=lte.${dueDateTo}`;
  const res = await fetch(url, {
    headers: {
      apikey: SUPABASE_SERVICE_ROLE_KEY,
      Authorization: `Bearer ${SUPABASE_SERVICE_ROLE_KEY}`,
    },
  });
  if (!res.ok) {
    throw new Error(`Supabase ${res.status}: ${(await res.text()).slice(0, 200)}`);
  }
  const rows = await res.json();
  return new Set(rows.map((r) => r.nibo_file_id).filter(Boolean));
}

const app = express();
// Limite alto porque a resposta pode incluir vários PDFs em base64
app.use(express.json({ limit: '128mb' }));

// Healthcheck (não exige auth — usado pelo Docker healthcheck). Inclui o
// BUILD pra dar pra confirmar de fora (via n8n, por ex.) qual versão do
// código está rodando, sem precisar abrir os logs do EasyPanel.
app.get('/health', (_, res) => res.json({ ok: true, ts: Date.now(), build: BUILD }));

// Middleware: auth de quem chama o scraper (apenas o n8n deve conseguir)
app.use((req, res, next) => {
  if (req.path === '/health') return next();
  const sent = req.headers['x-scraper-token'];
  if (!sent || sent !== SCRAPER_TOKEN) {
    logger.warn({ path: req.path, ip: req.ip }, 'request sem token válido');
    return res.status(401).json({ ok: false, error: 'invalid scraper token' });
  }
  next();
});

/**
 * POST /run
 *
 * Body (JSON):
 *   {
 *     "lojas": [
 *       {
 *         "nome": "Matriz Tijuca",
 *         "lojaId": "<uuid da loja no Supabase>",        // opcional, pra o n8n usar depois
 *         "accountantUuid": "46acdb69-e1e8-4f92-861c-98084e1eb1b5",
 *         "customerUuid":   "276dcb80-6463-4bb7-bab1-c82dd9397b93"
 *       },
 *       { ...filial 01... }
 *     ],
 *     "dueDateFrom": "2026-01-01",   // ISO date
 *     "dueDateTo":   "2026-05-31",
 *     "obligationName": "FOLHA DE PAGAMENTO - 5"   // opcional
 *   }
 *
 * Resposta:
 *   {
 *     "ok": true,
 *     "ranAt": "2026-06-04T12:34:56Z",
 *     "items": [
 *       {
 *         "lojaId": "...",
 *         "lojaNome": "Matriz Tijuca",
 *         "customerUuid": "276dcb80-...",
 *         "documentId": 124860967,
 *         "fileId": "c91b0004-...",
 *         "fileOriginalName": "885-ExtratoMensal-052026.pdf",
 *         "obligationName": "FOLHA DE PAGAMENTO - 5º dia útil",
 *         "accrual": "05/2026",
 *         "dueDate": "2026-05-29T00:00:00Z",
 *         "contentType": "application/pdf",
 *         "sizeBytes": 234567,
 *         "sha256": "abc123...",
 *         "pdfBase64": "JVBERi0xLjQK..."
 *       }
 *     ],
 *     "errors": []
 *   }
 */
app.post('/run', async (req, res) => {
  const startedAt = new Date();
  const body = req.body ?? {};
  const lojas = Array.isArray(body.lojas) ? body.lojas : [];
  // obligationName pode ser string OU array. Array = consultar vários filtros
  // (ex: ['ICMS','DIFAL']) numa MESMA sessão de login (menos flakiness).
  const obligationRaw = body.obligationName ?? 'FOLHA DE PAGAMENTO - 5';
  const obligations = Array.isArray(obligationRaw) ? obligationRaw : [obligationRaw];
  const dueDateFrom = body.dueDateFrom;
  const dueDateTo = body.dueDateTo;
  // Quando true, consulta a tabela folha_documentos antes de baixar e pula
  // PDFs cujo nibo_file_id já está lá. Idempotência + economia em runs diários.
  const skipExisting = body.skipExisting === true;

  if (lojas.length === 0 || !dueDateFrom || !dueDateTo) {
    return res
      .status(400)
      .json({ ok: false, error: 'body precisa de lojas[] + dueDateFrom + dueDateTo' });
  }

  logger.info({ lojas: lojas.length, dueDateFrom, dueDateTo, skipExisting }, 'run iniciado');

  let session;
  const items = [];
  const skipped = [];
  const errors = [];

  // Se skipExisting, busca fileIds já no banco antes do login (economiza tempo)
  let existingFileIds = new Set();
  if (skipExisting) {
    try {
      existingFileIds = await getExistingFileIds(dueDateFrom, dueDateTo);
      logger.info({ count: existingFileIds.size }, 'fileIds já no banco (vão ser skipados)');
    } catch (err) {
      logger.warn({ err: err.message }, 'falhou checar banco — vou baixar tudo por segurança');
    }
  }

  try {
    session = await loginAndCaptureSession({
      email: process.env.NIBO_EMAIL,
      password: process.env.NIBO_PASSWORD,
    });

    for (const loja of lojas) {
      const { lojaId, nome, accountantUuid, customerUuid } = loja;
      if (!accountantUuid || !customerUuid) {
        errors.push({ loja: nome, error: 'accountantUuid e customerUuid obrigatórios' });
        continue;
      }

      try {
        // Consulta cada filtro na mesma sessão; junta tudo, dedup por fileId.
        const seenFileIds = new Set();
        const docs = [];
        for (const obg of obligations) {
          const parte = await listarFolhasDoIntervalo(session, {
            accountantUuid,
            customerUuid,
            obligationName: obg,
            dueDateFrom,
            dueDateTo,
          });
          for (const d of parte) {
            if (d.fileId && seenFileIds.has(d.fileId)) continue;
            if (d.fileId) seenFileIds.add(d.fileId);
            docs.push(d);
          }
        }

        for (const doc of docs) {
          if (skipExisting && existingFileIds.has(doc.fileId)) {
            skipped.push({ loja: nome, fileId: doc.fileId, accrual: doc.accrual });
            continue;
          }
          try {
            const { buffer, contentType } = await baixarPdf(session, {
              fileId: doc.fileId,
              originalName: doc.fileOriginalName,
            });
            const sha256 = crypto.createHash('sha256').update(buffer).digest('hex');
            items.push({
              lojaId,
              lojaNome: nome,
              customerUuid,
              documentId: doc.documentId,
              fileId: doc.fileId,
              fileOriginalName: doc.fileOriginalName,
              obligationName: doc.obligationName,
              accrual: doc.accrual,
              dueDate: doc.dueDate,
              protocolId: doc.protocolId,
              protocolDate: doc.protocolDate,
              contentType,
              sizeBytes: buffer.length,
              sha256,
              pdfBase64: buffer.toString('base64'),
            });
          } catch (err) {
            errors.push({
              loja: nome,
              fileId: doc.fileId,
              fileName: doc.fileOriginalName,
              error: err?.message ?? String(err),
            });
          }
        }
      } catch (err) {
        errors.push({ loja: nome, error: `listagem falhou: ${err?.message ?? String(err)}` });
      }
    }

    res.json({
      ok: true,
      ranAt: startedAt.toISOString(),
      durationMs: Date.now() - startedAt.getTime(),
      itemsCount: items.length,
      skippedCount: skipped.length,
      errorsCount: errors.length,
      items,
      skipped,
      errors,
    });
    logger.info(
      {
        items: items.length,
        skipped: skipped.length,
        errors: errors.length,
        durationMs: Date.now() - startedAt.getTime(),
      },
      'run completo'
    );
  } catch (err) {
    const msg = err?.message ?? String(err);
    logger.error({ err: msg }, 'run falhou');
    res.status(500).json({
      ok: false,
      ranAt: startedAt.toISOString(),
      error: msg,
      itemsCount: items.length,
      skippedCount: skipped.length,
      errorsCount: errors.length + 1,
      items,
      skipped,
      errors: [...errors, { fatal: msg }],
    });
  } finally {
    await closeSession(session);
  }
});

/**
 * POST /list
 *
 * Lista as folhas sem baixar PDFs (debug / sanity check).
 * Mesmo body que /run, mas resposta é só metadados.
 */
app.post('/list', async (req, res) => {
  const body = req.body ?? {};
  const lojas = Array.isArray(body.lojas) ? body.lojas : [];
  const obligationRaw = body.obligationName ?? 'FOLHA DE PAGAMENTO - 5';
  const obligations = Array.isArray(obligationRaw) ? obligationRaw : [obligationRaw];
  const { dueDateFrom, dueDateTo } = body;

  if (lojas.length === 0 || !dueDateFrom || !dueDateTo) {
    return res
      .status(400)
      .json({ ok: false, error: 'body precisa de lojas[] + dueDateFrom + dueDateTo' });
  }

  let session;
  try {
    session = await loginAndCaptureSession({
      email: process.env.NIBO_EMAIL,
      password: process.env.NIBO_PASSWORD,
    });

    const out = [];
    for (const loja of lojas) {
      const seenFileIds = new Set();
      const docs = [];
      for (const obg of obligations) {
        const parte = await listarFolhasDoIntervalo(session, {
          accountantUuid: loja.accountantUuid,
          customerUuid: loja.customerUuid,
          obligationName: obg,
          dueDateFrom,
          dueDateTo,
        });
        for (const d of parte) {
          if (d.fileId && seenFileIds.has(d.fileId)) continue;
          if (d.fileId) seenFileIds.add(d.fileId);
          docs.push(d);
        }
      }
      out.push({ loja: loja.nome, count: docs.length, items: docs });
    }
    res.json({ ok: true, lojas: out });
  } catch (err) {
    logger.error({ err: err.message }, 'list falhou');
    res.status(500).json({ ok: false, error: err.message });
  } finally {
    await closeSession(session);
  }
});

/**
 * POST /sync-consignado
 *
 * Recorrência do dashboard de Consignado. Dispara o script Python
 * scripts/sync_consignado.py DENTRO deste container (que tem Python +
 * pdfplumber e fala com este mesmo server via localhost). O script:
 *   1. Calcula a competência-alvo (mês anterior) — ou usa a do body.
 *   2. Se já está no banco -> no-op (não re-baixa).
 *   3. Senão -> chama POST /run (consignado), parseia e faz upsert via RPCs.
 *
 * Pensado pra ser chamado por um cron do n8n nos dias 10-17 (1 POST/dia).
 *
 * Body (JSON, tudo opcional):
 *   { "competencia": "2026-04", "force": true }
 *
 * Requer no ambiente do container: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY,
 * SCRAPER_TOKEN (mesmo do /run). SCRAPER_URL já vem do Dockerfile (localhost).
 */
app.post('/sync-consignado', (req, res) => {
  const startedAt = new Date();
  const body = req.body ?? {};
  const args = ['scripts/sync_consignado.py'];
  if (typeof body.competencia === 'string' && /^\d{4}-\d{2}$/.test(body.competencia)) {
    args.push(body.competencia);
  }
  if (body.force === true) args.push('--force');

  logger.info({ args }, 'sync-consignado iniciado');

  const py = spawn('python3', args, { cwd: process.cwd(), env: process.env });
  let out = '';
  let err = '';
  py.stdout.on('data', (d) => {
    out += d.toString();
  });
  py.stderr.on('data', (d) => {
    err += d.toString();
  });
  py.on('error', (e) => {
    logger.error({ err: e.message }, 'sync-consignado falhou ao iniciar python3');
    res.status(500).json({ ok: false, error: `spawn python3: ${e.message}` });
  });
  py.on('close', (code) => {
    const durationMs = Date.now() - startedAt.getTime();
    logger.info({ code, durationMs }, 'sync-consignado finalizado');
    res.status(code === 0 ? 200 : 500).json({
      ok: code === 0,
      ranAt: startedAt.toISOString(),
      durationMs,
      exitCode: code,
      // Só as últimas linhas pra resposta não ficar gigante.
      stdout: out.trim().split('\n').slice(-50),
      stderr: err.trim() ? err.trim().split('\n').slice(-30) : [],
    });
  });
});

/**
 * POST /sync-impostos
 *
 * Recorrência do dashboard de ICMS (impostos). Dispara scripts/sync_impostos.py
 * DENTRO deste container. O script:
 *   1. Calcula a competência-alvo (mês anterior) — ou usa a do body.
 *   2. Se já está completa em `impostos` (>=4 guias) -> no-op.
 *   3. Senão -> chama /run nas 2 lojas com filtros ICMS+DIFAL, parseia, upsert.
 *
 * Pensado pra cron do n8n nos dias 08-11 (1 POST/dia).
 *
 * Body (JSON, tudo opcional): { "competencia": "2026-05", "force": true }
 */
app.post('/sync-impostos', (req, res) => {
  const startedAt = new Date();
  const body = req.body ?? {};
  const args = ['scripts/sync_impostos.py'];
  if (typeof body.competencia === 'string' && /^\d{4}-\d{2}$/.test(body.competencia)) {
    args.push(body.competencia);
  }
  if (body.force === true) args.push('--force');

  logger.info({ args }, 'sync-impostos iniciado');

  const py = spawn('python3', args, { cwd: process.cwd(), env: process.env });
  let out = '';
  let err = '';
  py.stdout.on('data', (d) => {
    out += d.toString();
  });
  py.stderr.on('data', (d) => {
    err += d.toString();
  });
  py.on('error', (e) => {
    logger.error({ err: e.message }, 'sync-impostos falhou ao iniciar python3');
    res.status(500).json({ ok: false, error: `spawn python3: ${e.message}` });
  });
  py.on('close', (code) => {
    const durationMs = Date.now() - startedAt.getTime();
    logger.info({ code, durationMs }, 'sync-impostos finalizado');
    res.status(code === 0 ? 200 : 500).json({
      ok: code === 0,
      ranAt: startedAt.toISOString(),
      durationMs,
      exitCode: code,
      stdout: out.trim().split('\n').slice(-50),
      stderr: err.trim() ? err.trim().split('\n').slice(-30) : [],
    });
  });
});

/**
 * POST /sync-gfd
 *
 * Recorrência do dashboard de FGTS (GFD). Dispara scripts/sync_gfd.py no
 * container. O script: calcula a competência-alvo (mês anterior); se já está
 * completa em `impostos` (imposto='FGTS', >=2 linhas) -> no-op; senão chama
 * /run na Matriz (filtro 'GFD'), parseia e faz upsert por loja.
 *
 * Cron do n8n nos dias 17-20. Body (JSON, opcional): { "competencia": "2026-05", "force": true }
 */
app.post('/sync-gfd', (req, res) => {
  const startedAt = new Date();
  const body = req.body ?? {};
  const args = ['scripts/sync_gfd.py'];
  if (typeof body.competencia === 'string' && /^\d{4}-\d{2}$/.test(body.competencia)) {
    args.push(body.competencia);
  }
  if (body.force === true) args.push('--force');

  logger.info({ args }, 'sync-gfd iniciado');

  const py = spawn('python3', args, { cwd: process.cwd(), env: process.env });
  let out = '';
  let err = '';
  py.stdout.on('data', (d) => {
    out += d.toString();
  });
  py.stderr.on('data', (d) => {
    err += d.toString();
  });
  py.on('error', (e) => {
    logger.error({ err: e.message }, 'sync-gfd falhou ao iniciar python3');
    res.status(500).json({ ok: false, error: `spawn python3: ${e.message}` });
  });
  py.on('close', (code) => {
    const durationMs = Date.now() - startedAt.getTime();
    logger.info({ code, durationMs }, 'sync-gfd finalizado');
    res.status(code === 0 ? 200 : 500).json({
      ok: code === 0,
      ranAt: startedAt.toISOString(),
      durationMs,
      exitCode: code,
      stdout: out.trim().split('\n').slice(-50),
      stderr: err.trim() ? err.trim().split('\n').slice(-30) : [],
    });
  });
});

/**
 * POST /sync-pis-cofins
 *
 * Recorrência do dashboard de PIS/COFINS. Dispara scripts/sync_pis_cofins.py.
 * Idempotente: se a competência já tem 4 linhas (2 lojas × PIS/COFINS) -> no-op.
 * Cron do n8n nos dias 22-25. Body (opcional): { "competencia": "2026-04", "force": true }
 */
app.post('/sync-pis-cofins', (req, res) => {
  const startedAt = new Date();
  const body = req.body ?? {};
  const args = ['scripts/sync_pis_cofins.py'];
  if (typeof body.competencia === 'string' && /^\d{4}-\d{2}$/.test(body.competencia)) {
    args.push(body.competencia);
  }
  if (body.force === true) args.push('--force');

  logger.info({ args }, 'sync-pis-cofins iniciado');

  const py = spawn('python3', args, { cwd: process.cwd(), env: process.env });
  let out = '';
  let err = '';
  py.stdout.on('data', (d) => {
    out += d.toString();
  });
  py.stderr.on('data', (d) => {
    err += d.toString();
  });
  py.on('error', (e) => {
    logger.error({ err: e.message }, 'sync-pis-cofins falhou ao iniciar python3');
    res.status(500).json({ ok: false, error: `spawn python3: ${e.message}` });
  });
  py.on('close', (code) => {
    const durationMs = Date.now() - startedAt.getTime();
    logger.info({ code, durationMs }, 'sync-pis-cofins finalizado');
    res.status(code === 0 ? 200 : 500).json({
      ok: code === 0,
      ranAt: startedAt.toISOString(),
      durationMs,
      exitCode: code,
      stdout: out.trim().split('\n').slice(-50),
      stderr: err.trim() ? err.trim().split('\n').slice(-30) : [],
    });
  });
});

/**
 * POST /sync-inss-irrf
 *
 * Recorrência do dashboard de INSS/IRRF (DARF folha, arquivo Excel). Dispara
 * scripts/sync_inss_irrf.py. Idempotente: 4 linhas (2 lojas × INSS/IRRF) -> no-op.
 * Cron do n8n nos dias 22-25. Body (opcional): { "competencia": "2026-04", "force": true }
 */
app.post('/sync-inss-irrf', (req, res) => {
  const startedAt = new Date();
  const body = req.body ?? {};
  const args = ['scripts/sync_inss_irrf.py'];
  if (typeof body.competencia === 'string' && /^\d{4}-\d{2}$/.test(body.competencia)) {
    args.push(body.competencia);
  }
  if (body.force === true) args.push('--force');

  logger.info({ args }, 'sync-inss-irrf iniciado');

  const py = spawn('python3', args, { cwd: process.cwd(), env: process.env });
  let out = '';
  let err = '';
  py.stdout.on('data', (d) => {
    out += d.toString();
  });
  py.stderr.on('data', (d) => {
    err += d.toString();
  });
  py.on('error', (e) => {
    logger.error({ err: e.message }, 'sync-inss-irrf falhou ao iniciar python3');
    res.status(500).json({ ok: false, error: `spawn python3: ${e.message}` });
  });
  py.on('close', (code) => {
    const durationMs = Date.now() - startedAt.getTime();
    logger.info({ code, durationMs }, 'sync-inss-irrf finalizado');
    res.status(code === 0 ? 200 : 500).json({
      ok: code === 0,
      ranAt: startedAt.toISOString(),
      durationMs,
      exitCode: code,
      stdout: out.trim().split('\n').slice(-50),
      stderr: err.trim() ? err.trim().split('\n').slice(-30) : [],
    });
  });
});

/**
 * POST /sync-irpj-csll
 *
 * Recorrência TRIMESTRAL do dashboard de IRPJ/CSLL (mesmo relatório do
 * PIS/COFINS, fechamentos de trimestre). Dispara scripts/sync_irpj_csll.py.
 * Idempotente: 4 linhas (2 lojas × IRPJ/CSLL) -> no-op.
 * Cron do n8n ~dia 30 de jan/abr/jul/out. Body (opcional): { "competencia": "2026-03", "force": true }
 */
app.post('/sync-irpj-csll', (req, res) => {
  const startedAt = new Date();
  const body = req.body ?? {};
  const args = ['scripts/sync_irpj_csll.py'];
  if (typeof body.competencia === 'string' && /^\d{4}-\d{2}$/.test(body.competencia)) {
    args.push(body.competencia);
  }
  if (body.force === true) args.push('--force');

  logger.info({ args }, 'sync-irpj-csll iniciado');

  const py = spawn('python3', args, { cwd: process.cwd(), env: process.env });
  let out = '';
  let err = '';
  py.stdout.on('data', (d) => {
    out += d.toString();
  });
  py.stderr.on('data', (d) => {
    err += d.toString();
  });
  py.on('error', (e) => {
    logger.error({ err: e.message }, 'sync-irpj-csll falhou ao iniciar python3');
    res.status(500).json({ ok: false, error: `spawn python3: ${e.message}` });
  });
  py.on('close', (code) => {
    const durationMs = Date.now() - startedAt.getTime();
    logger.info({ code, durationMs }, 'sync-irpj-csll finalizado');
    res.status(code === 0 ? 200 : 500).json({
      ok: code === 0,
      ranAt: startedAt.toISOString(),
      durationMs,
      exitCode: code,
      stdout: out.trim().split('\n').slice(-50),
      stderr: err.trim() ? err.trim().split('\n').slice(-30) : [],
    });
  });
});

/**
 * POST /sync-csll-pis-cofins-pj
 *
 * Recorrência do dashboard CSLL/PIS/COFINS PJ (retenção PJ a PJ — IRRF +
 * CSLL/PIS/COFINS unificados numa DARF). Dispara scripts/sync_csll_pis_cofins_pj.py.
 * Idempotente: 2 linhas (1 por loja) -> no-op. Cron n8n dias 15-20.
 *
 * Body (opcional): { "competencia": "2026-05", "force": true }
 */
app.post('/sync-csll-pis-cofins-pj', (req, res) => {
  const startedAt = new Date();
  const body = req.body ?? {};
  const args = ['scripts/sync_csll_pis_cofins_pj.py'];
  if (typeof body.competencia === 'string' && /^\d{4}-\d{2}$/.test(body.competencia)) {
    args.push(body.competencia);
  }
  if (body.force === true) args.push('--force');

  logger.info({ args }, 'sync-csll-pis-cofins-pj iniciado');

  const py = spawn('python3', args, { cwd: process.cwd(), env: process.env });
  let out = '';
  let err = '';
  py.stdout.on('data', (d) => { out += d.toString(); });
  py.stderr.on('data', (d) => { err += d.toString(); });
  py.on('error', (e) => {
    logger.error({ err: e.message }, 'sync-csll-pis-cofins-pj falhou ao iniciar python3');
    res.status(500).json({ ok: false, error: `spawn python3: ${e.message}` });
  });
  py.on('close', (code) => {
    const durationMs = Date.now() - startedAt.getTime();
    logger.info({ code, durationMs }, 'sync-csll-pis-cofins-pj finalizado');
    res.status(code === 0 ? 200 : 500).json({
      ok: code === 0,
      ranAt: startedAt.toISOString(),
      durationMs,
      exitCode: code,
      stdout: out.trim().split('\n').slice(-50),
      stderr: err.trim() ? err.trim().split('\n').slice(-30) : [],
    });
  });
});

/**
 * POST /sync-folha
 *
 * Pos-processamento dos PDFs da folha mensal. Roda backfill_eventos.py com
 * --only-pending: filtra documentos em folha_documentos que ainda nao tem
 * entradas em folha_funcionario_mes e processa cada um (parse_folha_pdf +
 * upsert nas RPCs upsert_folha_funcionario_mes + upsert_folha_resumo_mes).
 *
 * Idempotente — re-rodar e seguro (nao reprocessa o que ja esta no banco).
 *
 * Body (JSON, todos opcionais):
 *   { "documento_id": "uuid" }  -> processa SO esse doc (forca, ignora pending)
 *   sem body                      -> processa todos os pendentes
 */
app.post('/sync-folha', (req, res) => {
  const startedAt = new Date();
  const body = req.body ?? {};
  const args = ['scripts/backfill_eventos.py'];
  if (typeof body.documento_id === 'string' && body.documento_id.length > 0) {
    args.push('--only-id', body.documento_id);
  } else {
    args.push('--only-pending');
  }

  logger.info({ args }, 'sync-folha iniciado');

  const py = spawn('python3', args, { cwd: process.cwd(), env: process.env });
  let out = '';
  let err = '';
  py.stdout.on('data', (d) => {
    out += d.toString();
  });
  py.stderr.on('data', (d) => {
    err += d.toString();
  });
  py.on('error', (e) => {
    logger.error({ err: e.message }, 'sync-folha falhou ao iniciar python3');
    res.status(500).json({ ok: false, error: `spawn python3: ${e.message}` });
  });
  py.on('close', (code) => {
    const durationMs = Date.now() - startedAt.getTime();
    logger.info({ code, durationMs }, 'sync-folha finalizado');
    res.status(code === 0 ? 200 : 500).json({
      ok: code === 0,
      ranAt: startedAt.toISOString(),
      durationMs,
      exitCode: code,
      stdout: out.trim().split('\n').slice(-50),
      stderr: err.trim() ? err.trim().split('\n').slice(-30) : [],
    });
  });
});

/**
 * POST /sync-pex
 *
 * Recorrência do Folhetim PEX (Meu Mania). Loga no meumania.com via Playwright,
 * baixa os N folhetins mais recentes (default 4 — o parser precisa de ~3 pra
 * média móvel do Farol), salva num tmp e dispara scripts/sync_pex_folhetim.py
 * (parseia + upsert do mês mais recente). Idempotente.
 * Cron do n8n nos dias 15-25. Body (opcional): { "quantidade": 4 }
 */
app.post('/sync-pex', async (req, res) => {
  const startedAt = new Date();
  const quantidade = Number(req.body?.quantidade ?? 4);
  let dir;
  try {
    dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pex-'));
    logger.info({ quantidade, dir }, 'sync-pex iniciado (baixando folhetins)');
    const items = await baixarFolhetins({ quantidade, destDir: dir });

    const py = spawn('python3', ['scripts/sync_pex_folhetim.py', dir], {
      cwd: process.cwd(),
      env: process.env,
    });
    let out = '';
    let err = '';
    py.stdout.on('data', (d) => { out += d.toString(); });
    py.stderr.on('data', (d) => { err += d.toString(); });
    py.on('error', (e) => {
      try { fs.rmSync(dir, { recursive: true, force: true }); } catch {}
      logger.error({ err: e.message }, 'sync-pex: spawn python3 falhou');
      res.status(500).json({ ok: false, error: `spawn python3: ${e.message}` });
    });
    py.on('close', (code) => {
      try { fs.rmSync(dir, { recursive: true, force: true }); } catch {}
      const durationMs = Date.now() - startedAt.getTime();
      logger.info({ code, durationMs }, 'sync-pex finalizado');
      res.status(code === 0 ? 200 : 500).json({
        ok: code === 0,
        ranAt: startedAt.toISOString(),
        durationMs,
        baixados: items.map((i) => ({ lead: i.lead, kb: Math.round(i.sizeBytes / 1024) })),
        exitCode: code,
        stdout: out.trim() ? out.trim().split('\n').slice(-40) : [],
        stderr: err.trim() ? err.trim().split('\n').slice(-20) : [],
      });
    });
  } catch (e) {
    if (dir) { try { fs.rmSync(dir, { recursive: true, force: true }); } catch {} }
    logger.error({ err: e?.message ?? String(e) }, 'sync-pex falhou');
    res.status(500).json({ ok: false, error: e?.message ?? String(e) });
  }
});

app.listen(PORT, () => {
  logger.info({ port: PORT, build: BUILD }, 'nibo-scraper escutando');
});

// Encerramento gracioso
const shutdown = (sig) => {
  logger.info({ sig }, 'desligando');
  process.exit(0);
};
process.on('SIGTERM', shutdown);
process.on('SIGINT', shutdown);
