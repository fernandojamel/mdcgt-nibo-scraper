import { withRetry } from '../util/retry.js';
import { logger } from '../util/logger.js';
import {
  API_CONTADOR,
  ARQUIVOS,
  buildApiHeaders,
} from './config.js';

/**
 * Faz uma chamada autenticada à API do Nibo usando o request context do Playwright.
 *
 * Por que usar o contexto do Playwright em vez de axios? Porque:
 *  - Compartilha cookies da sessão (necessário pra arquivos.nibo.com.br)
 *  - Compartilha User-Agent, fingerprint TLS — parece o mesmo navegador
 *  - O Nibo pode validar fingerprint num futuro próximo
 *
 * Tradeoff: um pouco mais lento que axios direto, mas muito mais estável.
 */
async function apiGet(session, url, organizationUuid) {
  return withRetry(
    async () => {
      const res = await session.context.request.get(url, {
        headers: buildApiHeaders(session.bearerToken, organizationUuid),
        timeout: 60_000,
      });
      if (!res.ok()) {
        const err = new Error(`GET ${url} → ${res.status()}`);
        err.status = res.status();
        throw err;
      }
      return res.json();
    },
    { label: `apiGet ${url.split('/').slice(-2).join('/')}` }
  );
}

/**
 * Lista todas as lojas/empresas que o usuário logado tem acesso.
 * Útil pra descobrir UUIDs novas sem precisar atualizar config manualmente.
 *
 * Retorna: [{ organizationId, organizationName, accountantId, ... }]
 */
export async function listarLojas(session) {
  // /organizations/context retorna a loja ATUAL, não a lista. A lista vem de
  // empresa.nibo.com.br/Organization/GetOrganizations — mas esse é HTML/json
  // dependendo do header. Vamos chamar e parsear o que vier.
  const res = await session.context.request.get(
    'https://empresa.nibo.com.br/Organization/GetOrganizations',
    {
      headers: {
        Accept: 'application/json',
        'X-Requested-With': 'XMLHttpRequest',
        Referer: 'https://empresa.nibo.com.br/Organization',
      },
    }
  );
  if (!res.ok()) {
    throw new Error(`GetOrganizations → ${res.status()}`);
  }
  // Pode retornar HTML se o endpoint não respeita o Accept; tentamos JSON primeiro
  const text = await res.text();
  try {
    return JSON.parse(text);
  } catch {
    logger.warn('GetOrganizations retornou HTML — não consegui parsear lista de lojas');
    return null;
  }
}

/**
 * Lista documentos do tipo "FOLHA DE PAGAMENTO - 5º dia útil" de uma loja
 * num intervalo de vencimento.
 *
 * Args:
 *  - session: retorno de loginAndCaptureSession
 *  - { accountantUuid, customerUuid }: identificadores do contador e da loja
 *  - { obligationName }: filtro de nome (default "FOLHA DE PAGAMENTO - 5")
 *  - { dueDateFrom, dueDateTo }: datas ISO (ex: "2026-01-01")
 *
 * Retorna: array de documentos. Cada um tem fileId, fileOriginalName,
 *   accrual, dueDate, documentId, obligationName, etc.
 */
export async function listarFolhasDoIntervalo(session, {
  accountantUuid,
  customerUuid,
  obligationName = 'FOLHA DE PAGAMENTO - 5',
  dueDateFrom,
  dueDateTo,
  top = 50,
}) {
  // Monta o filtro OData do mesmo jeito que o SPA do Nibo faz.
  // Usamos contains() em obligationName E accrual pra cobrir as duas variantes.
  const filter = [
    `(contains(obligationName,'${obligationName}') or contains(accrual,'${obligationName}'))`,
    `(dueDate ge ${dueDateFrom}T00:00:00.000Z)`,
    `(dueDate le ${dueDateTo}T23:59:59.000Z)`,
  ].join(' and ');

  const params = new URLSearchParams({
    $filter: filter,
    $orderby: 'protocolDate desc',
    $skip: '0',
    $top: String(top),
  });

  const url = `${API_CONTADOR}/accountants/${accountantUuid}/integrations/customers/${customerUuid}/documents/toreceive?${params}`;

  logger.info({ customerUuid, dueDateFrom, dueDateTo }, 'listando folhas');
  const data = await apiGet(session, url, customerUuid);
  const items = data?.items ?? [];
  logger.info({ count: items.length, customerUuid }, 'folhas encontradas');
  return items;
}

/**
 * Baixa um PDF do Nibo dado um fileId.
 * arquivos.nibo.com.br retorna 302 → Azure Blob SAS URL → PDF bytes.
 * Playwright segue redirects automaticamente.
 *
 * Retorna: { buffer, contentType, originalName }
 */
export async function baixarPdf(session, { fileId, originalName }) {
  const url = `${ARQUIVOS}/download/${fileId}`;
  logger.info({ fileId, originalName }, 'baixando PDF');

  return withRetry(
    async () => {
      const res = await session.context.request.get(url, {
        headers: {
          Referer: 'https://empresa.nibo.com.br/',
          'X-Requested-With': 'XMLHttpRequest',
        },
        timeout: 120_000,
        maxRedirects: 5,
      });
      if (!res.ok()) {
        const err = new Error(`download ${fileId} → ${res.status()}`);
        err.status = res.status();
        throw err;
      }
      const buffer = await res.body();
      const contentType = res.headers()['content-type'] ?? 'application/pdf';
      logger.info({ fileId, bytes: buffer.length }, 'PDF baixado');
      return { buffer, contentType, originalName };
    },
    { label: `download ${fileId}` }
  );
}
