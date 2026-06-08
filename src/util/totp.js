import { authenticator } from 'otplib';

// Padrão Nibo: TOTP SHA-1, 6 dígitos, janela de 30s (compatível com Google Authenticator/2FAS)
authenticator.options = { algorithm: 'sha1', digits: 6, step: 30 };

export function generateTotp(secret) {
  if (!secret) throw new Error('TOTP secret ausente (NIBO_TOTP_SECRET)');
  return authenticator.generate(secret.replace(/\s+/g, ''));
}
