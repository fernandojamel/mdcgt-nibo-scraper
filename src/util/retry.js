import { logger } from './logger.js';

export async function withRetry(fn, opts = {}) {
  // `|| 3` (não `?? 3`) porque Number('') = 0 e Number(undefined) = NaN — os
  // dois são "falsy" e devem cair pro default. Com `??`, NaN/0 passariam e o
  // loop rodaria 0 vezes, fazendo `throw undefined` (bug que mascarava o erro real).
  const tries = opts.tries ?? (Number(process.env.RETRY_ATTEMPTS) || 3);
  const baseMs = opts.baseMs ?? 1000;
  const label = opts.label ?? 'op';

  let lastErr;
  for (let i = 0; i < tries; i++) {
    try {
      return await fn();
    } catch (err) {
      lastErr = err;
      const status = err?.response?.status ?? err?.status;
      // 401/403: sessão expirou — não adianta retry, bubble up pra relogar
      if (status === 401 || status === 403) {
        logger.warn({ label, status }, 'auth expirou, abortando retry');
        throw err;
      }
      const isLast = i === tries - 1;
      const wait = status === 429 ? 30_000 : baseMs * 2 ** i + Math.random() * 500;
      logger.warn(
        { label, attempt: i + 1, tries, status, errMsg: err?.message ?? String(err) },
        isLast ? 'última tentativa falhou' : `retry em ${Math.round(wait)}ms`
      );
      if (!isLast) await sleep(wait);
    }
  }
  throw lastErr;
}

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
