// URLs e identificadores fixos da arquitetura do Nibo, descobertos via captura HAR.
// Documentado em README.md.

export const PASSPORT_BASE = 'https://passport.nibo.com.br';
export const APP_BASE = 'https://empresa.nibo.com.br';
export const API_CONTADOR = 'https://api-contador.nibo.com.br';
export const API_EMPRESA = 'https://api-empresa.nibo.com.br';
export const ARQUIVOS = 'https://arquivos.nibo.com.br';

// OAuth client_id do app empresa.nibo.com.br (fixo, não é segredo — aparece na URL pública)
export const OAUTH_CLIENT_ID = 'D2CBFE38-9803-4DA0-8E2C-4E67F26BA9F5';
export const OAUTH_REDIRECT = `${APP_BASE}/Auth/Callback`;

// Tipo do método 2FA dentro do Nibo. 4 = aplicativo autenticador (TOTP). 1 = SMS.
export const MFA_TYPE_AUTHENTICATOR = 4;

// User-Agent realista — o Nibo bloqueia UAs vazios/bot.
export const USER_AGENT =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
  '(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0';

// Headers comuns pra chamadas autenticadas em api-contador / api-empresa.
// O Bearer token é adicionado dinamicamente por requisição.
export function buildApiHeaders(token, organizationUuid) {
  return {
    Authorization: `Bearer ${token}`,
    Organization: organizationUuid,
    Origin: APP_BASE,
    Referer: `${APP_BASE}/`,
    Accept: 'application/json, text/plain, */*',
    'X-Requested-With': 'XMLHttpRequest',
    'User-Agent': USER_AGENT,
  };
}
